from __future__ import annotations

from click.testing import CliRunner

from ktxgo import cli
from ktxgo.korail import Train


def _train(train_no: str, dep_time: str, gen_code: str) -> Train:
    return Train.from_schedule(
        {
            "h_trn_no": train_no.zfill(5),
            "h_car_tp_nm": "KTX",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_nm": "KTX",
            "h_dpt_rs_stn_nm": "나주",
            "h_arv_rs_stn_nm": "용산",
            "h_dpt_tm_qb": dep_time,
            "h_arv_tm_qb": "23:26",
            "h_dpt_dt": "20261115",
            "h_gen_rsv_nm": "좌석많음" if gen_code == "11" else "매진",
            "h_gen_rsv_cd": gen_code,
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


def _run(monkeypatch, trains: list[Train], args: list[str]) -> tuple[list[str], str]:
    reserved: list[str] = []

    class DummyAPI:
        def __init__(self, page: object):
            del page

        def search(self, *args, **kwargs) -> list[Train]:
            return trains

        def reserve(self, train: Train, **kwargs) -> dict[str, object]:
            reserved.append(train.train_no)
            return {"h_pnr_no": "PNR1", "strResult": "SUCC"}

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
        cli.main,
        ["--no-interactive", "--departure", "나주", "--arrival", "용산", "--seat", "general", *args],
    )
    assert result.exit_code == 0, result.output
    return reserved, result.output


def test_train_option_ignores_other_trains_with_seats(monkeypatch) -> None:
    trains = [_train("426", "14:46", "13"), _train("444", "22:13", "11")]

    reserved, output = _run(monkeypatch, trains, ["--train", "426", "--max-attempts", "2"])

    assert reserved == []
    assert "대상 열차: 426" in output


def test_train_option_reserves_the_named_train(monkeypatch) -> None:
    trains = [_train("426", "14:46", "13"), _train("444", "22:13", "11")]

    reserved, _ = _run(monkeypatch, trains, ["--train", "426", "--train", "0444"])

    assert [cli._normalize_train_no(n) for n in reserved] == ["444"]


def test_without_train_option_any_train_with_seats_is_reserved(monkeypatch) -> None:
    trains = [_train("426", "14:46", "13"), _train("444", "22:13", "11")]

    reserved, _ = _run(monkeypatch, trains, [])

    assert len(reserved) == 1
