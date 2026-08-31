from __future__ import annotations

from datetime import datetime, timedelta

import click
import pytest
from click.core import ParameterSource
from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import KorailAPI, KorailError, MacroBlockedError, Train


# Saved-date defaults are discarded once the date is in the past, so tests that
# expect a stored date to survive must use one that is still ahead of today.
FUTURE_DATE = (datetime.now() + timedelta(days=10)).strftime("%Y%m%d")


class _DummyManager:
    def __init__(self, headless: bool):
        self.page = object()

    def __enter__(self) -> _DummyManager:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _make_waitlist_train() -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": "00123",
            "h_car_tp_nm": "KTX",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_nm": "KTX",
            "h_dpt_rs_stn_nm": "서울",
            "h_arv_rs_stn_nm": "부산",
            "h_dpt_tm_qb": "08:10",
            "h_arv_tm_qb": "10:59",
            "h_dpt_dt": "20260320",
            "h_gen_rsv_nm": "매진",
            "h_gen_rsv_cd": "13",
            "h_spe_rsv_nm": "매진",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_nm": "없음",
            "h_wait_rsv_nm": "가능",
            "h_wait_rsv_cd": "09",
            "h_rcvd_amt": "0059800",
            "h_trn_clsf_cd": "100",
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_run_dt": "20260320",
        }
    )


def _patch_cli_runtime(monkeypatch) -> None:
    monkeypatch.setattr(cli, "BrowserManager", _DummyManager)
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless: api)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)


def _set_parameter_source(
    ctx: click.Context, param_name: str, source: ParameterSource
) -> None:
    if hasattr(ctx, "set_parameter_source"):
        ctx.set_parameter_source(param_name, source)
        return
    ctx._parameter_source[param_name] = source  # type: ignore[attr-defined]


def _patch_store_reads(monkeypatch, values: dict[str, str]) -> None:
    """Route both preference and secret reads at one in-memory mapping."""
    monkeypatch.setattr(cli.store, "get_pref", lambda key: values.get(key))
    monkeypatch.setattr(cli.store, "get_secret", lambda key: values.get(key))


def _patch_store_writes(monkeypatch, record) -> None:
    """Route both preference and secret writes at one recorder."""
    monkeypatch.setattr(cli.store, "set_pref", record)
    monkeypatch.setattr(cli.store, "set_secret", record)


def test_set_waitlist_alert_uses_korail_wait_endpoint() -> None:
    api = KorailAPI.__new__(KorailAPI)
    captured: dict[str, object] = {}

    def fake_api_call(endpoint: str, params: dict[str, str]) -> dict[str, object]:
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"strResult": "SUCC"}

    api._api_call = fake_api_call  # type: ignore[attr-defined]

    api.set_waitlist_alert("PNR123", "01012341234")

    assert captured["endpoint"] == "/classes/com.korail.mobile.reservationWait.ReservationWait"
    assert captured["params"] == {
        "Device": "AD",
        "Version": "250601002",
        "Key": "korail1234567890",
        "txtPnrNo": "PNR123",
        "txtPsrmClChgFlg": "N",
        "txtSmsSndFlg": "Y",
        "txtCpNo": "01012341234",
    }


def test_resolve_waitlist_alert_phone_prefers_cli_over_keyring(monkeypatch) -> None:
    _patch_store_reads(
        monkeypatch,
        {
            "waitlist_alert_phone": "01099998888",
        },
    )

    assert cli._resolve_waitlist_alert_phone("01012341234") == "01012341234"


def test_set_waitlist_alert_phone_interactive_saves_normalized_phone(
    monkeypatch,
) -> None:
    stored: dict[str, str] = {}

    monkeypatch.setattr(
        cli,
        "_prompt_guarded",
        lambda questions: {"phone": "010-1234-5678"},
    )
    _patch_store_reads(
        monkeypatch,
        {
            "waitlist_alert_phone": "01000000000",
        },
    )
    _patch_store_writes(monkeypatch, lambda key, value: stored.__setitem__(key, value))

    assert cli._set_waitlist_alert_phone_interactive() is True
    assert stored == {"waitlist_alert_phone": "01012345678"}


def test_interactive_menu_dispatches_waitlist_alert_setting(monkeypatch) -> None:
    actions = iter(["waitlist-alert", "exit"])
    calls: list[str] = []

    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: next(actions))
    monkeypatch.setattr(
        cli,
        "_set_waitlist_alert_phone_interactive",
        lambda: calls.append("waitlist-alert") or True,
    )
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main.callback(
            departure="서울",
            arrival="부산",
            date="20260320",
            time_str="07",
            adults=1,
            headless=True,
            interactive=True,
            max_attempts=1,
            train_types=("ktx",),
            seat="any",
            set_card_mode=False,
            auto_pay=False,
            smart_ticket=True,
            telegram=False,
            waitlist_alert_phone=None,
        )

    assert exc_info.value.code == 0
    assert calls == ["waitlist-alert"]


def test_cli_registers_waitlist_alert_after_waitlist_success(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return [_make_waitlist_train()]

        def reserve(
            self,
            train: Train,
            seat_type: str = "general",
            adults: int = 1,
            waitlist: bool = False,
        ) -> dict[str, object]:
            del train, seat_type, adults
            assert waitlist is True
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no: str, phone: str) -> dict[str, object]:
            calls.append((pnr_no, phone))
            return {"strResult": "SUCC"}

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--waitlist-alert-phone",
            "01012341234",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("PNR123", "01012341234")]
    assert "좌석배정 알림 등록완료" in result.output


def test_cli_keeps_waitlist_success_when_alert_registration_fails(monkeypatch) -> None:
    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return [_make_waitlist_train()]

        def reserve(
            self,
            train: Train,
            seat_type: str = "general",
            adults: int = 1,
            waitlist: bool = False,
        ) -> dict[str, object]:
            del train, seat_type, adults
            assert waitlist is True
            return {"h_pnr_no": "PNR123", "strResult": "SUCC"}

        def set_waitlist_alert(self, pnr_no: str, phone: str) -> dict[str, object]:
            del pnr_no, phone
            raise KorailError("alert registration failed", "ERR")

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--no-interactive",
            "--max-attempts",
            "1",
            "--waitlist-alert-phone",
            "01012341234",
        ],
    )

    assert result.exit_code == 0
    assert "예약대기 신청완료" in result.output
    assert "alert registration failed" in result.output


def test_load_saved_interactive_defaults_sanitizes_invalid_values(monkeypatch) -> None:
    _patch_store_reads(
        monkeypatch,
        {
            "departure": "없는역",
            "arrival": "서울",
            "date": "2026-03-20",
            "time": "99",
            "adults": "0",
            "train_types": "invalid-type",
            "seat": "invalid-seat",
            "auto_pay": "maybe",
            "smart_ticket": "off",
        },
    )

    defaults = cli._load_saved_interactive_reservation_defaults(
        stations=["서울", "부산", "대전"],
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert defaults == (
        "서울",
        "부산",
        "20260320",
        "07",
        1,
        ("ktx",),
        "any",
        False,
        True,
    )


def test_apply_saved_interactive_defaults_preserves_explicit_cli_sources(
    monkeypatch,
) -> None:
    _patch_store_reads(
        monkeypatch,
        {
            "departure": "대전",
            "arrival": "부산",
            "date": FUTURE_DATE,
            "time": "13",
            "adults": "2",
            "train_types": "legacy-all",
            "seat": "special",
            "auto_pay": "1",
            "smart_ticket": "0",
        },
    )
    ctx = click.Context(cli.main)
    _set_parameter_source(ctx, "departure", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "arrival", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "date", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "time_str", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "adults", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "train_types", ParameterSource.DEFAULT)
    _set_parameter_source(ctx, "seat", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "auto_pay", ParameterSource.COMMANDLINE)
    _set_parameter_source(ctx, "smart_ticket", ParameterSource.DEFAULT)

    merged = cli._apply_saved_interactive_reservation_defaults(
        ctx,
        stations=["서울", "대전", "부산"],
        departure="서울",
        arrival="광명",
        date="20260319",
        time_str="08",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert merged == (
        "대전",
        "광명",
        FUTURE_DATE,
        "13",
        1,
        ("ktx", "itx-saemaeul", "mugunghwa", "tonggeun", "itx-cheongchun", "itx-maeum", "airport"),
        "any",
        False,
        True,
    )


def test_apply_saved_interactive_defaults_keeps_default_map_values(
    monkeypatch,
) -> None:
    _patch_store_reads(
        monkeypatch,
        {
            "departure": "대전",
            "arrival": "부산",
            "date": "20260320",
            "time": "13",
            "adults": "2",
            "train_types": "legacy-all",
            "seat": "special",
            "auto_pay": "1",
            "smart_ticket": "0",
        },
    )
    ctx = click.Context(cli.main)
    for param_name in (
        "departure",
        "arrival",
        "date",
        "time_str",
        "adults",
        "train_types",
        "seat",
        "auto_pay",
        "smart_ticket",
    ):
        _set_parameter_source(ctx, param_name, ParameterSource.DEFAULT_MAP)

    merged = cli._apply_saved_interactive_reservation_defaults(
        ctx,
        stations=["서울", "대전", "부산"],
        departure="서울",
        arrival="광명",
        date="20260319",
        time_str="08",
        adults=1,
        train_types=("ktx",),
        seat="any",
        auto_pay=False,
        smart_ticket=True,
    )

    assert merged == (
        "서울",
        "광명",
        "20260319",
        "08",
        1,
        ("ktx",),
        "any",
        False,
        True,
    )


def test_prompt_conditions_persists_partial_progress_before_cancellation(
    monkeypatch,
) -> None:
    answers = iter(
        [
            {"departure": "서울"},
            {"arrival": "부산"},
            None,
        ]
    )
    stored: list[tuple[str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    _patch_store_writes(monkeypatch, lambda key, value: stored.append((key, value)))

    with pytest.raises(SystemExit) as exc_info:
        cli._prompt_conditions(
            departure="서울",
            arrival="대전",
            date="20260320",
            time_str="07",
            adults=1,
            stations=["서울", "부산", "대전"],
            train_types=("ktx",),
        )

    assert exc_info.value.code == 0
    assert stored == [
        ("departure", "서울"),
        ("arrival", "부산"),
    ]


def test_prompt_reservation_options_persists_reservation_defaults(monkeypatch) -> None:
    answers = iter([
        {"seat": "special"},
        {"auto_pay": True},
    ])
    stored: list[tuple[str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    _patch_store_writes(monkeypatch, lambda key, value: stored.append((key, value)))

    result = cli._prompt_reservation_options("any", False, False)

    assert result == ("special", True, False)
    assert stored == [
        ("seat", "special"),
        ("auto_pay", "1"),
    ]


def test_prompt_reservation_options_persists_partial_progress_on_cancellation(
    monkeypatch,
) -> None:
    answers = iter([
        {"seat": "general"},
        None,
    ])
    stored: list[tuple[str, str]] = []

    monkeypatch.setattr(cli, "_prompt_guarded", lambda questions: next(answers))
    _patch_store_writes(monkeypatch, lambda key, value: stored.append((key, value)))

    with pytest.raises(SystemExit) as exc_info:
        cli._prompt_reservation_options("any", False, True)

    assert exc_info.value.code == 0
    assert stored == [
        ("seat", "general"),
    ]


def test_main_persists_auto_pay_false_after_card_check_fallback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "configure_keyring_backend", lambda: None)
    monkeypatch.setattr(cli, "_load_visible_stations", lambda: ["서울", "부산"])
    monkeypatch.setattr(cli, "_prompt_main_menu", lambda: "reserve")
    monkeypatch.setattr(
        cli,
        "_prompt_conditions",
        lambda departure, arrival, date, time_str, adults, stations, train_types: (
            departure,
            arrival,
            date,
            time_str,
            adults,
            train_types,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_prompt_target_trains",
        lambda api, departure, arrival, date, time_str, adults, train_types: [
            (date, "00123", "0810", departure, arrival)
        ],
    )
    monkeypatch.setattr(
        cli,
        "_prompt_reservation_options",
        lambda seat, auto_pay, smart_ticket: ("any", True, smart_ticket),
    )
    monkeypatch.setattr(cli, "_ensure_card_for_auto_pay", lambda: False)
    monkeypatch.setattr(cli.click, "confirm", lambda message, default=True: True)

    stored: list[tuple[str, str]] = []
    _patch_store_writes(monkeypatch, lambda key, value: stored.append((key, value)))

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return []

    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        type("DummyStdin", (), {"isatty": lambda self: True})(),
    )

    cli.main.callback(
        departure="서울",
        arrival="부산",
        date="20260320",
        time_str="07",
        adults=1,
        headless=True,
        interactive=True,
        max_attempts=1,
        train_types=("ktx",),
        seat="any",
        set_card_mode=False,
        auto_pay=False,
        smart_ticket=True,
        telegram=False,
        waitlist_alert_phone=None,
    )

    assert ("auto_pay", "0") in stored


class _LoginDummyManager:
    """Manager stub for _ensure_login tests."""

    _headless = True
    _fresh_session = False
    page = object()

    def __init__(self, sink: list[str]):
        self._sink = sink

    def close(self) -> None:
        pass

    def start(self) -> None:
        pass

    def save_cookies(self) -> None:
        self._sink.append("saved")


class _LoggedOutAPI:
    def wait_for_login_stable(self, **kwargs) -> bool:
        return False

    def login_manual(self, *args, **kwargs) -> bool:
        raise AssertionError("manual login must not run in this scenario")


def _prepare_auto_login_state() -> None:
    cli._auto_login_attempts.clear()
    cli._reset_auto_login_failures()


def test_ensure_login_prefers_automatic_login(monkeypatch) -> None:
    messages: list[str] = []
    auto_api = object()
    _prepare_auto_login_state()

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("member1234", "secret"))
    monkeypatch.setattr(
        cli, "_attempt_auto_login", lambda manager, creds, headless: auto_api
    )
    monkeypatch.setattr(cli.click, "echo", lambda message="": messages.append(str(message)))

    result = cli._ensure_login(_LoggedOutAPI(), _LoginDummyManager(messages), headless=True)

    assert result is auto_api
    assert "saved" in messages


def test_ensure_login_stops_when_macro_blocked(monkeypatch) -> None:
    messages: list[str] = []
    _prepare_auto_login_state()

    def _blocked(manager, creds, headless):
        raise MacroBlockedError("blocked", "macro_err1")

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("member1234", "secret"))
    monkeypatch.setattr(cli, "_attempt_auto_login", _blocked)
    monkeypatch.setattr(cli, "colored", lambda text, *args, **kwargs: text)
    monkeypatch.setattr(cli.click, "echo", lambda message="": messages.append(str(message)))

    with pytest.raises(SystemExit) as exc_info:
        cli._ensure_login(_LoggedOutAPI(), _LoginDummyManager(messages), headless=True)

    assert exc_info.value.code == 1
    assert any("안티매크로 차단 감지" in message for message in messages)
    # A block must never be answered with another login attempt.
    assert "saved" not in messages


def test_ensure_login_stops_once_auto_login_budget_is_spent(monkeypatch) -> None:
    messages: list[str] = []
    attempts: list[int] = []
    _prepare_auto_login_state()
    cli._auto_login_attempts.extend(
        [cli.time.monotonic()] * cli._AUTO_LOGIN_MAX_IN_WINDOW
    )

    class _NoTTY:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(cli, "_load_login_credentials", lambda: ("member1234", "secret"))
    monkeypatch.setattr(
        cli,
        "_attempt_auto_login",
        lambda manager, creds, headless: attempts.append(1),
    )
    monkeypatch.setattr(cli.sys, "stdin", _NoTTY())
    monkeypatch.setattr(cli.click, "echo", lambda message="": messages.append(str(message)))

    with pytest.raises(SystemExit) as exc_info:
        cli._ensure_login(_LoggedOutAPI(), _LoginDummyManager(messages), headless=True)

    assert exc_info.value.code == 1
    assert attempts == []
    assert any("자동 로그인 한도 초과" in message for message in messages)


def test_auto_login_backoff_grows_with_consecutive_failures() -> None:
    _prepare_auto_login_state()
    assert cli._auto_login_backoff_delay() == 0.0
    delays = []
    for failures in range(1, len(cli._AUTO_LOGIN_BACKOFF_S) + 2):
        cli._auto_login_failures = failures
        delays.append(cli._auto_login_backoff_delay())
    assert delays == sorted(delays)
    assert delays[-1] == cli._AUTO_LOGIN_BACKOFF_S[-1]
    _prepare_auto_login_state()
