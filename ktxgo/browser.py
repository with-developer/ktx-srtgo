from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from .cdp import CdpConnection, CdpError, CdpPage, cast_dict
from .config import COOKIE_PATH, DATA_DIR, SEARCH_URL

_CHROME_PATHS: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "win32": (
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
}
_CHROME_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
_UA_PLATFORM: dict[str, str] = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "win32": "Windows NT 10.0; Win64; x64",
}
_SAME_SITE = {"Strict", "Lax", "None"}
# DevTools lives on loopback; an http(s)_proxy from the environment must not catch it.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_chrome() -> str:
    override = os.environ.get("KTXGO_CHROME", "").strip()
    if override:
        if not Path(override).is_file():
            raise RuntimeError(f"KTXGO_CHROME points to a missing file: {override}")
        return override

    for candidate in _CHROME_PATHS.get(sys.platform, ()):
        path = Path(os.path.expandvars(candidate)).expanduser()
        if path.is_file():
            return str(path)
    for command in _CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    raise RuntimeError(
        "Google Chrome was not found. Install it, or set KTXGO_CHROME to the executable."
    )


def headless_user_agent(chrome: str) -> str:
    """The UA a headed Chrome of this version would send.

    Headless Chrome announces itself as "HeadlessChrome" in its User-Agent, and
    DynaPath rejects that token. Passing the plain UA as a launch flag avoids
    the CDP override (Emulation) that would otherwise be needed.
    """
    output = subprocess.run(
        [chrome, "--version"], capture_output=True, text=True, timeout=15
    ).stdout
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", output)
    if match is None:
        raise RuntimeError(
            "Could not read the Chrome version for headless mode; run with --no-headless."
        )
    platform = _UA_PLATFORM.get(sys.platform, "X11; Linux x86_64")
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{match.group(1)}.0.0.0 Safari/537.36"
    )


def _to_cookie_param(cookie: dict[str, Any]) -> dict[str, Any] | None:
    name, value = cookie.get("name"), cookie.get("value")
    domain = cookie.get("domain")
    if not isinstance(name, str) or not isinstance(value, str) or not isinstance(domain, str):
        return None

    param: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
        "secure": bool(cookie.get("secure")),
        "httpOnly": bool(cookie.get("httpOnly")),
    }
    if cookie.get("sameSite") in _SAME_SITE:
        param["sameSite"] = cookie["sameSite"]

    expires = cookie.get("expires")
    if not cookie.get("session") and isinstance(expires, (int, float)) and expires > 0:
        if expires <= time.time():
            return None
        param["expires"] = float(expires)
    return param


class BrowserManager:
    """Runs one throwaway Chrome and hands out a CDP-driven tab.

    Each start uses a fresh temporary profile, so several runs never fight over
    a profile lock; the login session travels between runs as a cookie file.
    """

    def __init__(self, *, headless: bool = True, fresh_session: bool = False):
        self._headless: bool = headless
        self._fresh_session: bool = fresh_session
        self._process: subprocess.Popen[bytes] | None = None
        self._profile_dir: Path | None = None
        self._browser_conn: CdpConnection | None = None
        self._page: CdpPage | None = None

    @staticmethod
    def _secure_state_permissions() -> None:
        # Restrict local session artifacts to the current user.
        try:
            if DATA_DIR.exists():
                DATA_DIR.chmod(0o700)
        except OSError:
            pass
        try:
            if COOKIE_PATH.exists():
                COOKIE_PATH.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _wait_for_devtools(self, port: int) -> str:
        """Wait for Chrome's DevTools endpoint; return the browser's WebSocket URL."""
        assert self._process is not None
        deadline = time.monotonic() + 30
        url = f"http://127.0.0.1:{port}/json/version"
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited during startup (exit code {self._process.returncode})."
                )
            try:
                with _LOCAL.open(url, timeout=1) as response:
                    return str(json.load(response)["webSocketDebuggerUrl"])
            except (OSError, ValueError, KeyError):
                time.sleep(0.2)
        raise RuntimeError("Timed out waiting for Chrome's DevTools endpoint.")

    @staticmethod
    def _page_ws_url(port: int) -> str:
        base = f"http://127.0.0.1:{port}"
        with _LOCAL.open(f"{base}/json/list", timeout=10) as response:
            targets = json.load(response)
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return str(target["webSocketDebuggerUrl"])

        request = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
        with _LOCAL.open(request, timeout=10) as response:
            return str(json.load(response)["webSocketDebuggerUrl"])

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        """Make sure the process is gone and reaped."""
        if process.poll() is None:
            process.terminate()
            try:
                _ = process.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            _ = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _launch(self, chrome: str) -> tuple[int, str]:
        # A new profile per attempt: a Chrome that died at startup can leave a
        # lock behind that would make the retry die the same way.
        if self._process is not None:
            self._stop(self._process)
        if self._profile_dir is not None:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        self._profile_dir = Path(tempfile.mkdtemp(prefix="ktxgo-chrome-"))
        # Resolved before the port is picked: `chrome --version` can be slow,
        # and another process could take the port while it runs.
        user_agent = headless_user_agent(chrome) if self._headless else None
        # A fixed port, not --remote-debugging-port=0: with 0 Chrome reports
        # navigator.webdriver = true, which a normal browser never does.
        port = self._free_port()
        args = [
            chrome,
            f"--user-data-dir={self._profile_dir}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if user_agent is not None:
            args += [
                "--headless=new",
                f"--user-agent={user_agent}",
                "--window-size=1440,900",
            ]
        args.append("about:blank")

        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Own session, so Ctrl-C reaches Python (which cleans up) and
            # not Chrome directly.
            start_new_session=os.name == "posix",
        )
        return port, self._wait_for_devtools(port)

    def start(self) -> CdpPage:
        chrome = find_chrome()
        try:
            for attempt in range(3):
                try:
                    port, browser_ws = self._launch(chrome)
                    break
                except RuntimeError:
                    # The port picked a moment ago can be taken before Chrome
                    # binds it; that surfaces as Chrome exiting at startup.
                    if attempt == 2 or self._process is None or self._process.poll() is None:
                        raise
            self._browser_conn = CdpConnection(browser_ws)
            self._page = CdpPage(CdpConnection(self._page_ws_url(port)))
            self._secure_state_permissions()
            if not self._fresh_session:
                self._restore_cookies()
            self._page.goto(SEARCH_URL)
        except BaseException:
            self.close()
            raise
        return self._page

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    def save_cookies(self) -> None:
        """Save the login session to disk for reuse."""
        if self._page is None:
            return
        try:
            cookies = [
                c for c in self._page.get_cookies() if "korail" in str(c.get("domain", ""))
            ]
            DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            COOKIE_PATH.touch(mode=0o600, exist_ok=True)  # never readable by others, even briefly
            COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
            self._secure_state_permissions()
        except (CdpError, OSError):
            pass

    def _restore_cookies(self) -> None:
        """Load previously saved cookies into the fresh browser."""
        if self._page is None:
            return
        try:
            saved = json.loads(COOKIE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(saved, list):
            return
        params = [
            param
            for param in (_to_cookie_param(cast_dict(c)) for c in saved)
            if param is not None
        ]
        try:
            self._page.set_cookies(params)
        except CdpError:
            pass

    def clear_cookies(self) -> None:
        """Delete the saved session file."""
        try:
            COOKIE_PATH.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._page is not None:
            self._page.close()
        if self._browser_conn is not None:
            try:
                _ = self._browser_conn.call("Browser.close", timeout_s=5)
            except CdpError:
                pass  # closing the browser also closes the socket that answers
            self._browser_conn.close()

        process = self._process
        if process is not None:
            try:
                _ = process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._stop(process)
        if self._profile_dir is not None:
            for _attempt in range(3):
                shutil.rmtree(self._profile_dir, ignore_errors=True)
                if not self._profile_dir.exists():
                    break
                time.sleep(0.3)  # Chrome may still be flushing files on its way out

        self._process = None
        self._profile_dir = None
        self._browser_conn = None
        self._page = None

    @property
    def page(self) -> CdpPage:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    def __enter__(self) -> BrowserManager:
        _ = self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
