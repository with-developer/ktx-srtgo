# KTXgo

Chrome(CDP) 기반 코레일 열차 예매 자동화 CLI.

코레일 웹사이트의 DynaPath 매크로 차단을 우회하여 열차 검색 → 좌석 예매/예약대기 → 자동결제 → 텔레그램 알림까지 자동화합니다.

> **주의**: 본 프로그램의 모든 상업적·영리적 이용을 엄격히 금지합니다. 사용에 따른 모든 책임은 사용자에게 있으며, 개발자는 어떠한 책임도 부담하지 않습니다.

## 동작 원리

1. 설치된 **Google Chrome**을 직접 실행하고(임시 프로필), korail.com에 접속하여 DynaPath JS를 정상 로드
2. 브라우저 컨텍스트 내에서 `fetch()` API를 직접 호출하여 열차 검색·예약·결제 수행
3. Chrome은 **아무 CDP 도메인도 켜지 않는 최소 클라이언트**(`cdp.py`)로만 조종합니다. Playwright가 켜는 기능(`Runtime.enable` 등)이 DynaPath에 잡혀 로그인과 조회가 거부되기 때문입니다 (아래 '기술적 세부사항')
4. 로그인은 저장된 계정을 실제 폼에 한 글자씩 입력해 **1회만 제출**하고, 세션 쿠키를 저장해 재사용합니다

## 요구사항

- Python 3.10+
- Google Chrome (다른 위치에 있으면 `KTXGO_CHROME`에 실행 파일 경로 지정)

```bash
pip install click inquirer keyring keyrings.alt python-telegram-bot termcolor websockets
```

## 초기 설정

### 코레일 계정 (keyring)

```bash
keyring set KTX id        # 회원번호
keyring set KTX pass      # 비밀번호 (세션 만료 시 자동 로그인에 사용)
```

### 카드 정보 (자동결제 사용 시)

```bash
# TTY 등록 (권장)
python3 -m ktxgo --set-card

# 직접 keyring 등록
keyring set KTX card_number     # 카드번호 (하이픈 없이)
keyring set KTX card_password   # 카드 비밀번호 앞 2자리
keyring set KTX birthday        # 생년월일 YYMMDD (개인) / 사업자등록번호 10자리
keyring set KTX card_expire     # 유효기간 YYMM
```

### 텔레그램 알림 (선택)

```bash
keyring set telegram token      # 봇 토큰
keyring set telegram chat_id    # 채팅 ID
```

### 예약대기 좌석배정 SMS 알림 (선택)

```bash
# interactive 메뉴 등록/수정
python3 -m ktxgo

# 직접 keyring 등록
keyring set KTX waitlist_alert_phone   # 좌석배정 SMS를 받을 전화번호
```

interactive 메뉴의 `예약대기 SMS 알림 번호 등록/수정`에서도 같은 값을 저장할 수 있습니다.

현재 확인된 코레일톡 요청 계약은 `SMS` 알림만 노출합니다. 카카오톡 채널은 아직 확인되지 않았습니다.

## 사용법

```bash
# 기본 실행 (메뉴: 예매 시작 / 로그인 설정 / 역 설정 / 예약대기 SMS 알림 번호 등록·수정 / 카드 등록·수정 / 나가기)
python3 -m ktxgo

# 옵션 지정 후 대화형 실행
python3 -m ktxgo \
  --departure 서울 \
  --arrival 부산 \
  --date 20260305 \
  --time 06 \
  --train-type itx-saemaeul \
  --train-type mugunghwa \
  --seat general \
  --waitlist-alert-phone 01012341234 \
  --auto-pay \
  --telegram

# 브라우저 창 표시 (디버깅용)
python3 -m ktxgo --no-headless

# 시도 횟수 제한
python3 -m ktxgo --max-attempts 100

# 기존 방식(비대화형, 기본은 KTX만)
python3 -m ktxgo --no-interactive

# 일반열차 포함 비대화형 실행
python3 -m ktxgo \
  --no-interactive \
  --train-type itx-saemaeul \
  --train-type mugunghwa \
  --train-type itx-cheongchun
```

### CLI 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--departure` | 서울 | 출발역 |
| `--arrival` | 부산 | 도착역 |
| `--date` | 현재+10분 기준 | 출발일 (YYYYMMDD) |
| `--time` | 현재+10분 기준 | 출발 시간대 (HH) |
| `--interactive` / `--no-interactive` | TTY에서 interactive | 날짜/시간/열차 선택 프롬프트 사용 여부. interactive에서는 `KTX만` / `KTX + ITX/무궁화 등` 프리셋 제공 |
| `--train-type` | `ktx` | 반복 지정 가능. `ktx`, `itx-saemaeul`, `mugunghwa`, `tonggeun`, `itx-cheongchun`, `itx-maeum`, `airport`, `legacy-all` (`saemaeul`, `nuriro` alias 지원) |
| `--seat` | any | `general` / `special` / `any` / `standing` |
| `--headless` / `--no-headless` | headless | 브라우저 표시 여부 |
| `--set-card` | off | TTY에서 카드 정보 등록 후 종료 |
| `--max-attempts` | 0 (무한) | 최대 검색 시도 횟수 |
| `--auto-pay` | off | 예약 성공 후 자동 카드결제 |
| `--smart-ticket` / `--no-smart-ticket` | smart-ticket | 자동결제 시 스마트티켓 발권 여부 |
| `--telegram` | off | 예약/결제 결과 텔레그램 알림 |
| `--waitlist-alert-phone` | off | 예약대기 성공 시 좌석배정 SMS 알림을 등록할 전화번호. 미지정 시 `keyring`의 `KTX waitlist_alert_phone` 사용 |

### 지원 역 목록

서울, 용산, 광명, 수서, 영등포, 수원, 평택, 천안아산, 천안, 오송, 조치원, 대전, 서대전, 김천구미, 구미, 동대구, 대구, 경주, 울산(통도사), 포항, 경산, 밀양, 부산, 구포, 창원중앙, 평창, 진부(오대산), 강릉, 익산, 전주, 광주송정, 목포, 순천, 청량리, 정동진

## 실행 흐름

```
시작
 ├─ 저장된 쿠키로 로그인 확인
 │   ├─ 성공 → headless 모드로 진행
 │   └─ 실패 → 저장된 계정(KTX id/pass)으로 자동 로그인 (실제 폼에 입력, 제출 1회)
 │            ├─ 성공 → 세션 저장
 │            ├─ 차단(통신 중 에러) → 재시도 없이 즉시 중지
 │            ├─ 계정 거부(잘못 입력) → 자동 로그인 중단 (실패 로그인이 계정에 쌓이지 않도록)
 │            └─ 그 외 실패/한도 초과(1시간 3회, 30·60·120초 백오프)
 │                  → 브라우저 창을 띄워 사용자가 직접 로그인 (5분) → 세션 저장 → (필요 시) headless 전환
 │
 ├─ (interactive 모드) 시작 메뉴
 │   ├─ 로그인 설정
 │   ├─ 역 설정
 │   ├─ 예약대기 SMS 알림 번호 등록/수정
 │   ├─ 카드 등록/수정
 │   └─ 예매 시작
 │      ├─ 출발/도착/날짜/시간/조회 열차 범위(`KTX만` / `KTX + ITX/무궁화 등`) 입력
 │      ├─ 초기 조회 결과에서 예약 시도할 열차 선택 (다중선택 가능)
 │      └─ 좌석 선호 + 자동결제 여부 + 스마트티켓 발권 여부 확인
 │
 ├─ 매크로 루프 시작
 │   ├─ ScheduleView API로 열차 검색
 │   ├─ 선택한 열차(비대화형은 전체) 중
 │   │   ├─ 좌석 있으면 TicketReservation API(`txtJobId=1101`)로 예매
 │   │   └─ 좌석 매진 + 예약대기 가능이면 TicketReservation API(`txtJobId=1102`)로 예약대기 신청
 │   │       ├─ 성공
 │   │       │   ├─ `ReservationWait` API로 좌석배정 SMS 알림 전화번호 등록
 │   │       │   ├─ 좌석 예매 + --auto-pay → ReservationPayment API로 결제
 │   │       │   ├─ --telegram → 텔레그램 알림 전송
 │   │       │   └─ 종료
 │   │       └─ 실패 → 다음 열차 시도
 │   └─ 좌석/예약대기 모두 불가 → 1.2초 대기 후 재검색
 │
 └─ 세션 만료 감지 시 자동 재인증
```

## 프로젝트 구조

```
ktxgo/
├── __init__.py     # 패키지 초기화, 버전
├── __main__.py     # python -m ktxgo 진입점
├── config.py       # API 엔드포인트, 역 목록, 상수
├── cdp.py          # 최소 Chrome DevTools Protocol 클라이언트 (도메인 enable 없음)
├── browser.py      # Chrome 실행/종료, 임시 프로필, 쿠키 저장/복원
├── korail.py       # KorailAPI (검색, 예약, 결제, 로그인)
├── store.py        # 설정(prefs.json) / 비밀(키체인 항목 1개) 저장
├── runlog.py       # 실행 기록 (화면 출력을 ~/.ktxgo/logs/에 복사)
├── keyring_bootstrap.py  # keyring 백엔드 폴백
└── cli.py          # Click CLI, 매크로 루프, 자동결제, 텔레그램
```

## 데이터 저장 경로

| 파일 | 경로 | 설명 |
|------|------|------|
| 쿠키 | `~/.ktxgo/cookies.json` | 브라우저 세션 쿠키 (0600) |
| 설정 | `~/.ktxgo/prefs.json` | 역 목록, 마지막 조회 조건 (비밀 아님) |
| 실행 기록 | `~/.ktxgo/logs/YYYYMMDD-HHMMSS.log` | 실행마다 새 파일. 화면 출력에 시각을 붙여 저장(0600). 메뉴 화면·같은 조회 결과 반복은 빼고, 조회마다 `조회 요청 #N` 줄을 남김. 끝에 `실행 종료`가 없으면 비정상 종료. 자동 삭제 없음 |
| 계정/카드/알림전화/텔레그램 | OS keyring 항목 1개 (`KTX` / `ktxgo-secrets`) | JSON으로 묶어 저장해 키체인 접근을 실행당 1회로 줄임 |

## 기술적 세부사항

### DynaPath

코레일은 DynaPath SDK를 사용하여 매크로/봇을 차단합니다:

- **loginProcess**: 최엄격 보호. 요청이 `/dynaPath/…`(또는 `/web_s/…`) 주소로 감싸져 전송되고, 거부되면 HTTP 500이 돌아와 화면에는 "통신 중 에러가 발생하였습니다"가 뜹니다
- **ScheduleView**: `web_s` 레벨 보호
- **TicketReservation**: `web_r` 레벨 보호
- **ReservationWait**: 예약대기 후 좌석배정 SMS 알림 전화번호 등록
- **ReservationPayment**: dpCnf 목록에 없음 → 직접 fetch() 호출 가능

### Playwright가 막히는 이유 (2026-09 실측)

같은 Chrome, 같은 계정, 같은 IP에서 조종 방식만 바꿔 비교한 결과입니다.

| 조종 방식 | 로그인 | 사이트 자신의 조회 클릭 |
|---|---|---|
| 사람이 직접 (CDP 없음) | 정상 | 정상 |
| Playwright로 실행 / Playwright로 연결 | **"통신 중 에러"** (HTTP 500) | **HTTP 500** ("운행하는 열차가 없습니다"로 표시) |
| 최소 CDP (`Page.navigate`·`Runtime.evaluate`·`Input.*`만) | 정상 | 정상 |

- 사람이 로그인한 창에 Playwright가 붙기만 해도 그 창의 조회가 거부되고, 연결을 끊고 최소 CDP로 다시 하면 같은 세션에서 통과합니다. 낙인이 세션에 남는 방식이 아니라 **요청 시점에 보이는 CDP 사용 흔적**을 검사합니다.
- 플래그 없이 평범하게 띄운 Chrome에 Playwright만 붙여도 거부되므로 실행 플래그·뷰포트 에뮬레이션이 원인이 아닙니다.
- 그래서 `cdp.py`는 `*.enable` 계열 명령을 보내지 않습니다 (테스트가 이를 강제합니다).

### 브라우저 실행 시 주의한 점

- `--remote-debugging-port=0`(자동 배정)으로 띄우면 `navigator.webdriver`가 `true`가 됩니다. 그래서 비어 있는 포트를 골라 고정 포트로 실행합니다.
- headless Chrome은 User-Agent에 `HeadlessChrome`을 싣고, DynaPath는 이 문자열을 거부합니다. CDP 재정의(Emulation) 없이 `--user-agent` 실행 인자로 일반 Chrome과 같은 값을 넘깁니다.
- 디버그 포트는 `127.0.0.1`에만 열리며 실행 중에만 존재합니다. 같은 PC의 다른 프로세스는 그동안 접속할 수 있습니다.

## 감사의 말

- [SRT](https://github.com/ryanking13/SRT) by ryanking13 (MIT License)
- [korail2](https://github.com/carpedm20/korail2) by carpedm20 (BSD License)
- [srtgo](https://github.com/lapis42/srtgo) by lapis42 — 원본 프로젝트
