# 📈 일일 투자 분석 보고서 + 텔레그램 포트폴리오 관리

매일 아침 Claude AI가 거시경제·기술적 지표·내부자 거래를 분석해 이메일로 보고서를 발송하고,  
텔레그램 봇으로 포트폴리오(매수/매도/현금)를 실시간으로 관리하는 시스템입니다.

---

## 목차

1. [시스템 구성](#시스템-구성)
2. [사전 준비](#사전-준비)
3. [로컬 설치 및 실행](#로컬-설치-및-실행)
4. [portfolio.json 설명](#portfoliojson-설명)
5. [텔레그램 봇 사용법](#텔레그램-봇-사용법)
6. [GitHub Actions 자동화 설정](#github-actions-자동화-설정)

---

## 시스템 구성

| 파일 | 역할 |
|------|------|
| `investment_report.py` | 매일 07:00 자동 실행 — 데이터 수집 → Claude 분석 → 이메일 발송 |
| `telegram_bot.py` | 텔레그램 봇 — 포트폴리오 매수/매도/현금 관리 |
| `portfolio.json` | 포트폴리오 상태 저장 파일 (봇이 읽고 씀) |
| `.github/workflows/daily_report.yml` | GitHub Actions 스케줄러 |

---

## 사전 준비

### 1. Anthropic API 키

[console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key

### 2. Gmail 앱 비밀번호

Google 계정 → 보안 → 2단계 인증 활성화 → 앱 비밀번호 생성 (메일 + Mac/Windows 선택)

### 3. 텔레그램 봇 토큰 발급

1. 텔레그램에서 **@BotFather** 검색
2. `/newbot` 명령어 입력
3. 봇 이름과 username 입력 (username은 `bot`으로 끝나야 함)
4. 발급된 **HTTP API 토큰** 저장 (예: `7123456789:AAHmxyz...`)

### 4. 텔레그램 Chat ID 확인

1. 발급한 봇에게 아무 메시지 전송
2. 아래 URL에서 `chat.id` 값 확인:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. `"chat":{"id": 123456789}` — 이 숫자가 Chat ID

---

## 로컬 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정 (또는 .env 파일 사용)
export ANTHROPIC_API_KEY="sk-ant-api03-..."
export GMAIL_ADDRESS="your@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export RECIPIENT_EMAIL="your@gmail.com"
export TELEGRAM_BOT_TOKEN="7123456789:AAHmxyz..."
export TELEGRAM_CHAT_ID="123456789"

# 보고서 즉시 실행
python3 investment_report.py

# 텔레그램 봇 실행 (별도 터미널)
python3 telegram_bot.py
```

---

## portfolio.json 설명

포트폴리오 상태를 저장하는 파일입니다. 텔레그램 봇이 매수/매도 시 자동으로 업데이트합니다.

```json
{
  "cash": { "krw": 13585062, "usd": 3566 },
  "category1": [
    { "ticker": "GOOGL", "name": "알파벳A", "shares": 2.77, "avg_price": 325.10,
      "currency": "USD", "daily_buy": 25 }
  ],
  "category2": [
    { "ticker": "068760.KS", "name": "셀트리온제약", "shares": 398,
      "avg_price": 68287, "currency": "KRW" }
  ],
  "category3": [],
  "category3_cash": 5000000,
  "last_updated": "2026-05-26"
}
```

| 필드 | 설명 |
|------|------|
| `cash.krw` | 일반 보유 원화 현금 |
| `cash.usd` | 일반 보유 달러 현금 |
| `category1` | 매일 자동 적립 종목 (`daily_buy` = 1일 적립금 달러) |
| `category2` | 일반 보유 종목 |
| `category3` | 500만원 프로젝트 보유 종목 |
| `category3_cash` | 500만원 프로젝트 현금 시드 |

**currency 규칙**: 달러 종목은 `"USD"` (avg_price/daily_buy 모두 달러 기준), 한국 종목은 `"KRW"`.

---

## 텔레그램 봇 사용법

### 매수

```
매수 [종목명 또는 티커] [수량] [단가]
```

- 한국어 이름, 영어 티커 모두 인식
- 매수 후 현금 자동 차감
- 평단가 자동 재계산 (수량 가중 평균)
- 잔고가 낮으면 확인 메시지 출력

```
매수 NVDA 5 220
매수 엔비디아 5 220
매수 카카오 10 42000
매수 한미반도체 1 320000
```

### 매도

```
매도 [종목명 또는 티커] [수량] [단가]
```

- 매도 후 현금 자동 추가

```
매도 GOOGL 1 400
매도 셀트리온제약 50 52000
```

### 현금 설정

```
현금 원화 15000000
현금 달러 5000
```

### 잔고 조회

```
잔고
```

### 포트폴리오 전체 조회

```
포트폴리오
```

### 카테고리3 시드 변경

```
카테고리3시드 6000000
```

### 도움말

```
도움말
```

---

## GitHub Actions 자동화 설정

### 1. Secrets 등록

GitHub 리포지토리 → Settings → Secrets and variables → Actions → New repository secret

| Secret 이름 | 값 |
|-------------|-----|
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `GMAIL_ADDRESS` | Gmail 주소 |
| `GMAIL_APP_PASSWORD` | Gmail 앱 비밀번호 |
| `RECIPIENT_EMAIL` | 수신 이메일 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 텔레그램 Chat ID |

### 2. 실행 스케줄

- **자동 실행**: 매일 UTC 22:00 (= KST 07:00)
- **수동 실행**: GitHub → Actions 탭 → `Daily Investment Report` → `Run workflow`

### 3. portfolio.json과 GitHub Actions

GitHub Actions는 매 실행 시 리포지토리를 새로 체크아웃하므로, `portfolio.json`은  
**로컬 봇이 변경한 내용이 GitHub에 푸시되어 있어야** Actions에 반영됩니다.

텔레그램 봇으로 포트폴리오를 수정한 후 커밋/푸시하는 것을 권장합니다:

```bash
git add portfolio.json
git commit -m "포트폴리오 업데이트"
git push
```
