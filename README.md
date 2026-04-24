# Polymarket HK Weather Tracker

홍콩 천문대(HKO) 기온을 실시간 추적해 **온도가 바뀔 때마다 Telegram 으로 알림**을 보내고, 같은 메시지에 **Polymarket 의 "Highest temperature in Hong Kong on {date}" 이벤트 버킷 확률**을 같이 얹어주는 봇입니다. 관측치·예측치·버킷 스냅샷은 SQLite 에 축적되어 나중에 분석할 수 있습니다.

## 기능 요약

| 영역 | 동작 |
|---|---|
| HKO 폴링 | `/wxinfo/json/one_json.xml` 을 기본 10초마다 호출, `If-None-Match`(ETag) 조건부 GET 으로 변화 없을 땐 `304 Not Modified`(바디 0바이트, ~60ms) 단락 — 서버 `Cache-Control: max-age=30` 존중 |
| 알림 | `hko.Temperature` 가 직전 bulletin 대비 바뀌면(≥0.1°C) Telegram 발송 |
| 피크 예측 | 최근 1h 기울기 + HKO 예보 최고 + HKT 13:00–16:30 창으로 ETA 추정, 피크 90분+ 하락 시 **confirmed** |
| Polymarket 버킷 | Gamma API 로 오늘의 HK **highest + lowest** 두 이벤트를 병렬 조회, 각 11개 버킷 Yes 확률·볼륨을 메시지·DB 에 반영. 현재 최고/최저 기온이 속한 버킷은 `← today's high` / `← today's low`, 각 이벤트 최다 확률 버킷은 `★` 로 표시 |
| 일일 요약 | 매일 23:55 HKT 에 오늘 최고/최저/수신건수 1회 발송 (idempotent) |
| 명령 | `/status` `/today` `/markets` `/forecast` `/stats` `/help` |
| 저장 | SQLite 3-테이블 (`readings`, `notifications`, `market_snapshots`) 영속화 |
| 운영 | Docker / docker-compose, 자동 재시작, `SIGTERM` 정상 종료 |

## 빠른 시작

```bash
cd polymarket-hongkong-tracker
cp .env.example .env           # 이미 값이 채워져 있으면 스킵
vim .env                        # 토큰/채팅ID 확인
docker compose up -d --build
docker compose logs -f hko-tracker
```

기동 직후 Telegram 에 `🟢 HK weather tracker online.` + 도움말이 도착합니다. 그 이후 HKO bulletin 이 갱신되고 기온이 바뀔 때마다 자동 알림이 옵니다. 즉시 확인하려면 봇에 `/status` 전송.

## 필수 사전 준비물

- Docker 20+ (`docker compose` v2)
- Telegram bot token 과 봇과 대화한 개인 chat id
  - BotFather 로 토큰 발급
  - 봇에게 `/start` 한 뒤 `https://api.telegram.org/bot<TOKEN>/getUpdates` 에서 `chat.id` 확인

## 환경 변수 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(필수)* | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | *(필수)* | 봇이 메시지를 보낼 chat id (다른 chat 의 명령은 무시) |
| `POLL_INTERVAL_SECONDS` | `10` | HKO 폴링 주기. ETag 캐싱 덕분에 변화 없으면 304 로 비용 최소화 |
| `TEMP_CHANGE_THRESHOLD` | `0.0` | `0.0` 이면 HKO 해상도(0.1°C) 모든 변화(상승·하강 양방향) 알림. `0.5` 로 올리면 큰 변화만 |
| `NOTIFY_DAILY_SUMMARY_HKT` | `21:00` | 일일 요약 발송 시각 (HKT, `HH:MM`) |
| `POLYMARKET_ENABLED` | `true` | `false` 로 끄면 버킷 섹션 생략 |
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` | Gamma API 엔드포인트 |
| `POLYMARKET_EVENT_SLUG_HIGH` | *(빈값)* | highest 이벤트 슬러그 강제 지정 (기본: HKT 날짜 자동 유도). 구 `POLYMARKET_EVENT_SLUG` 는 back-compat 으로 여기에 매핑됨 |
| `POLYMARKET_EVENT_SLUG_LOW` | *(빈값)* | lowest 이벤트 슬러그 강제 지정 |
| `HKO_URL` | `https://www.weather.gov.hk/wxinfo/json/one_json.xml` | HKO 엔드포인트 |
| `DB_PATH` | `/data/hko.db` | SQLite 경로 (컨테이너 내부, `./data` 볼륨에 영속화) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |

## Telegram 명령

| 명령 | 설명 |
|---|---|
| `/status` | 현재 HKO 기온 + 오늘 최고/최저/예보/피크추정/Polymarket 버킷 |
| `/today` | DB 기준 오늘 요약 + 피크추정 + 버킷 |
| `/history [N]` | DB 에 쌓인 최근 N개(기본 15, 최대 50) 온도 변화 이벤트 (시간·이전→현재·Δ) |
| `/markets` | Polymarket 오늘 이벤트 버킷만 |
| `/forecast` | HKO 9일 예보 |
| `/stats` | 누적 레코드 수 / 추적 시작일 / DB 경로 |
| `/help` | 명령 목록 |

`/start` 는 `/help` 와 동일합니다. `TELEGRAM_CHAT_ID` 이외 chat 에서 온 명령은 로그에만 남고 무시합니다.

## 메시지 예시

```
🌡 HK Temp: 23.8°C (↑ +0.3°C)
2026-04-24 14:30 HKT · bulletin 202604241430

Today so far
  ▲ High: 23.8°C @ 14:30
  ▼ Low:  19.2°C @ 06:15
  Forecast: 19–24°C (HKO)

Peak estimate [estimated]
  Rising at +0.40°C/hr. Estimated peak ~24.0°C near 14:45 HKT.

Humidity: 84%

🎯 Polymarket buckets · vol $192.2k
≤18°C    0.1%
 19°C    0.1%
 20°C    0.1%
 21°C    0.3%
 22°C    0.5%
 23°C   67.5%  ★
 24°C   26.0%  ← now
 25°C    4.2%
 26°C    3.8%
 27°C    1.4%
≥28°C    0.5%
```

- `★` = 현재 확률이 가장 높은 버킷
- `← now` = `round(오늘 최고기온)` 에 해당하는 버킷

## 데이터 모델

```
readings            bulletin 단위 원자료 (UNIQUE bulletin_time, raw_json 포함)
notifications       보낸 알림 (temp_change / daily_summary, daily_summary 는 날짜당 유니크)
market_snapshots    알림마다 Polymarket 11개 버킷 Yes/No/volume 스냅샷
```

SQLite 는 `./data/hko.db` 에 저장됩니다(Docker 볼륨). 백업은 컨테이너 정지 후 파일 복사 또는 핫-백업:

```bash
docker compose exec hko-tracker sqlite3 /data/hko.db ".backup /data/hko-$(date +%F).bak"
```

간단한 조회:

```bash
sqlite3 data/hko.db "SELECT bulletin_time, temperature FROM readings ORDER BY bulletin_time DESC LIMIT 20;"
sqlite3 data/hko.db "SELECT fetched_at_utc, temp, kind, yes_price FROM market_snapshots WHERE hkt_date='2026-04-24' ORDER BY fetched_at_utc DESC LIMIT 22;"
```

## 피크 예측 로직 요점

1. 오늘 모든 readings 중 `running_max` 계산.
2. 최근 60분 readings 에 선형회귀 → °C/h 기울기 `rate`.
3. 상태 판정:
   - `rate ≥ 0.1` → **rising**. 예보 최고 `forecast_max` 까지 ETA = `(target − cur)/rate`. 13:00–16:30 HKT 로 클램프.
   - `rate ≤ −0.1` 이면서 최고지점 이후 90분+ 경과 && 현 시간 ≥ 13:00 HKT → **peaked** (confirmed).
   - 그 외 하락 중 → **falling**.
   - 평탄 → **flat**.
4. 메시지 `[confidence]` 태그: `confirmed` / `estimated` / `forecast` / `none`.

로직은 `app/predictor.py` 의 `estimate_peak()` 한 함수에 집중돼 있습니다.

## Polymarket 통합

- Gamma API `GET /events?slug=...` 로 **highest + lowest 두 이벤트를 `asyncio.gather` 병렬 조회**.
- Slug 규칙: `{highest|lowest}-temperature-in-hong-kong-on-{month}-{day}-{year}` (HKT 날짜).
- 각 마켓의 `outcomePrices` JSON 문자열에서 Yes/No 확률을 추출, `volume` 은 달러 기준.
- 알림 발송 시마다 각 이벤트 11개 버킷씩 총 22행을 `market_snapshots` 에 insert.
- 어느 한 쪽이 아직 올라오지 않았거나 네트워크 오류면 해당 블록만 생략하고 나머지는 그대로 표시.
- 최고기온 → highest 이벤트 버킷(`← today's high`) / 최저기온 → lowest 이벤트 버킷(`← today's low`) 자동 하이라이트.

## 로컬 개발 (선택)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export $(grep -v '^#' .env | xargs)  # 또는 수동으로 export
export DB_PATH=./data/hko.db
python -m app.main
```

## 운영

```bash
docker compose up -d --build         # 시작 / 갱신
docker compose logs -f hko-tracker   # 로그 팔로우
docker compose restart hko-tracker   # 재시작 (DB 는 볼륨에 유지)
docker compose down                  # 정지 (데이터 유지)
docker compose down -v               # 볼륨까지 삭제 — 주의
```

컨테이너는 `SIGTERM` 시 알림 루프·명령 루프·일일요약 루프를 모두 취소하고 DB 를 닫은 뒤 종료합니다.

## 디렉토리 구조

```
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env / .env.example
├── README.md
├── data/                    # SQLite 볼륨 (gitignored)
└── app/
    ├── main.py              # 오케스트레이터 (poll / command / daily-summary 루프)
    ├── config.py            # env → Config
    ├── hko.py               # HKO JSON 파서 + HTTP 클라이언트
    ├── polymarket.py        # Gamma API 클라이언트 + 버킷 파서
    ├── db.py                # aiosqlite 스키마 + 질의
    ├── predictor.py         # 피크 추정 (선형회귀)
    └── telegram_bot.py      # Bot API sendMessage / getUpdates
```

## 트러블슈팅

- **메시지가 오지 않음** → 봇에게 먼저 `/start` 를 보내 채널을 열어두었는지 확인. `docker compose logs hko-tracker | grep telegram`.
- **Polymarket 블록이 빠져있음** → 오늘 날짜로 이벤트가 아직 생성되지 않은 경우. `POLYMARKET_EVENT_SLUG` 로 다른 이벤트를 고정하거나 그냥 무시하면 됩니다.
- **"Missing required env var"** → `.env` 의 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 값 확인.
- **파싱 오류 (`missing hko.BulletinTime`)** → HKO 엔드포인트 응답 포맷이 바뀐 경우. `app/hko.py` 의 `parse_reading()` 수정 필요.
- **중복 일일 요약** → `notifications` 의 partial unique index 로 하루 1회 보장. 재전송하려면 `DELETE FROM notifications WHERE kind='daily_summary' AND date_key='YYYY-MM-DD';`.
