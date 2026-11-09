from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
import urllib.request
from typing import Any

import pytest
from websockets.sync.server import serve

from ktxgo import browser, cdp, cli
from ktxgo import korail
from ktxgo.korail import (
    BadCredentialsError,
    KorailAPI,
    MacroBlockedError,
)


# ---------------------------------------------------------------------------
# CDP client
# ---------------------------------------------------------------------------


class _Server:
    """A scripted CDP endpoint: `script(request)` yields the frames to send back."""

    def __init__(self, script: Any) -> None:
        def handler(ws: Any) -> None:
            for raw in ws:
                for frame in script(json.loads(raw)):
                    ws.send(json.dumps(frame))

        self._server = serve(handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()


def test_call_matches_the_reply_by_id_and_skips_events() -> None:
    def script(request: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"method": "Runtime.consoleAPICalled", "params": {}},
            {"id": request["id"] + 99, "result": {"stale": True}},
            {"id": request["id"], "result": {"echo": request["method"]}},
        ]

    server = _Server(script)
    try:
        conn = cdp.CdpConnection(server.url)
        assert conn.call("Page.navigate", {"url": "x"}) == {"echo": "Page.navigate"}
        assert conn.call("Runtime.evaluate") == {"echo": "Runtime.evaluate"}
        conn.close()
    finally:
        server.close()


def test_call_raises_the_protocol_error_message() -> None:
    server = _Server(
        lambda r: [{"id": r["id"], "error": {"code": -32000, "message": "No target"}}]
    )
    try:
        conn = cdp.CdpConnection(server.url)
        with pytest.raises(cdp.CdpError, match="No target"):
            conn.call("Page.navigate")
        conn.close()
    finally:
        server.close()


def test_call_times_out_when_the_browser_never_answers() -> None:
    server = _Server(lambda r: [])
    try:
        conn = cdp.CdpConnection(server.url)
        started = time.monotonic()
        with pytest.raises(cdp.CdpError, match="timed out"):
            conn.call("Runtime.evaluate", timeout_s=0.3)
        assert time.monotonic() - started < 3
        conn.close()
    finally:
        server.close()


def test_call_reports_a_crashed_tab() -> None:
    server = _Server(lambda r: [{"method": "Inspector.targetCrashed", "params": {}}])
    try:
        conn = cdp.CdpConnection(server.url)
        with pytest.raises(cdp.CdpError, match="crashed"):
            conn.call("Runtime.evaluate")
        conn.close()
    finally:
        server.close()


class _RecordingConnection:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        return self.result


def test_evaluate_calls_a_function_with_its_argument_like_playwright() -> None:
    conn = _RecordingConnection({"result": {"value": 7}})
    page = cdp.CdpPage(conn)  # type: ignore[arg-type]

    assert page.evaluate("(a) => a.n", {"n": "한글"}) == 7

    method, params = conn.calls[0]
    assert method == "Runtime.evaluate"
    assert params["expression"] == '((a) => a.n)({"n": "한글"})'
    assert params["awaitPromise"] is True and params["returnByValue"] is True


def test_evaluate_without_an_argument_runs_the_source_as_an_expression() -> None:
    conn = _RecordingConnection({"result": {"value": "/ticket/main"}})
    page = cdp.CdpPage(conn)  # type: ignore[arg-type]

    assert page.evaluate("location.pathname") == "/ticket/main"
    assert conn.calls[0][1]["expression"] == "location.pathname"


def test_evaluate_surfaces_a_page_exception() -> None:
    conn = _RecordingConnection(
        {"exceptionDetails": {"exception": {"description": "ReferenceError: nope"}}}
    )
    page = cdp.CdpPage(conn)  # type: ignore[arg-type]

    with pytest.raises(cdp.CdpError, match="ReferenceError"):
        page.evaluate("nope")


def test_click_refuses_an_element_covered_by_an_overlay() -> None:
    conn = _RecordingConnection({"result": {"value": {"x": 5, "y": 6, "hit": False}}})
    page = cdp.CdpPage(conn)  # type: ignore[arg-type]

    assert page.click("button.btn_lookup") is False
    assert [m for m, _ in conn.calls] == ["Runtime.evaluate"]  # no mouse event went out


def test_click_presses_an_uncovered_element() -> None:
    conn = _RecordingConnection({"result": {"value": {"x": 5, "y": 6, "hit": True}}})
    page = cdp.CdpPage(conn)  # type: ignore[arg-type]

    assert page.click("button.btn_lookup") is True
    events = [p["type"] for m, p in conn.calls if m == "Input.dispatchMouseEvent"]
    assert events == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_the_client_never_enables_a_domain() -> None:
    # The whole point of not using Playwright: a "*.enable" is what DynaPath
    # reacts to, so no code path may send one.
    source = "\n".join(
        path.read_text()
        for path in (
            browser.Path(cdp.__file__),
            browser.Path(browser.__file__),
            browser.Path(korail.__file__),
        )
    )
    assert not re.search(r"\b[A-Z][A-Za-z]+\.enable\b", source)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


def test_cookie_param_drops_the_expiry_of_session_cookies() -> None:
    param = browser._to_cookie_param(
        {"name": "JSESSIONID", "value": "v", "domain": ".korail.com", "path": "/",
         "session": True, "expires": -1, "secure": True, "httpOnly": True, "sameSite": "Lax"}
    )

    assert param is not None
    assert "expires" not in param
    assert param["secure"] is True and param["httpOnly"] is True
    assert param["sameSite"] == "Lax"


def test_cookie_param_keeps_a_future_expiry_and_drops_an_expired_cookie() -> None:
    future = time.time() + 3600
    kept = browser._to_cookie_param(
        {"name": "WMONID", "value": "v", "domain": "www.korail.com", "expires": future}
    )
    expired = browser._to_cookie_param(
        {"name": "old", "value": "v", "domain": "www.korail.com", "expires": time.time() - 5}
    )

    assert kept is not None and kept["expires"] == pytest.approx(future)
    assert kept["path"] == "/"
    assert expired is None


def test_cookie_param_rejects_malformed_entries_and_unknown_same_site() -> None:
    assert browser._to_cookie_param({"name": "a", "value": "b"}) is None
    param = browser._to_cookie_param(
        {"name": "a", "value": "b", "domain": "korail.com", "sameSite": "Weird"}
    )
    assert param is not None and "sameSite" not in param


def test_headless_user_agent_uses_the_installed_chrome_version(monkeypatch) -> None:
    class _Done:
        stdout = "Google Chrome 154.0.8037.58 \n"

    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(browser.sys, "platform", "darwin")

    user_agent = browser.headless_user_agent("chrome")

    assert user_agent == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    )
    assert "Headless" not in user_agent


def test_headless_user_agent_refuses_to_guess_an_unreadable_version(monkeypatch) -> None:
    class _Done:
        stdout = ""

    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: _Done())

    with pytest.raises(RuntimeError, match="no-headless"):
        browser.headless_user_agent("chrome")


def test_find_chrome_honours_the_override_and_rejects_a_missing_file(
    monkeypatch, tmp_path
) -> None:
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("KTXGO_CHROME", str(fake))
    assert browser.find_chrome() == str(fake)

    monkeypatch.setenv("KTXGO_CHROME", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="missing"):
        browser.find_chrome()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class _LoginPage:
    """Stands in for the login page: answers the few probes the login makes."""

    def __init__(
        self,
        *,
        form: bool = True,
        paths: list[str] | None = None,
        notice: str = "",
        typed_ok: bool = True,
    ) -> None:
        self.form = form
        self.paths = list(paths or ["/ticket/login"])
        self.last_path = self.paths[0]
        self.notice = notice
        self.typed_ok = typed_ok
        self.typed: list[str] = []

    def goto(self, url: str, **_: Any) -> None:
        pass

    def wait_for_selector(self, selector: str, **_: Any) -> bool:
        return self.form

    def click(self, selector: str) -> bool:
        return True

    def type_text(self, text: str, **_: Any) -> None:
        self.typed.append(text)

    def evaluate(self, source: str, *args: Any, **_: Any) -> Any:
        if source == "location.pathname":
            self.last_path = self.paths.pop(0) if len(self.paths) > 1 else self.paths[0]
            return self.last_path
        if source == korail.NOTICE_TEXT_JS:
            return self.notice
        if source.startswith("[document.querySelector"):
            return [10, 8] if self.typed_ok else [0, 0]
        raise AssertionError(f"unexpected script: {source}")


def _login(monkeypatch, page: _LoginPage, *, session_ok: Any) -> KorailAPI:
    """`session_ok` is a bool, or a callable evaluated when the session is checked."""
    monkeypatch.setattr(korail.time, "sleep", lambda _s: None)
    api = KorailAPI(page)  # type: ignore[arg-type]
    check = session_ok if callable(session_ok) else (lambda: session_ok)
    monkeypatch.setattr(api, "wait_for_login_stable", lambda **_: check())
    return api


def test_login_succeeds_once_the_form_redirects_and_the_session_holds(monkeypatch) -> None:
    page = _LoginPage(paths=["/ticket/login", "/ticket/main"])
    api = _login(monkeypatch, page, session_ok=True)

    assert api.login_with_credentials("1234567890", "password") is True
    assert page.typed == ["1234567890", "password"]
    assert api.last_auto_login_error is None


def test_login_treats_the_generic_communication_error_as_a_block(monkeypatch) -> None:
    page = _LoginPage(notice="안내 메세지 통신 중 에러가 발생하였습니다. 잠시 후 다시 이용바랍니다.")
    api = _login(monkeypatch, page, session_ok=False)

    with pytest.raises(MacroBlockedError):
        api.login_with_credentials("1234567890", "password")


def test_login_reports_refused_credentials_apart_from_a_block(monkeypatch) -> None:
    page = _LoginPage(notice="안내 메세지 잘못 입력하셨습니다(회원번호) 확인")
    api = _login(monkeypatch, page, session_ok=False)

    with pytest.raises(BadCredentialsError, match="회원번호"):
        api.login_with_credentials("1234567890", "password")


def test_login_recognises_the_november_wording_for_an_unknown_member_number(monkeypatch) -> None:
    page = _LoginPage(notice="안내 메세지 등록되지 않는 회원번호입니다. 회원번호를 확인해 주십시오. 확인")
    api = _login(monkeypatch, page, session_ok=False)

    # A short limit so a regression fails here instead of waiting out the default.
    with pytest.raises(BadCredentialsError, match="등록되지 않는"):
        api.login_with_credentials("1234567890", "password", timeout_s=1.0)


def test_login_trusts_the_session_over_a_reminder_notice(monkeypatch) -> None:
    # Korail pops a password-change reminder up on a successful login; the
    # text alone must not be read as a failure.
    page = _LoginPage(notice="비밀변경 안내 비밀번호를 변경하시겠습니까?")
    api = _login(monkeypatch, page, session_ok=True)

    assert api.login_with_credentials("1234567890", "password", timeout_s=0.05) is True


def test_an_unknown_notice_does_not_cut_the_wait_short(monkeypatch) -> None:
    # The reminder shows up while the redirect is still on its way; only a
    # known refusal may end the wait before the page leaves the login route.
    page = _LoginPage(
        paths=["/ticket/login", "/ticket/login", "/ticket/main"],
        notice="비밀변경 안내 비밀번호를 변경하시겠습니까?",
    )
    api = _login(
        monkeypatch,
        page,
        session_ok=lambda: not page.last_path.startswith("/ticket/login"),
    )

    assert api.login_with_credentials("1234567890", "password") is True


def test_a_reminder_notice_alone_is_not_a_credentials_refusal(monkeypatch) -> None:
    # Same reminder as above, but the session check has not caught up yet.
    page = _LoginPage(notice="비밀변경 안내 비밀번호를 변경하시겠습니까?")
    api = _login(monkeypatch, page, session_ok=False)

    assert api.login_with_credentials("1234567890", "password", timeout_s=0.05) is False
    assert api.last_auto_login_error == "login_notice"


def test_login_reports_a_message_it_does_not_know_apart_from_a_plain_failure(monkeypatch) -> None:
    page = _LoginPage(notice="점검 중입니다")
    api = _login(monkeypatch, page, session_ok=False)

    assert api.login_with_credentials("1234567890", "password", timeout_s=0.05) is False
    assert api.last_auto_login_error == "login_notice"
    assert api.last_auto_login_detail == "점검 중입니다"


def test_login_without_any_message_is_an_unconfirmed_login(monkeypatch) -> None:
    api = _login(monkeypatch, _LoginPage(), session_ok=False)

    assert api.login_with_credentials("1234567890", "password", timeout_s=0.05) is False
    assert api.last_auto_login_error == "login_check_failed"


def test_login_stops_when_the_form_is_missing_or_typing_did_not_land(monkeypatch) -> None:
    api = _login(monkeypatch, _LoginPage(form=False), session_ok=True)
    assert api.login_with_credentials("1234567890", "password") is False
    assert api.last_auto_login_error == "login_form_not_found"

    api = _login(monkeypatch, _LoginPage(typed_ok=False), session_ok=True)
    assert api.login_with_credentials("1234567890", "password") is False
    assert api.last_auto_login_error == "fill_failed"


def test_login_needs_both_credentials(monkeypatch) -> None:
    api = _login(monkeypatch, _LoginPage(), session_ok=True)

    assert api.login_with_credentials("", "password") is False
    assert api.last_auto_login_error == "missing_credentials"


# ---------------------------------------------------------------------------
# Automatic re-login guards
# ---------------------------------------------------------------------------


class _Manager:
    _headless = True
    _fresh_session = False
    page = object()

    def close(self) -> None:
        pass

    def start(self) -> None:
        pass

    def save_cookies(self) -> None:
        pass


def _reset_guards() -> None:
    cli._auto_login_attempts.clear()
    cli._reset_auto_login_failures()
    cli._auto_login_halted = False


def test_refused_credentials_stop_further_automatic_logins(monkeypatch) -> None:
    _reset_guards()
    said: list[str] = []

    class _Refusing:
        last_auto_login_detail = None
        last_auto_login_error = None

        def __init__(self, page: object) -> None:
            pass

        def login_with_credentials(self, *_: Any, **__: Any) -> bool:
            raise BadCredentialsError("잘못 입력하셨습니다(비밀번호)")

    monkeypatch.setattr(cli, "KorailAPI", _Refusing)
    monkeypatch.setattr(cli.click, "echo", lambda message="": said.append(str(message)))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    assert cli._attempt_auto_login(_Manager(), ("id", "pw"), headless=True) is None

    assert cli._auto_login_halted is True
    assert any("거부" in line for line in said)
    # A refusal is not a transport failure, so it must not feed the backoff.
    assert cli._auto_login_backoff_delay() == 0.0
    _reset_guards()


def test_ensure_login_skips_automatic_login_once_credentials_were_refused(monkeypatch) -> None:
    _reset_guards()
    cli._auto_login_halted = True
    attempts: list[int] = []

    class _LoggedOut:
        def wait_for_login_stable(self, **_: Any) -> bool:
            return False

    class _NoTTY:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("id", "pw"))
    monkeypatch.setattr(cli, "_attempt_auto_login", lambda *a, **k: attempts.append(1))
    monkeypatch.setattr(cli.sys, "stdin", _NoTTY())
    monkeypatch.setattr(cli.click, "echo", lambda message="": None)

    with pytest.raises(SystemExit):
        cli._ensure_login(_LoggedOut(), _Manager(), headless=True)  # type: ignore[arg-type]

    assert attempts == []
    _reset_guards()


def test_a_login_message_halts_automatic_logins_without_feeding_the_backoff(monkeypatch) -> None:
    _reset_guards()

    class _Noticed:
        last_auto_login_error = "login_notice"
        last_auto_login_detail = "계정이 잠겼습니다"

        def __init__(self, page: object) -> None:
            pass

        def login_with_credentials(self, *_: Any, **__: Any) -> bool:
            return False

    monkeypatch.setattr(cli, "KorailAPI", _Noticed)
    monkeypatch.setattr(cli.click, "echo", lambda message="": None)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    assert cli._attempt_auto_login(_Manager(), ("id", "pw"), headless=True) is None

    assert cli._auto_login_halted is True
    assert cli._auto_login_backoff_delay() == 0.0
    _reset_guards()


def test_http_5xx_gets_a_hint_and_other_codes_do_not() -> None:
    assert "HTTP 5xx" in (cli._http_error_hint("HTTP_500") or "")
    assert cli._http_error_hint("P058") is None
    assert cli._http_error_hint("") is None


def test_a_restored_cookie_backup_is_private(monkeypatch, tmp_path) -> None:
    cookie = tmp_path / "cookies.json"
    cookie.write_text('[{"name": "a"}]')

    class _Failing:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> "_Failing":
            raise RuntimeError("chrome is missing")

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(cli, "COOKIE_PATH", cookie)
    monkeypatch.setattr(cli, "BrowserManager", _Failing)
    monkeypatch.setattr(cli.click, "echo", lambda message="": None)

    previous = os.umask(0o022)
    try:
        assert cli._login_and_save_session(force_relogin=True) is False
    finally:
        os.umask(previous)

    assert cookie.read_text() == '[{"name": "a"}]'
    assert stat.S_IMODE(cookie.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Waiting for the user, proxies, process cleanup
# ---------------------------------------------------------------------------


class _Terminal:
    @staticmethod
    def isatty() -> bool:
        return True


def _no_read() -> str:
    raise AssertionError("must not block on input()")


def test_wait_for_enter_gives_up_when_the_time_limit_passes(monkeypatch) -> None:
    monkeypatch.setattr(korail.sys, "stdin", _Terminal())
    monkeypatch.setattr(korail.select, "select", lambda *_: ([], [], []))
    monkeypatch.setattr("builtins.input", _no_read)

    assert korail._wait_for_enter(0.1) is False


def test_wait_for_enter_returns_once_a_line_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(korail.sys, "stdin", _Terminal())
    monkeypatch.setattr(korail.select, "select", lambda *_: ([object()], [], []))
    monkeypatch.setattr("builtins.input", lambda: "")

    assert korail._wait_for_enter(5) is True


def test_wait_for_enter_reads_plainly_when_there_is_no_terminal(monkeypatch) -> None:
    class _Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(korail.sys, "stdin", _Pipe())
    monkeypatch.setattr("builtins.input", lambda: "")

    assert korail._wait_for_enter(0.0) is True


def test_devtools_requests_bypass_proxies_from_the_environment(monkeypatch) -> None:
    import http.server
    import socket

    class _DevTools(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"webSocketDebuggerUrl": "ws://devtools"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(5)
    proxy.settimeout(0.5)
    target = http.server.HTTPServer(("127.0.0.1", 0), _DevTools)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    for key in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(key, f"http://127.0.0.1:{proxy.getsockname()[1]}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    url = f"http://127.0.0.1:{target.server_address[1]}/json/version"

    try:
        with browser._LOCAL.open(url, timeout=3) as response:
            assert json.load(response)["webSocketDebuggerUrl"] == "ws://devtools"
        with pytest.raises(socket.timeout):
            proxy.accept()  # nothing reached the proxy

        # The same request through a stock urlopen is caught by that proxy,
        # which is what the opener above exists to prevent.
        with pytest.raises(Exception):
            urllib.request.urlopen(url, timeout=1)
    finally:
        target.shutdown()
        proxy.close()


def test_the_websocket_is_opened_without_a_proxy(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Ws:
        def __enter__(self) -> "_Ws":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def fake_connect(url: str, **kwargs: Any) -> _Ws:
        seen.update(kwargs)
        return _Ws()

    monkeypatch.setattr(cdp, "connect", fake_connect)
    cdp.CdpConnection("ws://127.0.0.1:1")

    assert "proxy" in seen and seen["proxy"] is None


class _Process:
    def __init__(self, *, alive: bool, stubborn: bool = False) -> None:
        self.alive = alive
        self.stubborn = stubborn
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.calls.append("terminate")
        if not self.stubborn:
            self.alive = False

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        if self.alive:
            self.calls.append("wait-timeout")
            raise subprocess.TimeoutExpired("chrome", timeout or 0)
        self.calls.append("wait")
        return 0


def test_stop_reaps_a_process_that_already_exited() -> None:
    process = _Process(alive=False)
    browser.BrowserManager._stop(process)  # type: ignore[arg-type]
    assert process.calls == ["wait"]


def test_stop_escalates_to_kill_and_still_waits() -> None:
    process = _Process(alive=True, stubborn=True)
    browser.BrowserManager._stop(process)  # type: ignore[arg-type]
    assert process.calls == ["terminate", "wait-timeout", "kill", "wait"]
