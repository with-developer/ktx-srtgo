"""Minimal Chrome DevTools Protocol client.

Playwright drives Chrome over CDP too, but it switches on a wide set of
domains (Runtime, Network, Page, ...). Korail's DynaPath anti-macro filter
notices: with Playwright attached, every DynaPath-wrapped request — the login
and the site's own train search alike — is answered with HTTP 500, while the
same Chrome driven with nothing but Page.navigate, Runtime.evaluate and
Input.* is accepted. So this client sends only the commands it needs and
never any "*.enable".
"""

from __future__ import annotations

import json
import random
import time
from contextlib import ExitStack
from typing import Any, Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

_MISSING: Final = object()


class CdpError(RuntimeError):
    pass


class CdpConnection:
    def __init__(self, ws_url: str, *, timeout_s: float = 30.0) -> None:
        # websockets 16+ wants connect() used as a context manager; an
        # ExitStack keeps that working on older releases as well.
        self._stack = ExitStack()
        try:
            # proxy=None: this is a loopback socket to our own Chrome, and a
            # proxy from the environment would only get in the way.
            self._ws: ClientConnection = self._stack.enter_context(
                connect(
                    ws_url,
                    max_size=None,
                    open_timeout=10,
                    ping_interval=None,
                    proxy=None,
                )
            )
        except BaseException:
            self._stack.close()
            raise
        self._timeout_s: float = timeout_s
        self._next_id: int = 0

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        call_id = self._next_id
        payload = json.dumps({"id": call_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + (timeout_s or self._timeout_s)
        try:
            self._ws.send(payload)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdpError(f"CDP {method} timed out")
                try:
                    raw = self._ws.recv(timeout=remaining)
                except TimeoutError as exc:
                    raise CdpError(f"CDP {method} timed out") from exc
                message = cast_dict(json.loads(raw))
                if message.get("id") != call_id:
                    if message.get("method") == "Inspector.targetCrashed":
                        raise CdpError("The browser tab crashed")
                    continue
                error = message.get("error")
                if isinstance(error, dict):
                    raise CdpError(f"CDP {method} failed: {error.get('message', error)}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        except ConnectionClosed as exc:
            raise CdpError(f"CDP {method}: the browser closed the connection") from exc

    def close(self) -> None:
        try:
            self._stack.close()
        except Exception:
            pass


def cast_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class CdpPage:
    """One browser tab, driven through the smallest useful command set."""

    def __init__(self, connection: CdpConnection) -> None:
        self._conn: CdpConnection = connection

    def evaluate(self, source: str, arg: Any = _MISSING, *, timeout_s: float = 60.0) -> Any:
        """Run JavaScript in the page and return its JSON-able result.

        Without `arg`, `source` is evaluated as an expression. With `arg`, it
        must be a function expression and is called with `arg` (as Playwright's
        page.evaluate does); returned promises are awaited.
        """
        expression = (
            source
            if arg is _MISSING
            else f"({source})({json.dumps(arg, ensure_ascii=False)})"
        )
        result = self._conn.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout_s=timeout_s,
        )
        details = result.get("exceptionDetails")
        if isinstance(details, dict):
            exception = cast_dict(details.get("exception"))
            text = exception.get("description") or details.get("text") or "evaluation failed"
            raise CdpError(f"Page script failed: {text}")
        return cast_dict(result.get("result")).get("value")

    def goto(self, url: str, *, timeout_s: float = 60.0) -> None:
        # Page.navigate can return while the old document is still current, so
        # plant a marker and wait for it to disappear with the old document.
        self.evaluate("window.__ktxgoNav = 1")
        result = self._conn.call("Page.navigate", {"url": url}, timeout_s=timeout_s)
        error_text = result.get("errorText")
        if error_text:
            raise CdpError(f"Navigation to {url} failed: {error_text}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = self.evaluate(
                    "[window.__ktxgoNav === undefined, document.readyState]"
                )
            except CdpError:
                state = None  # the document is being swapped out
            if (
                isinstance(state, list)
                and state[0] is True
                and state[1] in ("interactive", "complete")
            ):
                return
            time.sleep(0.15)
        raise CdpError(f"Timed out loading {url}")

    @property
    def url(self) -> str:
        return str(self.evaluate("location.href") or "")

    def wait_for_selector(
        self, selector: str, *, timeout_s: float = 15.0, visible: bool = True
    ) -> bool:
        probe = (
            "(() => { const e = document.querySelector(%s); if (!e) return false;"
            " if (!%s) return true; const r = e.getBoundingClientRect();"
            " return r.width > 0 && r.height > 0; })()"
        ) % (json.dumps(selector), "true" if visible else "false")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.evaluate(probe):
                    return True
            except CdpError:
                pass
            time.sleep(0.3)
        return False

    def click(self, selector: str) -> bool:
        """Click the element's centre; False if it is missing or covered.

        A notice popup can sit on top of the page, and a real click would then
        land on the popup while the caller believes it pressed the button.
        """
        point = self.evaluate(
            "(() => { const e = document.querySelector(%s); if (!e) return null;"
            " e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();"
            " if (!r.width || !r.height) return null;"
            " const x = r.x + r.width / 2, y = r.y + r.height / 2;"
            " const top = document.elementFromPoint(x, y);"
            " return {x, y, hit: !!top && (e === top || e.contains(top))}; })()"
            % json.dumps(selector)
        )
        if not isinstance(point, dict) or not point.get("hit"):
            return False
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            self._conn.call(
                "Input.dispatchMouseEvent",
                {
                    "type": event,
                    "x": point["x"],
                    "y": point["y"],
                    "button": "left",
                    "clickCount": 0 if event == "mouseMoved" else 1,
                },
            )
            time.sleep(random.uniform(0.03, 0.09))
        return True

    def type_text(self, text: str, *, delay_s: tuple[float, float] = (0.05, 0.11)) -> None:
        """Type into whatever is focused, one key at a time like a person."""
        for char in text:
            if char.isascii() and char.isprintable():
                self._conn.call(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", "text": char, "key": char, "unmodifiedText": char},
                )
                self._conn.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
            else:
                self._conn.call("Input.insertText", {"text": char})
            time.sleep(random.uniform(*delay_s))

    def get_cookies(self) -> list[dict[str, Any]]:
        cookies = self._conn.call("Storage.getCookies").get("cookies")
        return [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if cookies:
            self._conn.call("Storage.setCookies", {"cookies": cookies})

    def close(self) -> None:
        self._conn.close()
