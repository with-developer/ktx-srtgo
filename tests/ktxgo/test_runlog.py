from __future__ import annotations

import re
import sys

from click.testing import CliRunner
from termcolor import colored

from ktxgo import cli, runlog
from ktxgo.korail import MacroBlockedError, Train

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def _log_lines(path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        assert _STAMP.match(line), line
    return [_STAMP.sub("", line) for line in lines]


def test_copies_output_without_colour_codes_or_screen_clears(capsys) -> None:
    path = runlog.start()
    assert path is not None

    print(colored("안티매크로 차단 감지", "white", "on_red", force_color=True))
    sys.stdout.write("\033[2J\033[1;1H")
    print("다음 줄")
    print("오류 메시지", file=sys.stderr)

    out = capsys.readouterr().out
    assert "\033[" in out  # the screen still gets colours and clears
    assert _log_lines(path)[1:] == ["안티매크로 차단 감지", "다음 줄", "오류 메시지"]


def test_paused_output_and_notes_stay_off_the_screen_or_out_of_the_log(capsys) -> None:
    path = runlog.start()
    assert path is not None

    with runlog.paused():
        print("메뉴 다시 그리기")
    with runlog.paused(False):
        print("표가 바뀜")
    runlog.note("조회 요청 #1")

    out = capsys.readouterr().out
    assert "메뉴 다시 그리기" in out
    assert "조회 요청" not in out
    assert _log_lines(path)[1:] == ["표가 바뀜", "조회 요청 #1"]


def test_flushed_prompt_is_its_own_line() -> None:
    path = runlog.start()
    assert path is not None

    print("Enter를 눌러주세요 ", end="", flush=True)
    print("로그인이 확인되지 않았습니다.")

    assert _log_lines(path)[1:] == ["Enter를 눌러주세요", "로그인이 확인되지 않았습니다."]


def _sold_out_train() -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": "00426",
            "h_car_tp_nm": "KTX",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_nm": "KTX",
            "h_dpt_rs_stn_nm": "나주",
            "h_arv_rs_stn_nm": "용산",
            "h_dpt_tm_qb": "14:46",
            "h_arv_tm_qb": "17:03",
            "h_dpt_dt": "20261115",
            "h_gen_rsv_nm": "매진",
            "h_gen_rsv_cd": "13",
            "h_spe_rsv_nm": "매진",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_nm": "매진",
            "h_wait_rsv_nm": "불가",
            "h_wait_rsv_cd": "13",
            "h_rcvd_amt": "0043100",
            "h_trn_clsf_cd": "100",
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_run_dt": "20261115",
        }
    )


def test_search_loop_logs_each_attempt_but_an_unchanged_table_once(monkeypatch) -> None:
    results = iter([[_sold_out_train()], [_sold_out_train()]])

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            try:
                return next(results)
            except StopIteration:
                raise MacroBlockedError("비정상적인 요청으로 감지되어 차단되었습니다.")

    class DummyManager:
        def __init__(self, headless: bool):
            self.page = object()

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(cli, "BrowserManager", DummyManager)
    monkeypatch.setattr(cli, "KorailAPI", DummyAPI)
    monkeypatch.setattr(cli, "_ensure_login", lambda api, manager, headless: api)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args, **kwargs: None)

    result = CliRunner().invoke(
        cli.main, ["--no-interactive", "--departure", "나주", "--arrival", "용산"]
    )

    assert result.exit_code == 1
    assert result.output.count("426 KTX") == 2  # the screen shows every table
    (path,) = list(runlog.LOG_DIR.iterdir())
    logged = _log_lines(path)
    assert sum("426 KTX" in line for line in logged) == 1
    assert [line for line in logged if line.startswith("조회 요청")] == [
        "조회 요청 #1",
        "조회 요청 #2",
        "조회 요청 #3",
    ]
    assert sum("Attempt" in line for line in logged) == 2
    assert any("안티매크로 차단 감지" in line for line in logged)
