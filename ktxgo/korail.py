from __future__ import annotations

import json
import os
import random
import select
import sys
import time
from dataclasses import dataclass
from typing import cast

from .cdp import CdpError, CdpPage
from .config import (
    API_LOGIN_CHECK,
    API_MYTICKET,
    API_PAY,
    API_RESERVATION_LIST,
    API_RESERVATION_VIEW,
    API_RESERVE,
    API_SCHEDULE,
    API_WAITLIST_ALERT,
    LOGIN_URL,
    MOBILE_DEVICE,
    MOBILE_KEY,
    MOBILE_VERSION,
    RSV_AVAILABLE,
    RSV_WAITING,
    SEARCH_URL,
    TRAIN_GROUP_KTX,
    train_type_codes,
)

# The login form as korail.com ships it. Typing goes through these ids and
# classes only, so a site redesign shows up as "login_form_not_found" rather
# than as a silent mis-click.
LOGIN_MEMBER_TAB = "button#memberNo"
LOGIN_ID_INPUT = "input#id"
LOGIN_PASSWORD_INPUT = "input#password"
LOGIN_SUBMIT = "section.loginWrap div.mem_wrap div.btnWrap > button.btn_bn-depblue"
# Korail shows its notices in a React modal, not a native dialog.
NOTICE_TEXT_JS = "(document.querySelector('.ReactModalPortal') || {}).innerText || ''"
# Shown for any login request DynaPath refuses to forward.
COMM_ERROR_NOTICE = "통신 중 에러"
# Kept specific on purpose: a successful login can pop up a "비밀번호 변경 안내",
# and a bare "비밀번호" here would turn that into a refused-credentials verdict.
# Korail words the same refusal differently over time ("잘못 입력하셨습니다(회원번호)"
# in September, "등록되지 않는 회원번호입니다" by November); an unlisted wording is
# still caught as an unknown notice, which also stops the automatic login.
BAD_CREDENTIAL_HINTS = ("잘못 입력", "일치하지", "존재하지", "등록되지 않")


def _wait_for_enter(timeout_s: float) -> bool:
    """Wait for the user to press Enter; False once `timeout_s` runs out.

    A bare input() never times out, which left the "5분 제한" (and the browser
    window) hanging for good if nobody came back to the terminal.
    """
    if os.name == "posix" and sys.stdin.isatty():
        ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout_s))
        if not ready:
            return False
    _ = input()
    return True


class KorailError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code: str | None = code


class MacroBlockedError(KorailError):
    """Korail's DynaPath anti-macro filter rejected the request.

    Retrying deepens the block instead of clearing it, so callers must stop
    rather than re-authenticate or back off.
    """


class BadCredentialsError(KorailError):
    """Korail refused the saved id/password.

    Every further attempt is another failed login on the account, so callers
    must stop automatic logins and hand over to the user.
    """


@dataclass(slots=True)
class Train:
    train_no: str
    train_type: str
    train_group: str
    departure: str
    arrival: str
    dep_time: str
    arr_time: str
    dep_date: str
    general_seat: str
    general_code: str
    special_seat: str
    special_code: str
    standing_seat: str
    waiting_seat: str
    waiting_code: str
    price: str
    raw: dict[str, str]

    @property
    def has_general(self) -> bool:
        return self.general_code == RSV_AVAILABLE

    @property
    def has_special(self) -> bool:
        return self.special_code == RSV_AVAILABLE

    @property
    def has_any_seat(self) -> bool:
        return self.has_general or self.has_special

    @property
    def has_standing(self) -> bool:
        return self.raw.get("h_stnd_rsv_cd", "") == RSV_AVAILABLE

    @property
    def has_waiting_list(self) -> bool:
        code = self.waiting_code.strip()
        if code.isdigit():
            code = code.zfill(2)
        if code == RSV_WAITING:
            return True

        name = self.waiting_seat.strip()
        if not name:
            return False
        if "가능" in name and not any(
            token in name for token in ("불가", "없", "마감")
        ):
            return True
        return False

    @property
    def waiting_status(self) -> str:
        name = self.waiting_seat.strip()
        if name:
            return name
        return "가능" if self.has_waiting_list else "불가"

    @classmethod
    def from_schedule(cls, row: dict[str, object]) -> Train:
        normalized = {
            str(key): "" if value is None else str(value) for key, value in row.items()
        }
        return cls(
            train_no=normalized.get("h_trn_no", ""),
            train_type=normalized.get("h_car_tp_nm", "")
            or normalized.get("h_trn_clsf_nm", ""),
            train_group=normalized.get("h_trn_gp_nm", ""),
            departure=normalized.get("h_dpt_rs_stn_nm", ""),
            arrival=normalized.get("h_arv_rs_stn_nm", ""),
            dep_time=normalized.get("h_dpt_tm_qb", ""),
            arr_time=normalized.get("h_arv_tm_qb", ""),
            dep_date=normalized.get("h_dpt_dt", ""),
            general_seat=normalized.get("h_gen_rsv_nm", ""),
            general_code=normalized.get("h_gen_rsv_cd", ""),
            special_seat=normalized.get("h_spe_rsv_nm", ""),
            special_code=normalized.get("h_spe_rsv_cd", ""),
            standing_seat=normalized.get("h_stnd_rsv_nm", ""),
            waiting_seat=normalized.get("h_wait_rsv_nm", ""),
            waiting_code=normalized.get(
                "h_wait_rsv_flg", normalized.get("h_wait_rsv_cd", "")
            ),
            price=normalized.get("h_rcvd_amt", ""),
            raw=normalized,
        )


class KorailAPI:
    def __init__(self, page: CdpPage):
        self.page: CdpPage = page
        self.last_auto_login_error: str | None = None
        self.last_auto_login_detail: str | None = None

    def _api_call(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        payload = cast(
            dict[str, object],
            self.page.evaluate(
                """async ({ endpoint, params }) => {
                const form = new FormData();
                for (const [key, value] of Object.entries(params)) {
                    form.append(key, value == null ? "" : String(value));
                }

                const response = await fetch(endpoint, {
                    method: "POST",
                    body: form,
                    credentials: "include"
                });

                const text = await response.text();
                return { ok: response.ok, status: response.status, text };
            }""",
                {"endpoint": endpoint, "params": params},
            ),
        )

        status = payload.get("status", 0)
        text_obj = payload.get("text", "")
        text = str(text_obj).strip()
        if not text:
            raise KorailError(f"Empty response from {endpoint} (HTTP {status})")

        raw_data: object
        try:
            raw_data = cast(object, json.loads(text))
        except json.JSONDecodeError as exc:
            # Korail answers a request it refuses with a bare HTML error page.
            raise KorailError(
                f"HTTP {status} without JSON from {endpoint}", f"HTTP_{status}"
            ) from exc

        if not isinstance(raw_data, dict):
            raise KorailError(f"Unexpected JSON payload from {endpoint}")

        data = cast(dict[str, object], raw_data)

        # DynaPath answers with its own payload shape that carries no
        # strResult, so without this check a block reads as an empty result
        # set and the caller silently retries forever.
        err_code = str(data.get("errCode", "")).strip()
        dyna_code = str(data.get("dynaPathResultCode", "")).strip()
        if err_code or dyna_code:
            raise MacroBlockedError(
                str(data.get("errMsg") or "Blocked by Korail anti-macro filter"),
                err_code or dyna_code,
            )

        if str(data.get("strResult", "")) == "FAIL":
            raise KorailError(
                str(
                    data.get("h_msg_txt") or data.get("message") or "Korail API failed"
                ),
                str(data.get("h_msg_cd") or data.get("code") or ""),
            )

        return data

    def _notice_text(self) -> str:
        try:
            text = str(self.page.evaluate(NOTICE_TEXT_JS) or "")
        except CdpError:
            return ""
        return " ".join(text.split())

    def login_manual(
        self, timeout_s: int = 300, *, open_login_page: bool = True
    ) -> bool:
        """Open the login page and wait for the user to log in by hand.

        The user does the typing in the visible browser. Afterwards they
        confirm in the terminal and a single loginCheck verifies it; polling
        instead would add traffic on the very page Korail watches closest.
        """
        if open_login_page:
            self.page.goto(LOGIN_URL)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                print(
                    "\n[로그인 대기] 브라우저에서 로그인을 완료한 후 "
                    "이 터미널로 돌아와 Enter를 눌러주세요 "
                    "(취소: Ctrl+C) ",
                    end="",
                    flush=True,
                )
                if not _wait_for_enter(deadline - time.monotonic()):
                    return False
            except (EOFError, KeyboardInterrupt):
                return False

            if self.is_logged_in():
                try:
                    self.page.goto(SEARCH_URL)
                except CdpError:
                    pass
                return True

            print(
                "로그인이 확인되지 않았습니다. 브라우저에서 다시 시도한 뒤 "
                "Enter를 눌러주세요."
            )
        return False

    def login_with_credentials(
        self, member_id: str, password: str, timeout_s: float = 25.0
    ) -> bool:
        """Log in through the real form, typed key by key. Submits exactly once.

        Returns True once the session is confirmed. Raises MacroBlockedError if
        Korail answers with its generic communication error (DynaPath refused
        the request) and BadCredentialsError if the id/password was refused.
        Other failures return False with last_auto_login_error/detail set.
        """
        self.last_auto_login_error = None
        self.last_auto_login_detail = None
        member_id = member_id.strip()
        if not member_id or not password:
            self.last_auto_login_error = "missing_credentials"
            self.last_auto_login_detail = "KTX id/pass is empty."
            return False

        self.page.goto(LOGIN_URL)
        if not self.page.wait_for_selector(LOGIN_PASSWORD_INPUT):
            self.last_auto_login_error = "login_form_not_found"
            self.last_auto_login_detail = "The login form did not appear."
            return False
        time.sleep(random.uniform(1.2, 2.0))  # let the page settle like a person would

        _ = self.page.click(LOGIN_MEMBER_TAB)
        for selector, text in ((LOGIN_ID_INPUT, member_id), (LOGIN_PASSWORD_INPUT, password)):
            if not self.page.click(selector):
                self.last_auto_login_error = "fill_failed"
                self.last_auto_login_detail = f"Could not focus {selector}."
                return False
            self.page.type_text(text)

        typed = self.page.evaluate(
            "[document.querySelector(%s).value.length, document.querySelector(%s).value.length]"
            % (json.dumps(LOGIN_ID_INPUT), json.dumps(LOGIN_PASSWORD_INPUT))
        )
        if typed != [len(member_id), len(password)]:
            self.last_auto_login_error = "fill_failed"
            self.last_auto_login_detail = "The typed text did not land in the form."
            return False

        time.sleep(random.uniform(0.6, 1.0))
        if not self.page.click(LOGIN_SUBMIT):
            self.last_auto_login_error = "submit_not_found"
            self.last_auto_login_detail = "The login button was not found."
            return False

        notice = ""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not str(self.page.evaluate("location.pathname")).startswith("/ticket/login"):
                break
            notice = self._notice_text() or notice
            # Only a known refusal ends the wait early. Any other notice (a
            # password-change reminder, say) can sit on screen while a
            # successful login is still redirecting.
            if COMM_ERROR_NOTICE in notice or any(
                hint in notice for hint in BAD_CREDENTIAL_HINTS
            ):
                break

        # A notice can also announce a successful login (password-change
        # reminders and the like), so trust the session check before the text.
        # After a known refusal there is nothing to wait for: one look suffices.
        refused = COMM_ERROR_NOTICE in notice or any(
            hint in notice for hint in BAD_CREDENTIAL_HINTS
        )
        if self.wait_for_login_stable(
            timeout_s=0.0 if refused else 4.0, interval_s=0.4, stable_checks=2
        ):
            return True

        if COMM_ERROR_NOTICE in notice:
            raise MacroBlockedError(
                "The login request was refused by Korail's anti-macro filter.",
                "login_rejected",
            )
        if any(hint in notice for hint in BAD_CREDENTIAL_HINTS):
            raise BadCredentialsError(notice)

        if notice:
            # Korail answered the login with a message this code does not know.
            # Reporting it apart from a plain failure lets the caller stop
            # instead of submitting the same password again and again.
            self.last_auto_login_error = "login_notice"
            self.last_auto_login_detail = notice
            return False

        self.last_auto_login_error = "login_check_failed"
        self.last_auto_login_detail = "Submitted, but the login was not confirmed."
        return False

    def wait_for_login_stable(
        self,
        *,
        timeout_s: float = 3.0,
        interval_s: float = 0.35,
        stable_checks: int = 2,
    ) -> bool:
        """Check login status with short retries to absorb session propagation delay."""
        required = max(1, stable_checks)
        streak = 0
        deadline = time.monotonic() + max(0.0, timeout_s)

        while True:
            if self.is_logged_in():
                streak += 1
                if streak >= required:
                    return True
            else:
                streak = 0

            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.05, interval_s))

    @staticmethod
    def _train_sort_key(train: Train) -> tuple[str, str, str, str, str, str]:
        return (
            train.dep_date,
            train.dep_time,
            train.arr_time,
            train.train_no,
            train.departure,
            train.arrival,
        )

    @staticmethod
    def _train_identity(train: Train) -> tuple[str, str, str, str, str]:
        return (
            train.dep_date,
            train.train_no,
            train.dep_time,
            train.departure,
            train.arrival,
        )

    @staticmethod
    def _trains_from_schedule_payload(data: dict[str, object]) -> list[Train]:
        trn_infos_obj = data.get("trn_infos")
        if not isinstance(trn_infos_obj, dict):
            return []
        trn_infos = cast(dict[str, object], trn_infos_obj)

        trn_info_obj = trn_infos.get("trn_info")
        if isinstance(trn_info_obj, dict):
            return [Train.from_schedule(cast(dict[str, object], trn_info_obj))]
        if not isinstance(trn_info_obj, list):
            return []

        trains: list[Train] = []
        for row in cast(list[object], trn_info_obj):
            if isinstance(row, dict):
                trains.append(Train.from_schedule(cast(dict[str, object], row)))
        return trains

    @staticmethod
    def _matches_requested_train_types(
        train: Train, requested_train_types: tuple[str, ...] | None
    ) -> bool:
        if not requested_train_types:
            return True

        raw_name = train.train_type.strip()
        code = str(
            train.raw.get("h_trn_gp_cd", "") or train.raw.get("h_trn_clsf_cd", "")
        ).strip()

        for train_type in requested_train_types:
            if train_type == "ktx" and code == "100":
                return True
            if train_type == "itx-saemaeul" and raw_name == "ITX-새마을":
                return True
            if train_type == "mugunghwa" and raw_name.startswith("무궁화"):
                return True
            if train_type == "tonggeun" and "통근" in raw_name:
                return True
            if train_type == "itx-cheongchun" and raw_name == "ITX-청춘":
                return True
            if train_type == "itx-maeum" and raw_name == "ITX-마음":
                return True
            if train_type == "airport" and "공항" in raw_name:
                return True

        return False

    def search(
        self,
        departure: str,
        arrival: str,
        date: str,
        time_str: str,
        adults: int = 1,
        train_types: tuple[str, ...] | None = None,
    ) -> list[Train]:
        hh = time_str.zfill(2)
        seen: set[tuple[str, str, str, str, str]] = set()
        trains: list[Train] = []

        for train_code in train_type_codes(train_types):
            params = {
                "Device": "BH",
                "Version": "999999999",
                "radJobId": "1",
                "selGoTrain": train_code,
                "txtCardPsgCnt": "0",
                "txtGdNo": "",
                "txtGoAbrdDt": date,
                "txtGoEnd": arrival,
                "txtGoHour": f"{hh}0000",
                "txtGoStart": departure,
                "txtJobDv": "",
                "txtMenuId": "11",
                "txtPsgFlg_1": str(adults),
                "txtPsgFlg_2": "0",
                "txtPsgFlg_3": "0",
                "txtPsgFlg_4": "0",
                "txtPsgFlg_5": "0",
                "txtSeatAttCd_2": "000",
                "txtSeatAttCd_3": "000",
                "txtSeatAttCd_4": "015",
                "txtTrnGpCd": train_code,
                "searchType": "GENERAL",
            }
            data = self._api_call(API_SCHEDULE, params)
            for train in self._trains_from_schedule_payload(data):
                if not self._matches_requested_train_types(train, train_types):
                    continue
                identity = self._train_identity(train)
                if identity in seen:
                    continue
                seen.add(identity)
                trains.append(train)

        return sorted(trains, key=self._train_sort_key)

    def reserve(
        self,
        train: Train,
        seat_type: str = "general",
        adults: int = 1,
        waitlist: bool = False,
    ) -> dict[str, object]:
        seat_code = "1" if seat_type == "general" else "2"
        dep_time = str(
            train.raw.get("h_dpt_tm", "") or train.raw.get("h_dpt_tm_qb", "")
        )
        dep_time = dep_time.replace(":", "")
        if len(dep_time) == 4:
            dep_time = f"{dep_time}00"

        params = {
            "Device": "BH",
            "Version": "999999999",
            "txtMenuId": "11",
            "txtJobId": "1102" if waitlist else "1101",
            "txtGdNo": "",
            "hidFreeFlg": "N",
            "txtTotPsgCnt": str(adults),
            "txtSeatAttCd1": "000",
            "txtSeatAttCd2": "000",
            "txtSeatAttCd3": "000",
            "txtSeatAttCd4": "015",
            "txtSeatAttCd5": "000",
            "txtStndFlg": "N",
            "txtSrcarCnt": "0",
            "txtJrnyCnt": "1",
            "txtJrnySqno1": "001",
            "txtJrnyTpCd1": "11",
            "txtDptDt1": train.dep_date,
            "txtDptRsStnCd1": str(train.raw.get("h_dpt_rs_stn_cd", "")),
            "txtDptTm1": dep_time,
            "txtArvRsStnCd1": str(train.raw.get("h_arv_rs_stn_cd", "")),
            "txtTrnNo1": train.train_no,
            "txtRunDt1": str(train.raw.get("h_run_dt", train.dep_date)),
            "txtTrnClsfCd1": str(train.raw.get("h_trn_clsf_cd", "100")),
            "txtTrnGpCd1": str(train.raw.get("h_trn_gp_cd", TRAIN_GROUP_KTX)),
            "txtPsrmClCd1": seat_code,
            "txtChgFlg1": "",
            # Passenger 1: adult
            "txtPsgTpCd1": "1",
            "txtDiscKndCd1": "000",
            "txtCompaCnt1": str(adults),
            "txtCardCode_1": "",
            "txtCardNo_1": "",
            "txtCardPw_1": "",
        }
        return self._api_call(API_RESERVE, params)

    def set_waitlist_alert(
        self,
        pnr_no: str,
        phone: str,
        *,
        allow_seat_change: bool = False,
    ) -> dict[str, object]:
        return self._api_call(
            API_WAITLIST_ALERT,
            {
                "Device": MOBILE_DEVICE,
                "Version": MOBILE_VERSION,
                "Key": MOBILE_KEY,
                "txtPnrNo": pnr_no,
                "txtPsrmClChgFlg": "Y" if allow_seat_change else "N",
                "txtSmsSndFlg": "Y",
                "txtCpNo": phone,
            },
        )

    def is_logged_in(self) -> bool:
        try:
            data = self._api_call(API_LOGIN_CHECK, {})
        except MacroBlockedError:
            # Being blocked is not the same as being logged out; swallowing it
            # here would make callers "recover" by logging in again.
            raise
        except KorailError:
            return False

        # loginCheck returns strResult=SUCC even when NOT logged in.
        # Must check h_msg_txt to distinguish.
        msg = str(data.get("h_msg_txt", "")).strip()
        if "로그인 정보가 없습니다" in msg or "로그인" in msg and "없" in msg:
            return False

        # Positive indicators
        if data.get("strResult") in {"SUCC", "SUCCESS", "Y"}:
            # Double-check: presence of member credentials confirms login
            for key in ("strMbCrdNo", "strCustNm", "mbCrdNo"):
                value = str(data.get(key, "")).strip()
                if value and value not in {"N", "FALSE", "0"}:
                    return True
            # strResult=SUCC without negative msg — likely logged in
            return True

        for key in ("loginYn", "isLogin"):
            value = str(data.get(key, "")).strip().upper()
            if value and value not in {"N", "FALSE", "0"}:
                return True
        return False

    def login_profile(self) -> dict[str, str] | None:
        """Return current login profile from loginCheck response, or None."""
        try:
            data = self._api_call(API_LOGIN_CHECK, {})
        except KorailError:
            return None

        msg = str(data.get("h_msg_txt", "")).strip()
        if "로그인 정보가 없습니다" in msg or ("로그인" in msg and "없" in msg):
            return None

        member_no = ""
        for key in ("strMbCrdNo", "mbCrdNo", "strCustNo", "custNo"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                member_no = value
                break

        name = ""
        for key in ("strCustNm", "custNm", "h_cust_nm", "strUserNm"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                name = value
                break

        login_id = ""
        for key in ("strCustId", "custId", "userId"):
            value = str(data.get(key, "")).strip()
            if value and value not in {"N", "FALSE", "0"}:
                login_id = value
                break

        if not any((member_no, name, login_id)):
            if data.get("strResult") in {"SUCC", "SUCCESS", "Y"}:
                return {"member_no": "", "name": "", "login_id": ""}
            return None

        return {
            "member_no": member_no,
            "name": name,
            "login_id": login_id,
        }

    def reservations(self) -> list[dict[str, object]]:
        mobile_base = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
        }
        try:
            data = self._api_call(API_RESERVATION_VIEW, mobile_base)
        except KorailError as exc:
            code = (exc.code or "").strip()
            msg = str(exc)
            no_result_codes = {"P100", "WRG000000", "WRD000061", "WRT300005"}
            if code in no_result_codes or ("예약" in msg and "없" in msg):
                return []
            raise

        jrny_infos_obj = data.get("jrny_infos")
        if not isinstance(jrny_infos_obj, dict):
            return []
        jrny_infos = cast(dict[str, object], jrny_infos_obj)

        jrny_info_obj = jrny_infos.get("jrny_info")
        jrny_items: list[dict[str, object]] = []
        if isinstance(jrny_info_obj, dict):
            jrny_items = [cast(dict[str, object], jrny_info_obj)]
        elif isinstance(jrny_info_obj, list):
            jrny_items = [
                cast(dict[str, object], item)
                for item in cast(list[object], jrny_info_obj)
                if isinstance(item, dict)
            ]
        if not jrny_items:
            return []

        reservations: list[dict[str, object]] = []
        inherit_keys = (
            "h_pnr_no",
            "h_rsv_amt",
            "h_ntisu_lmt_dt",
            "h_ntisu_lmt_tm",
            "h_run_dt",
            "h_dpt_dt",
            "h_dpt_tm",
            "h_dpt_rs_stn_nm",
            "h_arv_rs_stn_nm",
            "h_trn_no",
            "h_rsv_chg_no",
            "hidRsvChgNo",
            "h_wct_no",
        )
        for jrny in jrny_items:
            train_infos_obj = jrny.get("train_infos")
            if not isinstance(train_infos_obj, dict):
                reservations.append(jrny)
                continue
            train_info_obj = train_infos_obj.get("train_info")
            train_items: list[dict[str, object]] = []
            if isinstance(train_info_obj, dict):
                train_items = [cast(dict[str, object], train_info_obj)]
            elif isinstance(train_info_obj, list):
                train_items = [
                    cast(dict[str, object], item)
                    for item in cast(list[object], train_info_obj)
                    if isinstance(item, dict)
                ]

            if not train_items:
                reservations.append(jrny)
                continue

            for train in train_items:
                merged = dict(train)
                for key in inherit_keys:
                    if str(merged.get(key, "")).strip():
                        continue
                    if key in jrny:
                        merged[key] = jrny[key]
                reservations.append(cast(dict[str, object], merged))
        return reservations

    def tickets(self) -> list[dict[str, object]]:
        """Return issued ticket list (paid bookings)."""
        no_result_codes = {"P100", "WRG000000", "WRD000061", "WRT300005"}
        param_candidates = [
            {
                "Device": MOBILE_DEVICE,
                "Version": MOBILE_VERSION,
                "Key": MOBILE_KEY,
                "txtDeviceId": "",
                "txtIndex": "1",
                "h_page_no": "1",
                "h_abrd_dt_from": "",
                "h_abrd_dt_to": "",
                "hiduserYn": "Y",
            },
            {
                "Device": "BH",
                "Version": "999999999",
                "txtDeviceId": "",
                "txtIndex": "1",
                "h_page_no": "1",
                "h_abrd_dt_from": "",
                "h_abrd_dt_to": "",
                "hiduserYn": "Y",
            },
        ]

        data: dict[str, object] | None = None
        for params in param_candidates:
            try:
                data = self._api_call(API_MYTICKET, params)
                break
            except KorailError as exc:
                code = (exc.code or "").strip()
                msg = str(exc)
                if code in no_result_codes or ("예약" in msg and "없" in msg):
                    return []
                continue

        if data is None:
            return []

        reservation_list_obj = data.get("reservation_list")
        entries: list[dict[str, object]] = []
        if isinstance(reservation_list_obj, dict):
            entries = [cast(dict[str, object], reservation_list_obj)]
        elif isinstance(reservation_list_obj, list):
            entries = [
                cast(dict[str, object], item)
                for item in cast(list[object], reservation_list_obj)
                if isinstance(item, dict)
            ]
        if not entries:
            return []

        tickets: list[dict[str, object]] = []
        inherit_keys = (
            "h_pnr_no",
            "h_orgtk_sale_dt",
            "h_orgtk_wct_no",
            "h_orgtk_ret_sale_dt",
            "h_orgtk_sale_sqno",
            "h_orgtk_ret_pwd",
            "h_rcvd_amt",
            "h_buy_ps_nm",
        )
        for entry in entries:
            ticket_list_obj = entry.get("ticket_list")
            ticket_items: list[dict[str, object]] = []
            if isinstance(ticket_list_obj, dict):
                ticket_items = [cast(dict[str, object], ticket_list_obj)]
            elif isinstance(ticket_list_obj, list):
                ticket_items = [
                    cast(dict[str, object], item)
                    for item in cast(list[object], ticket_list_obj)
                    if isinstance(item, dict)
                ]

            if not ticket_items:
                tickets.append(entry)
                continue

            for ticket in ticket_items:
                train_info_obj = ticket.get("train_info")
                train_items: list[dict[str, object]] = []
                if isinstance(train_info_obj, dict):
                    train_items = [cast(dict[str, object], train_info_obj)]
                elif isinstance(train_info_obj, list):
                    train_items = [
                        cast(dict[str, object], item)
                        for item in cast(list[object], train_info_obj)
                        if isinstance(item, dict)
                    ]

                if not train_items:
                    merged_ticket = dict(ticket)
                    for key in inherit_keys:
                        if str(merged_ticket.get(key, "")).strip():
                            continue
                        if key in entry:
                            merged_ticket[key] = entry[key]
                    tickets.append(cast(dict[str, object], merged_ticket))
                    continue

                for train in train_items:
                    merged = dict(train)
                    for key in inherit_keys:
                        if str(merged.get(key, "")).strip():
                            continue
                        if key in ticket:
                            merged[key] = ticket[key]
                        elif key in entry:
                            merged[key] = entry[key]
                    tickets.append(cast(dict[str, object], merged))

        return tickets

    def pay(
        self,
        reserve_result: dict[str, object],
        card_number: str,
        card_password: str,
        birthday: str,
        card_expire: str,
        smart_ticket: bool = True,
        installment: int = 0,
        card_type: str | None = None,
    ) -> dict[str, object]:
        """Pay for a reservation using a credit card.

        Args:
            reserve_result: The dict returned by reserve().
            card_number: Full card number (no hyphens).
            card_password: First 2 digits of card password.
            birthday: YYMMDD (individual) or 10-digit biz registration number.
            card_expire: Card expiry in YYMM format.
            smart_ticket: True = issue as smart ticket (KorailTalk), False = non-smart.
            installment: 0 = lump sum, N = N-month installment.
            card_type: 'J' for individual, 'S' for business. Auto-detected from birthday length.
        """
        pnr_no = str(reserve_result.get("h_pnr_no", ""))
        if not pnr_no:
            raise KorailError("Missing reservation number (h_pnr_no).")

        wct_no = str(reserve_result.get("h_wct_no", ""))
        rsv_chg_no = str(
            reserve_result.get("h_rsv_chg_no", reserve_result.get("hidRsvChgNo", ""))
        ).strip()
        tmp_job_sqno1 = str(reserve_result.get("h_tmp_job_sqno1", "000000"))
        tmp_job_sqno2 = str(reserve_result.get("h_tmp_job_sqno2", "000000"))
        price = ""

        def _digit_only(value: object) -> str:
            return "".join(ch for ch in str(value) if ch.isdigit())

        def _dict_items(value: object) -> list[dict[str, object]]:
            if isinstance(value, dict):
                return [cast(dict[str, object], value)]
            if not isinstance(value, list):
                return []
            return [
                cast(dict[str, object], item)
                for item in cast(list[object], value)
                if isinstance(item, dict)
            ]

        def _hydrate_from_item(item: dict[str, object], *, require_pnr: bool) -> None:
            nonlocal price, wct_no, tmp_job_sqno1, tmp_job_sqno2, rsv_chg_no
            if require_pnr:
                item_pnr = str(item.get("h_pnr_no", item.get("hidPnrNo", ""))).strip()
                if item_pnr and item_pnr != pnr_no:
                    return

            if not price or price == "0":
                for key in ("h_rsv_amt", "h_rcvd_amt", "hidPayAmount"):
                    candidate = _digit_only(item.get(key, ""))
                    if candidate and candidate != "0":
                        price = candidate
                        break

            if not wct_no:
                for key in ("h_wct_no", "hidWctNo"):
                    candidate = str(item.get(key, "")).strip()
                    if candidate:
                        wct_no = candidate
                        break

            if not rsv_chg_no:
                for key in ("h_rsv_chg_no", "hidRsvChgNo"):
                    candidate = str(item.get(key, "")).strip()
                    if candidate:
                        rsv_chg_no = candidate
                        break

            if tmp_job_sqno1 in {"", "000000"}:
                candidate = str(item.get("h_tmp_job_sqno1", "")).strip()
                if candidate:
                    tmp_job_sqno1 = candidate

            if tmp_job_sqno2 in {"", "000000"}:
                candidate = str(item.get("h_tmp_job_sqno2", "")).strip()
                if candidate:
                    tmp_job_sqno2 = candidate

        def _hydrate_from_payload(
            data: dict[str, object], *, include_top_level: bool
        ) -> None:
            if include_top_level:
                _hydrate_from_item(data, require_pnr=False)
            jrny_infos_obj = data.get("jrny_infos")
            if not isinstance(jrny_infos_obj, dict):
                return
            jrny_items = _dict_items(jrny_infos_obj.get("jrny_info"))
            for jrny in jrny_items:
                _hydrate_from_item(jrny, require_pnr=False)

                train_infos_obj = jrny.get("train_infos")
                if not isinstance(train_infos_obj, dict):
                    continue
                for train in _dict_items(train_infos_obj.get("train_info")):
                    _hydrate_from_item(train, require_pnr=True)

        _hydrate_from_payload(reserve_result, include_top_level=True)

        # Some reserve responses omit payment context.
        # Recover from reservation APIs using the same defaults as KorailTalk.
        mobile_base = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
        }
        need_context = (
            (not price or price == "0")
            or not wct_no
            or not rsv_chg_no
            or tmp_job_sqno1 in {"", "000000"}
            or tmp_job_sqno2 in {"", "000000"}
        )
        if need_context:
            detail = self._api_call(
                API_RESERVATION_LIST,
                {
                    **mobile_base,
                    "hidPnrNo": pnr_no,
                },
            )
            _hydrate_from_payload(detail, include_top_level=True)

        if (
            (not price or price == "0")
            or not wct_no
            or not rsv_chg_no
            or tmp_job_sqno1 in {"", "000000"}
            or tmp_job_sqno2 in {"", "000000"}
        ):
            view = self._api_call(API_RESERVATION_VIEW, mobile_base)
            _hydrate_from_payload(view, include_top_level=False)

        if not price or price == "0":
            raise KorailError("Unable to determine payment amount.")
        if not wct_no:
            raise KorailError("Unable to determine payment key (h_wct_no).")

        if card_type is None:
            card_type = "J" if len(birthday) <= 6 else "S"

        params = {
            "Device": MOBILE_DEVICE,
            "Version": MOBILE_VERSION,
            "Key": MOBILE_KEY,
            "hidPnrNo": pnr_no,
            "hidWctNo": wct_no,
            "hidTmpJobSqno1": tmp_job_sqno1,
            "hidTmpJobSqno2": tmp_job_sqno2,
            "hidRsvChgNo": rsv_chg_no or "000",
            "hidInrecmnsGridcnt": "1",
            "hidStlMnsSqno1": "1",
            "hidStlMnsCd1": "02",
            "hidMnsStlAmt1": price,
            "hidCrdInpWayCd1": "@",
            "hidStlCrCrdNo1": card_number,
            "hidVanPwd1": card_password,
            "hidCrdVlidTrm1": card_expire,
            "hidIsmtMnthNum1": str(installment),
            "hidAthnDvCd1": card_type,
            "hidAthnVal1": birthday,
            "hiduserYn": "Y" if smart_ticket else "N",
        }
        return self._api_call(API_PAY, params)
