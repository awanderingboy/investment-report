from dotenv import load_dotenv
load_dotenv()

import anthropic
import yfinance as yf
import smtplib
import schedule
import time
import os
import re
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html import unescape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# ── 설정 (환경변수 우선) ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL",    "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

US_TICKERS = ["NVDA", "GOOGL", "VOO", "FCX", "MSFT", "PLTR", "RGTI", "JOBY", "BEAM", "PWFL"]
KR_TICKERS = [
    "068760.KS",  # 셀트리온제약 (포트폴리오)
    "035720.KS",  # 카카오 (포트폴리오)
    "035420.KS",  # 네이버 (포트폴리오)
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    "012450.KS",  # 한화에어로스페이스
    "068270.KS",  # 셀트리온
    "042700.KS",  # 한미반도체
]
INSIDER_TICKERS = ["NVDA", "GOOGL", "FCX", "PLTR", "BEAM", "PWFL", "VOO"]
PORTFOLIO_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
SENT_REPORTS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sent_reports.json")
FEAR_GREED_CACHE   = "/tmp/fear_greed_cache.json"

_HARDCODED_PORTFOLIO = {
    "cash": {"krw": 13585062, "usd": 3566},
    "category1": [
        {"ticker": "GOOGL", "name": "알파벳A",      "shares": 2.77,  "avg_price": 325.10,  "currency": "USD", "daily_buy": 25},
        {"ticker": "FCX",   "name": "프리포트맥모란", "shares": 12.84, "avg_price": 61.53,   "currency": "USD", "daily_buy": 10},
        {"ticker": "VOO",   "name": "VOO",           "shares": 3.39,  "avg_price": 621.68,  "currency": "USD", "daily_buy": 20},
    ],
    "category2": [
        {"ticker": "068760.KS", "name": "셀트리온제약", "shares": 398,   "avg_price": 68287,  "currency": "KRW"},
        {"ticker": "035720.KS", "name": "카카오",       "shares": 104,   "avg_price": 36151,  "currency": "KRW"},
        {"ticker": "035420.KS", "name": "네이버",       "shares": 50,    "avg_price": 214000, "currency": "KRW"},
        {"ticker": "BEAM",      "name": "빔테라퓨틱스", "shares": 100,   "avg_price": 32.49,  "currency": "USD"},
        {"ticker": "NVDA",      "name": "엔비디아",     "shares": 10,    "avg_price": 178.84, "currency": "USD"},
        {"ticker": "PLTR",      "name": "팔란티어",     "shares": 11.64, "avg_price": 142.36, "currency": "USD"},
        {"ticker": "PWFL",      "name": "파워플리트",   "shares": 1200,  "avg_price": 3.68,   "currency": "USD"},
    ],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
SEC_HEADERS = {"User-Agent": "investment-report-bot/1.0 xogus5512@gmail.com"}


# ── 포트폴리오 로드 ──────────────────────────────────────────────────────────
DAILY_DCA = {
    "GOOGL": {"daily_usd": 25, "currency": "USD"},
    "FCX":   {"daily_usd": 10, "currency": "USD"},
    "VOO":   {"daily_usd": 20, "currency": "USD"},
}


def update_dca_portfolio(portfolio_data: dict, us_data: dict, exchange_rate: float) -> dict:
    """매일 적립 매수 금액 기준으로 portfolio.json 수량 자동 업데이트"""
    import datetime

    today = datetime.date.today()

    # 주말 또는 미국 공휴일이면 스킵
    if today.weekday() >= 5:  # 0=월 ~ 4=금, 5=토, 6=일
        print(f"  [DCA] 주말({today}) — 적립 스킵", flush=True)
        return portfolio_data

    # 이미 오늘 적립했는지 확인
    last_dca = portfolio_data.get("last_dca_date", "")
    if last_dca == str(today):
        print(f"  [DCA] 오늘({today}) 이미 적립 완료 — 스킵", flush=True)
        return portfolio_data

    pf = portfolio_data.copy()
    updated = []

    for it in pf.get("category1", []):
        ticker = it["ticker"]
        if ticker not in DAILY_DCA:
            continue

        dca = DAILY_DCA[ticker]
        daily_usd = dca["daily_usd"]

        # 현재가 가져오기
        price = us_data.get(ticker, {}).get("현재가")
        if not price or price <= 0:
            print(f"  [DCA] {ticker} 현재가 없음 — 적립 스킵", flush=True)
            continue

        # 매수 수량 계산 (소수점 6자리)
        shares_bought = round(daily_usd / price, 6)
        old_shares = it["shares"]
        old_avg = it["avg_price"]

        # 평단 재계산
        new_shares = round(old_shares + shares_bought, 6)
        new_avg = round((old_shares * old_avg + shares_bought * price) / new_shares, 4)

        it["shares"] = new_shares
        it["avg_price"] = new_avg

        # 달러 현금 차감
        pf["cash"]["usd"] = round(pf["cash"].get("usd", 0) - daily_usd, 2)

        updated.append(f"{ticker}: +{shares_bought}주 @ ${price} (${daily_usd})")
        print(f"  [DCA] {ticker}: +{shares_bought}주 @ ${price:.2f} (${daily_usd}/일)", flush=True)

    if updated:
        pf["last_dca_date"] = str(today)
        # portfolio.json 저장
        try:
            with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
                json.dump(pf, f, ensure_ascii=False, indent=2)
            # git push
            import subprocess
            repo = os.path.dirname(os.path.abspath(PORTFOLIO_FILE))
            subprocess.run(["git", "-C", repo, "add", "portfolio.json"], capture_output=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", f"auto: DCA 적립 {today} ({', '.join(updated)})"], capture_output=True)
            subprocess.run(["git", "-C", repo, "push"], capture_output=True)
            print(f"  [DCA] portfolio.json 저장 및 git push 완료", flush=True)
        except Exception as e:
            print(f"  [DCA] 저장 실패: {e}", flush=True)

    return pf


# ── 중복 발송 방지 (Send Guard) ───────────────────────────────────────────────
# 1단계: .sent_reports.json (로컬, GitHub Actions fresh runner에서 상태 미보존)
# 2단계: Gmail IMAP 검색 (원격 상태 — runner 재시작 후에도 유효)

def _today_kst() -> str:
    """KST 기준 오늘 날짜 문자열 반환 (YYYY-MM-DD)."""
    from datetime import timezone, timedelta as _td
    kst = timezone(_td(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%d")

def _now_kst():
    """KST 현재 시간 반환 (timezone-aware datetime)."""
    from datetime import timezone, timedelta as _td
    return datetime.now(tz=timezone(_td(hours=9)))

def _report_date_kst_str() -> str:
    """KST 기준 오늘 날짜 보고서 표시용 문자열. 예: 2026년 06월 03일"""
    return _now_kst().strftime("%Y년 %m월 %d일")

def _report_timestamp_kst_str() -> str:
    """KST 기준 현재 시각 보고서 표시용 문자열. 예: 2026-06-03 07:17 KST"""
    return _now_kst().strftime("%Y-%m-%d %H:%M KST")

def get_market_data_label(market: str, data_date_str: str, now_kst) -> str:
    """주말/휴장일 여부에 따라 적절한 데이터 기준일 레이블을 반환한다."""
    import calendar as _cal
    _US_HOLIDAYS = {  # NYSE 주요 휴장일 (월-일 기준 간략 목록)
        (1, 1), (7, 4), (12, 25), (11, 11), (5, 30),
    }
    try:
        from datetime import datetime as _dt2
        _d = _dt2.strptime(data_date_str, "%Y-%m-%d")
        _weekday = _d.weekday()  # 0=월 … 6=일
        _is_weekend = _weekday >= 5
        _md = (_d.month, _d.day)
        _is_holiday = _md in _US_HOLIDAYS
    except Exception:
        _is_weekend = False
        _is_holiday = False

    if market.upper() in ("US", "USD", "미국"):
        if _is_weekend or _is_holiday:
            return f"미국 주식: 최근 정규장 종가 기준 — {data_date_str}"
        return f"미국 주식: 정규장 종가/장중 데이터 — {data_date_str} 기준"
    if market.upper() in ("KR", "KRW", "한국"):
        if _is_weekend or _is_holiday:
            return f"한국 주식: 최근 정규장 종가 기준 — {data_date_str}"
        return f"한국 주식: 전일 종가 기준 — {data_date_str}"
    return f"{market}: 최근 정규장 종가 기준 — {data_date_str}"


def validate_candidate_metrics(candidate: dict) -> tuple:
    """신규 수익 발굴 후보의 핵심 수치를 검증한다.

    candidate 예시:
      {"name": "AVGO", "current_price": 200.0, "stop_price": 180.0,
       "target_price": 240.0, "rr": 2.0, "data_source_count": 2}

    반환: (is_valid: bool, issues: list[str])
    """
    issues = []
    cp  = candidate.get("current_price")
    sp  = candidate.get("stop_price")
    tp  = candidate.get("target_price")
    rr  = candidate.get("rr")
    src = candidate.get("data_source_count", 2)

    if cp is None or cp <= 0:
        issues.append("현재가 미수집 — 후보 제외")
    if sp is not None and cp is not None and sp >= cp:
        issues.append("손절가 >= 현재가 — A/B 후보 금지")
    if tp is not None and cp is not None and tp <= cp:
        issues.append("목표가 <= 현재가 — A/B 후보 금지")
    if rr is None:
        issues.append("RR 계산 불가 — A/B 후보 금지")
    elif rr < 1.5:
        issues.append(f"RR {rr:.2f} < 1.5 — A/B 후보 부적합")
    if src <= 1:
        issues.append("단일 소스 — C 관찰만 허용, A/B 금지")

    # 비정상 가격 변동 탐지
    chg_1m = candidate.get("chg_1m")  # 1개월 등락률 (%)
    chg_6m = candidate.get("chg_6m")  # 6개월 등락률 (%)
    if chg_1m is not None and abs(chg_1m) > 50:
        issues.append(f"1개월 변동 {chg_1m:+.0f}% — 교차검증 필요")
    if chg_6m is not None and chg_6m > 100:
        issues.append(f"6개월 수익률 {chg_6m:+.0f}% — 강한 추세 또는 교차검증 필요")

    return (len(issues) == 0, issues)


def _load_sent_reports() -> dict:
    try:
        with open(SENT_REPORTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_sent_reports(data: dict) -> None:
    with open(SENT_REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def already_sent_today(report_type: str = "daily") -> bool:
    """FORCE_SEND_REPORT=true이면 항상 False를 반환해 재발송을 허용한다."""
    if os.environ.get("FORCE_SEND_REPORT", "").lower() == "true":
        return False
    data = _load_sent_reports()
    today = _today_kst()
    entry = data.get(today, {})
    return entry.get("report_type") == report_type and entry.get("status") == "sent"

def mark_sent_today(report_type: str = "daily") -> None:
    from datetime import timezone, timedelta as _td
    kst = timezone(_td(hours=9))
    sent_at = datetime.now(tz=kst).isoformat(timespec="seconds")
    data = _load_sent_reports()
    data[_today_kst()] = {"sent_at": sent_at, "report_type": report_type, "status": "sent"}
    _save_sent_reports(data)

# ── Gmail IMAP 중복 체크 ──────────────────────────────────────────────────────

def _today_report_subject_kst() -> str:
    """KST 기준 오늘 날짜로 이메일 제목 키워드 반환."""
    from datetime import timezone, timedelta as _td
    kst = timezone(_td(hours=9))
    today_str = datetime.now(tz=kst).strftime("%Y년 %m월 %d일")
    return f"투자 분석 보고서 — {today_str}"

def _imap_date_kst() -> str:
    """IMAP SINCE 쿼리용 날짜 문자열 반환 (DD-Mon-YYYY)."""
    from datetime import timezone, timedelta as _td
    import calendar
    kst = timezone(_td(hours=9))
    now = datetime.now(tz=kst)
    mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][now.month - 1]
    return f"{now.day:02d}-{mon}-{now.year}"

def _decode_email_subject(raw_subject) -> str:
    """email.header.decode_header로 제목을 디코딩한다. 인코딩 오류는 replace로 처리."""
    import email.header as _eh
    if not raw_subject:
        return ""
    parts = []
    for decoded, charset in _eh.decode_header(raw_subject):
        if isinstance(decoded, bytes):
            try:
                parts.append(decoded.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(decoded.decode("utf-8", errors="replace"))
        else:
            parts.append(str(decoded))
    return "".join(parts)

def _mailbox_has_today_report_by_header(imap, mailbox: str, today_date_str: str) -> bool:
    """
    IMAP SUBJECT 검색 대신 SINCE 날짜 검색 + RFC822.HEADER fetch + Python 비교 방식.
    한글/이모지 제목의 ASCII 인코딩 문제를 우회한다.
    """
    import email as _email_mod
    try:
        status, _ = imap.select(mailbox, readonly=True)
        if status != "OK":
            print(f"  [SEND-GUARD][WARN] mailbox 선택 실패: {mailbox}", flush=True)
            return False
        # SINCE만 사용 — ASCII-safe, SUBJECT는 Python에서 비교
        _, data = imap.search(None, "SINCE", _imap_date_kst())
        if not data or not data[0]:
            print(f"  [SEND-GUARD] checked {mailbox}: 0 messages", flush=True)
            return False
        msg_ids = data[0].split()[-50:]  # 최근 50개까지만 확인
        print(f"  [SEND-GUARD] checked {mailbox}: {len(msg_ids)} messages", flush=True)
        for mid in msg_ids:
            try:
                _, hdr_data = imap.fetch(mid, "(RFC822.HEADER)")
                if not hdr_data or not hdr_data[0]:
                    continue
                raw = hdr_data[0][1] if isinstance(hdr_data[0], tuple) else hdr_data[0]
                msg = _email_mod.message_from_bytes(raw)
                subject = _decode_email_subject(msg.get("Subject", ""))
                if "투자 분석 보고서" in subject and today_date_str in subject:
                    print(f"  [SEND-GUARD] Gmail duplicate found in {mailbox}: {subject}", flush=True)
                    return True
            except Exception as e:
                print(f"  [SEND-GUARD][WARN] fetch 실패 mid={mid}: {e}", flush=True)
                continue
        return False
    except Exception as e:
        print(f"  [SEND-GUARD][WARN] mailbox={mailbox} 검색 실패: {e}", flush=True)
        return False

def gmail_report_exists_today(report_type: str = "daily") -> bool:
    """
    Gmail IMAP으로 오늘 보고서가 이미 발송됐는지 확인한다.
    SINCE 날짜 검색 + 헤더 fetch + Python 제목 비교 방식으로 한글/이모지 인코딩 문제를 우회한다.
    FORCE_SEND_REPORT=true이면 체크를 우회해 False를 반환한다.
    IMAP 연결/검색 실패 시 False를 반환해 발송을 허용한다.
    """
    import imaplib
    if os.environ.get("FORCE_SEND_REPORT", "").lower() == "true":
        return False
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  [SEND-GUARD][WARN] Gmail 자격증명 없음 — IMAP 중복 체크 스킵", flush=True)
        return False

    # KST 오늘 날짜 문자열 — 제목 비교 기준
    from datetime import timezone, timedelta as _td
    _kst = timezone(_td(hours=9))
    today_date_str = datetime.now(tz=_kst).strftime("%Y년 %m월 %d일")

    # mailbox 후보: 영문명 우선, 한글명은 계정 언어에 따라 다를 수 있으므로 실패 허용
    mailboxes = [
        "INBOX",
        "[Gmail]/Sent Mail",
        "[Gmail]/All Mail",
        "[Gmail]/보낸편지함",
        "[Gmail]/전체보관함",
    ]
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        for mb in mailboxes:
            if _mailbox_has_today_report_by_header(imap, mb, today_date_str):
                imap.logout()
                return True
        imap.logout()
        return False
    except Exception as e:
        print(f"  [SEND-GUARD][WARN] Gmail duplicate check failed — allow send: {e}", flush=True)
        return False

# ─────────────────────────────────────────────────────────────────────────────

def load_portfolio() -> dict:
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            pf = json.load(f)
        print(f"  portfolio.json 로드 완료 (최종 수정: {pf.get('last_updated', '?')})", flush=True)
        return pf
    except FileNotFoundError:
        print("  portfolio.json 없음, 하드코딩 기본값 사용", flush=True)
        return _HARDCODED_PORTFOLIO
    except Exception as e:
        print(f"  portfolio.json 로드 실패 ({e}), 기본값 사용", flush=True)
        return _HARDCODED_PORTFOLIO


def _portfolio_section_str(pf: dict, exchange_rate: float = None) -> str:
    lines = ["[포트폴리오 카테고리]\n"]

    lines.append("카테고리 1 - 주식 모으기 중 (매일 자동 적립, 절대 단기 매도 금지):")
    for it in pf.get("category1", []):
        avg = f"${it['avg_price']:.2f}" if it["currency"] == "USD" else f"{it['avg_price']:,.0f}원"
        daily = f" / 매일 ${it['daily_buy']} 적립" if it.get("daily_buy") else ""
        lines.append(f"- {it['name']}({it['ticker']}): 평단 {avg} / {it['shares']}주{daily}")

    lines.append("\n카테고리 2 - 현재 보유 중 (적립 없음, 매매 판단 필요):")
    for it in pf.get("category2", []):
        avg = f"${it['avg_price']:.2f}" if it["currency"] == "USD" else f"{it['avg_price']:,.0f}원"
        lines.append(f"- {it['name']}({it['ticker']}): 평단 {avg} / {it['shares']}주")

    cash = pf.get("cash", {})
    krw  = cash.get("krw", 0)
    usd  = cash.get("usd", 0)
    lines.append(f"\n보유 현금: {krw:,}원 + ${usd:,}")
    return "\n".join(lines)


def _restricted_tickers_list(pf: dict) -> str:
    names = []
    for it in pf.get("category1", []) + pf.get("category2", []):
        names.append(it["name"] if it["currency"] == "KRW" else it["ticker"])
    return ", ".join(names)


# ── 환율 조회 ─────────────────────────────────────────────────────────────────
def get_exchange_rate():
    print("  환율(KRW/USD) 조회 중...", flush=True)
    try:
        krw = yf.Ticker("KRW=X")
        rate = krw.fast_info.last_price
        if rate and rate > 0:
            print(f"  환율: {rate:.1f}원/달러", flush=True)
            return round(rate, 1)
    except Exception as e:
        print(f"  환율 조회 실패: {e}", flush=True)
    print("  환율 폴백 적용: 1,380원/달러", flush=True)
    return 1380.0


# ── 거시경제 지표 수집 ─────────────────────────────────────────────────────────
def get_macro_data():
    print("  거시경제 지표 수집 중...", flush=True)
    macro_tickers = {
        "VIX":  "^VIX",
        "DXY":  "DX-Y.NYB",
        "TNX":  "^TNX",
        "WTI":  "CL=F",
        "Gold": "GC=F",
    }
    _range_bounds = {
        "VIX":  (10,    80),
        "DXY":  (85,   115),
        "WTI":  (50,   120),
        "Gold": (1800, 5500),
        "TNX":  (1.0,  7.0),   # 10년물 금리 정상 범위 (%)
    }
    _fallback_urls = {
        "WTI":  "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1d&range=5d",
        "Gold": "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=5d",
    }
    results = {}
    def _fetch_yahoo_json(url):
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            closes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                return closes
        return []

    for name, ticker in macro_tickers.items():
        current = None
        close_series = []

        # 1순위: yfinance
        try:
            t = yf.Ticker(ticker)
            fetch_period = "2d" if name in ("WTI", "Gold") else "5d"
            hist = t.history(period=fetch_period, auto_adjust=True)
            if not hist.empty:
                close = hist["Close"].astype(float).dropna()
                if not close.empty:
                    current = float(close.iloc[-1])
                    close_series = close.tolist()
        except Exception as e:
            print(f"  [{name}] yfinance 실패: {e}", flush=True)

        # 범위 검증 (yfinance 값) — 이탈 시 폴백 준비
        _range_failed = False
        if current is not None and name in _range_bounds:
            lo, hi = _range_bounds[name]
            if not (lo <= current <= hi):
                print(f"  ⚠️ [{name}] yfinance 범위 이탈 ({round(current, 2)}) — Yahoo JSON 폴백 시도", flush=True)
                current = None
                close_series = []
                _range_failed = True

        # 2순위: Yahoo Finance JSON API (WTI/Gold — 수집 실패 또는 범위 이탈 시)
        if current is None and name in _fallback_urls:
            try:
                closes = _fetch_yahoo_json(_fallback_urls[name])
                if closes:
                    current = float(closes[-1])
                    close_series = closes
                    print(f"  [{name}] Yahoo JSON API 폴백 사용", flush=True)
            except Exception as e:
                print(f"  [{name}] Yahoo JSON API 폴백 실패: {e}", flush=True)

        if current is None:
            print(f"  [{name}] 데이터 없음", flush=True)
            results[name] = None
            continue

        # 범위 검증 (최종)
        if name in _range_bounds:
            lo, hi = _range_bounds[name]
            if not (lo <= current <= hi):
                print(f"  ⚠️ [{name}] 범위 이탈 ({round(current, 2)}) — None 처리", flush=True)
                if name == "TNX":
                    print(f"  [MACRO][WARN] TNX out of expected range: {round(current, 2)}", flush=True)
                    results["TNX_WARN"] = "⚠️ 10년물 금리 데이터 이상치 — 금리 기반 판단 강도 낮춤"
                results[name] = None
                continue

        change_pct = round((current / close_series[-2] - 1) * 100, 2) if len(close_series) >= 2 else None
        results[name] = {"value": round(current, 2), "change_pct": change_pct}
        chg_str = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
        print(f"  [{name}] {round(current, 2)}{chg_str}", flush=True)
    return results


# ── 뉴스 수집 ─────────────────────────────────────────────────────────────────
def get_news_data(tickers: list, portfolio_data: dict = None) -> dict:
    print("  뉴스 데이터 수집 중...", flush=True)
    results = {}

    # 1. 보유 종목별 뉴스 수집
    us_tickers = [t for t in tickers if not (t.endswith(".KS") or t.endswith(".KQ"))]
    kr_tickers = [t for t in tickers if t.endswith(".KS") or t.endswith(".KQ")]

    # portfolio.json에서 종목명 자동 매핑
    ticker_name_map = {}
    if portfolio_data:
        for cat in ["category1", "category2"]:
            for it in portfolio_data.get(cat, []):
                ticker_name_map[it["ticker"]] = it["name"]

    # 미국 주식 뉴스 (Google RSS 1순위, Yahoo RSS 2순위 — Yahoo rate limit 회피)
    for ticker in us_tickers:
        news = []
        urls = [
            f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
        ]
        for url in urls:
            if news:
                break
            try:
                resp = requests.get(url, headers=HEADERS, timeout=8)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.findall(".//item")[:3]:
                        title = item.findtext("title", "").strip()
                        pubdate = item.findtext("pubDate", "").strip()
                        if title:
                            news.append(f"[{pubdate[:16]}] {title}")
                elif resp.status_code == 429:
                    import time as _time
                    _time.sleep(2)
            except Exception:
                pass

        if news:
            results[ticker] = news
            print(f"    [{ticker}] 뉴스 {len(news)}건", flush=True)
        else:
            results[ticker] = ["최신 뉴스 수집 실패"]
            print(f"    [{ticker}] 수집 실패", flush=True)

    # 한국 주식 뉴스
    for ticker in kr_tickers:
        news = []
        code = ticker.split(".")[0]
        try:
            url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
            resp = requests.get(url, headers={**HEADERS, "Referer": "https://finance.naver.com"}, timeout=8)
            if resp.status_code == 200 and BS4_AVAILABLE:
                soup = BeautifulSoup(resp.text, "html.parser")
                items = soup.select("table.type5 td.title a")[:3]
                news = [item.get_text(strip=True) for item in items if item.get_text(strip=True)]
        except Exception:
            pass

        if news:
            results[ticker] = news
            print(f"    [{ticker}] 뉴스 {len(news)}건", flush=True)
        else:
            results[ticker] = ["최신 뉴스 수집 실패"]

    # 2. 섹터별 뉴스 수집 (신규 종목 발굴용)
    sector_feeds = {
        "AI_반도체": [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,AMD,INTC,QCOM,AVGO&region=US&lang=en-US",
            "https://news.google.com/rss/search?q=AI+semiconductor+stock&hl=en-US&gl=US&ceid=US:en",
        ],
        "양자컴퓨팅": [
            "https://news.google.com/rss/search?q=quantum+computing+stock+2026&hl=en-US&gl=US&ceid=US:en",
        ],
        "UAM_항공": [
            "https://news.google.com/rss/search?q=eVTOL+UAM+FAA+certification&hl=en-US&gl=US&ceid=US:en",
        ],
        "바이오_유전자": [
            "https://news.google.com/rss/search?q=CRISPR+gene+therapy+FDA+2026&hl=en-US&gl=US&ceid=US:en",
        ],
        "한국_반도체": [
            "https://news.google.com/rss/search?q=SK하이닉스+HBM+삼성전자+반도체&hl=ko&gl=KR&ceid=KR:ko",
        ],
        "한국_방산": [
            "https://news.google.com/rss/search?q=한화에어로스페이스+한국방산+수주&hl=ko&gl=KR&ceid=KR:ko",
        ],
        "글로벌_시장": [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC,^DJI,^IXIC&region=US&lang=en-US",
        ],
    }

    print("  섹터별 뉴스 수집 중...", flush=True)
    for sector, urls in sector_feeds.items():
        sector_news = []
        for url in urls:
            if len(sector_news) >= 5:
                break
            try:
                resp = requests.get(url, headers=HEADERS, timeout=8)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.findall(".//item")[:5]:
                        title = item.findtext("title", "").strip()
                        pubdate = item.findtext("pubDate", "").strip()
                        if title and title not in sector_news:
                            sector_news.append(f"[{pubdate[:16]}] {title}")
            except Exception:
                pass

        if sector_news:
            results[f"__sector_{sector}"] = sector_news
            print(f"    [섹터:{sector}] 뉴스 {len(sector_news)}건", flush=True)

    return results


# ── 실적 수집 ─────────────────────────────────────────────────────────────────
def get_earnings_data(tickers: list) -> dict:
    print("  실적 데이터 수집 중...", flush=True)
    results = {}
    us_tickers = [t for t in tickers if not (t.endswith(".KS") or t.endswith(".KQ"))]
    for ticker in us_tickers:
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            next_date = None
            if cal is not None:
                if hasattr(cal, 'get'):
                    next_date = cal.get("Earnings Date")
                elif hasattr(cal, 'columns') and "Earnings Date" in cal.columns:
                    next_date = cal["Earnings Date"].iloc[0] if len(cal) > 0 else None
            fin = stock.quarterly_financials
            entry = {}
            if next_date is not None:
                if hasattr(next_date, '__iter__') and not isinstance(next_date, str):
                    next_date = list(next_date)[0]
                entry["다음실적발표"] = str(next_date)[:10]
            if fin is not None and not fin.empty:
                if "Total Revenue" in fin.index:
                    rev = fin.loc["Total Revenue"].iloc[0]
                    entry["최근분기매출"] = f"${rev/1e9:.2f}B" if rev else "N/A"
                if "Net Income" in fin.index:
                    ni = fin.loc["Net Income"].iloc[0]
                    entry["최근분기순이익"] = f"${ni/1e9:.2f}B" if ni else "N/A"
            if entry:
                results[ticker] = entry
                print(f"    [{ticker}] 실적 수집 완료", flush=True)
        except Exception as e:
            print(f"    [{ticker}] 실적 수집 실패: {e}", flush=True)
    return results


# ── 백테스팅 ──────────────────────────────────────────────────────────────────
def run_backtest(portfolio_data: dict, stock_data_all: dict, exchange_rate: float) -> str:
    print("  백테스팅 실행 중...", flush=True)
    lines = ["[백테스팅 — 보유 종목 vs VOO 벤치마크]"]
    voo_data = stock_data_all.get("VOO", {})
    voo_1m = voo_data.get("1개월_수익률")
    voo_3m = voo_data.get("3개월_수익률")
    voo_6m = voo_data.get("6개월_수익률")
    if voo_1m:
        lines.append(f"  VOO 벤치마크: 1M {voo_1m:+.1f}% / 3M {voo_3m:+.1f}% / 6M {voo_6m:+.1f}%")
    all_holdings = (
        portfolio_data.get("category1", []) +
        portfolio_data.get("category2", [])
    )
    outperform = 0
    total_compared = 0
    for it in all_holdings:
        ticker = it["ticker"]
        avg_price = it["avg_price"]
        data = stock_data_all.get(ticker, {})
        current_price = data.get("현재가")
        if not current_price:
            continue
        total_return = (float(current_price) / float(avg_price) - 1) * 100
        m1 = data.get("1개월_수익률") or 0
        m3 = data.get("3개월_수익률") or 0
        m6 = data.get("6개월_수익률") or 0
        vs_voo_1m = m1 - (voo_1m or 0)
        beat = "✅초과" if vs_voo_1m > 0 else "❌미달"
        if voo_1m:
            total_compared += 1
            if vs_voo_1m > 0:
                outperform += 1
        lines.append(
            f"  {it['name']}({ticker}): 진입후 {total_return:+.1f}% | 1M {m1:+.1f}% vs VOO {voo_1m:+.1f}% → {beat}"
        )
    if total_compared > 0:
        lines.append(f"  포트폴리오 vs VOO 초과 성과: {outperform}/{total_compared} ({outperform/total_compared*100:.0f}%)")
    return "\n".join(lines)


# ── Fear & Greed Index ────────────────────────────────────────────────────────
def get_fear_greed():
    print("  Fear & Greed Index 수집 중...", flush=True)

    # 1순위: CNN
    try:
        resp = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            fg = resp.json().get("fear_and_greed", {})
            score = fg.get("score")
            rating = fg.get("rating", "")
            if score is not None:
                result = {"score": round(float(score), 1), "rating": rating, "source": "CNN"}
                with open(FEAR_GREED_CACHE, "w") as f:
                    json.dump(result, f)
                print(f"  Fear&Greed (CNN): {result['score']} / {result['rating']}", flush=True)
                return result
    except Exception as e:
        print(f"  Fear&Greed CNN 실패: {e}", flush=True)

    # 2순위: alternative.me
    try:
        resp = requests.get("https://api.alternative.me/fng/", headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            entry = resp.json()["data"][0]
            score = int(entry["value"])
            rating = entry["value_classification"]
            result = {"score": score, "rating": rating, "source": "alternative.me"}
            with open(FEAR_GREED_CACHE, "w") as f:
                json.dump(result, f)
            print(f"  Fear&Greed (alternative.me): {score} / {rating}", flush=True)
            return result
    except Exception as e:
        print(f"  Fear&Greed alternative.me 실패: {e}", flush=True)

    # 3순위: 캐시
    try:
        with open(FEAR_GREED_CACHE, "r") as f:
            cached = json.load(f)
        cached["cached"] = True
        print(f"  Fear&Greed 캐시 사용: {cached['score']} / {cached['rating']}", flush=True)
        return cached
    except Exception:
        print("  Fear&Greed 모든 소스 실패", flush=True)
        return None


# ── SEC 내부자 거래 수집 ──────────────────────────────────────────────────────
def get_insider_trades():
    print("  SEC 내부자 거래 수집 중...", flush=True)
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    all_trades = []

    for ticker in INSIDER_TICKERS:
        collected = False

        # 1순위: SEC EDGAR full-text search
        try:
            url = (
                f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
                f"&dateRange=custom&startdt={week_ago}&enddt={today}&forms=4"
            )
            resp = requests.get(url, headers=SEC_HEADERS, timeout=10)
            if resp.status_code == 200:
                hits = resp.json().get("hits", {}).get("hits", [])
                for hit in hits[:3]:
                    src = hit.get("_source", {})
                    names = src.get("display_names", [""])
                    all_trades.append({
                        "ticker": ticker,
                        "date": src.get("period_of_report", ""),
                        "name": names[0] if names else "",
                        "form": "Form 4",
                        "source": "SEC EDGAR",
                    })
                if hits:
                    print(f"  [{ticker}] EDGAR {len(hits)}건", flush=True)
                    collected = True
        except Exception as e:
            print(f"  [{ticker}] EDGAR 실패: {e}", flush=True)

        # 2순위: SEC RSS Atom
        if not collected:
            try:
                url = (
                    f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                    f"&CIK={ticker}&type=4&dateb=&owner=include&count=5&output=atom"
                )
                resp = requests.get(url, headers=SEC_HEADERS, timeout=10)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("atom:entry", ns)[:3]:
                        updated = entry.findtext("atom:updated", "", ns)[:10]
                        if updated >= week_ago:
                            all_trades.append({
                                "ticker": ticker,
                                "date": updated,
                                "name": entry.findtext("atom:title", "", ns),
                                "form": "Form 4",
                                "source": "SEC RSS",
                            })
                    print(f"  [{ticker}] SEC RSS 완료", flush=True)
                    collected = True
            except Exception as e:
                print(f"  [{ticker}] SEC RSS 실패: {e}", flush=True)

        # 3순위: OpenInsider
        if not collected and BS4_AVAILABLE:
            try:
                resp = requests.get(
                    f"https://openinsider.com/screener?s={ticker}&fd=7",
                    headers=HEADERS, timeout=10
                )
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    table = soup.find("table", {"class": "tinytable"})
                    if table:
                        for row in table.find_all("tr")[1:4]:
                            cols = row.find_all("td")
                            if len(cols) > 6:
                                all_trades.append({
                                    "ticker": ticker,
                                    "date": cols[1].text.strip(),
                                    "name": cols[3].text.strip(),
                                    "trade_type": cols[6].text.strip(),
                                    "form": "Form 4",
                                    "source": "OpenInsider",
                                })
                    print(f"  [{ticker}] OpenInsider 완료", flush=True)
            except Exception as e:
                print(f"  [{ticker}] OpenInsider 실패: {e}", flush=True)

    if not all_trades:
        print("  내부자 거래 수집 실패 (모든 소스)", flush=True)
        return None
    return all_trades[:20]


# ── 정치인 거래 수집 ──────────────────────────────────────────────────────────
def get_congress_trades():
    print("  정치인 거래 수집 중...", flush=True)

    # 1순위: Unusual Whales
    try:
        resp = requests.get(
            "https://api.unusualwhales.com/api/congress/trades",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            trades = data.get("data", data if isinstance(data, list) else [])[:10]
            if trades:
                print(f"  정치인 거래 (Unusual Whales): {len(trades)}건", flush=True)
                return trades
    except Exception as e:
        print(f"  Unusual Whales 실패: {e}", flush=True)

    # 2순위: Quiver Quant
    try:
        resp = requests.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            trades = data[:10] if isinstance(data, list) else []
            if trades:
                print(f"  정치인 거래 (Quiver Quant): {len(trades)}건", flush=True)
                return trades
    except Exception as e:
        print(f"  Quiver Quant 실패: {e}", flush=True)

    # 3순위: Capitol Trades 스크래핑
    if BS4_AVAILABLE:
        try:
            resp = requests.get(
                "https://www.capitoltrades.com/trades",
                headers=HEADERS, timeout=15
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                trades = []
                for row in soup.select("table tbody tr")[:10]:
                    cols = row.find_all("td")
                    if len(cols) >= 4:
                        trades.append({
                            "politician":  cols[0].text.strip(),
                            "ticker":      cols[1].text.strip(),
                            "trade_type":  cols[2].text.strip(),
                            "date":        cols[3].text.strip(),
                        })
                if trades:
                    print(f"  정치인 거래 (Capitol Trades): {len(trades)}건", flush=True)
                    return trades
        except Exception as e:
            print(f"  Capitol Trades 실패: {e}", flush=True)

    print("  정치인 거래 수집 실패 (모든 소스)", flush=True)
    return None


# ── Put/Call Ratio 수집 ───────────────────────────────────────────────────────
def get_put_call_ratio(vix_value=None):
    print("  Put/Call Ratio 수집 중...", flush=True)

    # 1순위: CBOE
    if BS4_AVAILABLE:
        try:
            resp = requests.get(
                "https://www.cboe.com/us/options/market_statistics/daily/",
                headers=HEADERS, timeout=10
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if cells and "total" in cells[0].text.lower():
                        try:
                            ratio = float(cells[-1].text.strip())
                            print(f"  Put/Call Ratio (CBOE): {ratio}", flush=True)
                            return {"ratio": ratio, "source": "CBOE"}
                        except Exception:
                            pass
        except Exception as e:
            print(f"  CBOE 실패: {e}", flush=True)

    # 2순위: MarketWatch
    if BS4_AVAILABLE:
        try:
            resp = requests.get(
                "https://www.marketwatch.com/investing/index/vix",
                headers=HEADERS, timeout=10
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup.find_all(string=lambda t: t and "put" in t.lower() and "call" in t.lower()):
                    try:
                        ratio = float(tag.parent.find_next("span").text.strip())
                        print(f"  Put/Call Ratio (MarketWatch): {ratio}", flush=True)
                        return {"ratio": ratio, "source": "MarketWatch"}
                    except Exception:
                        pass
        except Exception as e:
            print(f"  MarketWatch 실패: {e}", flush=True)

    # 3순위: VIX 기반 추정
    if vix_value is not None:
        if vix_value >= 30:
            ratio, interp = 1.2, "Put 우세 (VIX 공포 구간 기반 추정)"
        elif vix_value >= 20:
            ratio, interp = 0.9, "중립 (VIX 주의 구간 기반 추정)"
        else:
            ratio, interp = 0.7, "Call 우세 (VIX 안정 구간 기반 추정)"
        print(f"  Put/Call Ratio VIX 추정: {ratio}", flush=True)
        return {"ratio": ratio, "source": f"VIX 추정({vix_value})", "interpretation": interp}

    print("  Put/Call Ratio 수집 실패 (모든 소스)", flush=True)
    return None


# ── 데이터 포맷 헬퍼 ──────────────────────────────────────────────────────────
def _fmt_macro(macro_data):
    if not macro_data:
        return "⚠️ 거시경제 데이터 수집 실패"
    labels = {"VIX": "VIX(변동성)", "DXY": "DXY(달러인덱스)", "TNX": "10년 국채금리(%)", "WTI": "WTI 원유($/배럴)", "Gold": "금($/oz)"}
    lines = []
    for key, label in labels.items():
        val = macro_data.get(key)
        if val:
            chg = f" ({val['change_pct']:+.2f}%)" if val.get("change_pct") is not None else ""
            lines.append(f"- {label}: {val['value']}{chg}")
        else:
            lines.append(f"- {label}: ⚠️ 수집 실패")
    return "\n".join(lines)

def _fmt_fear_greed(fg):
    if not fg:
        return "⚠️ Fear & Greed 데이터 수집 실패 - 수동 확인 필요"
    cached = " (캐시 사용)" if fg.get("cached") else f" / 출처: {fg.get('source','')}"
    return f"점수: {fg['score']}/100 / 등급: {fg['rating']}{cached}"

def _fmt_insider(trades):
    if not trades:
        return "⚠️ 내부자 거래 데이터 수집 실패 - 수동 확인 필요"
    lines = []
    for t in trades[:10]:
        trade_type = t.get("trade_type", t.get("form", ""))
        lines.append(f"- [{t.get('ticker','')}] {t.get('date','')} / {t.get('name','')} / {trade_type} (출처: {t.get('source','')})")
    return "\n".join(lines)

def _fmt_congress(trades):
    if not trades:
        return "⚠️ 정치인 거래 데이터 수집 실패 - 수동 확인 필요"
    lines = []
    for t in trades[:10]:
        if isinstance(t, dict):
            politician = t.get("politician", t.get("name", ""))
            ticker = t.get("ticker", t.get("symbol", ""))
            trade_type = t.get("trade_type", t.get("transaction", ""))
            date = t.get("date", t.get("transaction_date", ""))
            lines.append(f"- {politician} / {ticker} / {trade_type} / {date}")
    return "\n".join(lines) if lines else "⚠️ 정치인 거래 내역 없음"

def _fmt_pcr(pcr):
    if not pcr:
        return "⚠️ Put/Call Ratio 수집 실패 - 수동 확인 필요"
    interp = f" / {pcr['interpretation']}" if pcr.get("interpretation") else ""
    return f"{pcr['ratio']} (출처: {pcr['source']}){interp}"

def _fmt_news(news_data: dict) -> str:
    if not news_data:
        return "뉴스 데이터 수집 실패"
    holding_news = {k: v for k, v in news_data.items() if not k.startswith("__sector_")}
    sector_news = {k: v for k, v in news_data.items() if k.startswith("__sector_")}
    lines = []
    if holding_news:
        lines.append("=== 보유 종목 뉴스 ===")
        for ticker, news_list in holding_news.items():
            lines.append(f"[{ticker}]")
            for n in news_list:
                lines.append(f"  - {n}")
    if sector_news:
        lines.append("\n=== 섹터별 시장 동향 (신규 종목 발굴용) ===")
        sector_labels = {
            "__sector_AI_반도체": "AI/반도체",
            "__sector_양자컴퓨팅": "양자컴퓨팅",
            "__sector_UAM_항공": "UAM/항공우주",
            "__sector_바이오_유전자": "바이오/유전자",
            "__sector_한국_반도체": "한국 반도체",
            "__sector_한국_방산": "한국 방산",
            "__sector_글로벌_시장": "글로벌 시장",
        }
        for key, news_list in sector_news.items():
            label = sector_labels.get(key, key.replace("__sector_", ""))
            lines.append(f"[{label}]")
            for n in news_list:
                lines.append(f"  - {n}")
    return "\n".join(lines) if lines else "뉴스 없음"

def _fmt_earnings(earnings_data: dict) -> str:
    if not earnings_data:
        return "실적 데이터 수집 실패"
    lines = []
    for ticker, info in earnings_data.items():
        parts = []
        if "다음실적발표" in info:
            parts.append(f"다음발표: {info['다음실적발표']} (earnings.com 직접 확인 필수)")
        if "최근분기매출" in info:
            parts.append(f"최근매출: {info['최근분기매출']}")
        if "최근분기순이익" in info:
            parts.append(f"순이익: {info['최근분기순이익']}")
        if parts:
            lines.append(f"[{ticker}] " + " / ".join(parts))
    return "\n".join(lines) if lines else "실적 데이터 없음"


# ── 기술적 지표 계산 ──────────────────────────────────────────────────────────
def _calc_rsi(close, period=14):
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return round((100 - (100 / (1 + rs))).iloc[-1], 1)

def _calc_macd(close):
    if len(close) < 26:
        return None, None
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return round(macd.iloc[-1], 4), round(signal.iloc[-1], 4)

def _calc_ma(close, period):
    if len(close) >= period:
        return round(close.rolling(period).mean().iloc[-1], 2)
    return None

def _calc_volume_ratio(volume):
    if len(volume) < 20:
        return None
    avg5  = volume.iloc[-5:].mean()
    avg20 = volume.iloc[-20:].mean()
    return round(avg5 / avg20, 2) if avg20 > 0 else None


# ── 현재가 수집 ───────────────────────────────────────────────────────────────
def _get_current_price(stock, info, hist, ticker):
    is_kr = ticker.endswith(".KS") or ticker.endswith(".KQ")

    # 5일 평균 (이상치 검증용)
    hist_avg = None
    if not hist.empty:
        close5 = hist["Close"].dropna().tail(5)
        if len(close5) >= 2:
            hist_avg = float(close5.mean())

    def _is_valid(price):
        if price is None or price <= 0:
            return False
        if hist_avg is not None and abs(price / hist_avg - 1) > 0.30:
            return False
        return True

    def _src_naver():
        code = ticker.split(".")[0]
        try:
            url = f"https://finance.naver.com/item/main.nhn?code={code}"
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                tag = soup.find("strong", id="_nowVal")
                if tag:
                    return float(tag.get_text(strip=True).replace(",", ""))
        except Exception:
            pass
        return None

    def _src_hist():
        if not hist.empty:
            close = hist["Close"].dropna()
            if not close.empty:
                return float(close.iloc[-1])
        return None

    def _src_fast_info():
        try:
            p = stock.fast_info.last_price
            return float(p) if p and p > 0 else None
        except Exception:
            return None

    def _src_info_key():
        for key in ("regularMarketPrice", "currentPrice"):
            p = info.get(key)
            if p and p > 0:
                return float(p)
        return None

    sources = (
        [("네이버금융", _src_naver), ("history[-1]", _src_hist), ("fast_info", _src_fast_info)]
        if is_kr else
        [("history[-1]", _src_hist), ("fast_info", _src_fast_info), ("info[regularMarketPrice]", _src_info_key)]
    )

    for name, fn in sources:
        try:
            price = fn()
            if price and price > 0:
                if _is_valid(price):
                    print(f"    [{ticker}] 현재가 출처: {name} ({price})", flush=True)
                    return price
                avg_str = f"{hist_avg:.2f}" if hist_avg else "N/A"
                print(f"    [{ticker}] {name} 이상치 ({price:.2f}, 5일평균 {avg_str}) — 다음 소스 시도", flush=True)
        except Exception as e:
            print(f"    [{ticker}] {name} 오류: {e}", flush=True)

    print(f"    ⚠️ [{ticker}] 현재가 수집 실패 — 모든 소스 실패", flush=True)
    return None


# ── 주식 데이터 수집 ──────────────────────────────────────────────────────────
def get_stock_data(tickers):
    results = {}
    for ticker in tickers:
        print(f"    [{ticker}] 데이터 수집 중...", flush=True)
        try:
            stock = yf.Ticker(ticker)

            print(f"    [{ticker}] info 요청 중...", flush=True)
            info = stock.info

            print(f"    [{ticker}] 히스토리 요청 중...", flush=True)
            hist = stock.history(period="1y", auto_adjust=True)

            current_price = _get_current_price(stock, info, hist, ticker)
            if current_price is None:
                print(f"    [{ticker}] 현재가 수집 실패, 건너뜀", flush=True)
                continue

            if not hist.empty:
                close  = hist["Close"].astype(float).dropna()
                volume = hist["Volume"].astype(float).fillna(0)
            else:
                close = volume = None

            print(f"    [{ticker}] 데이터 포인트: {len(close) if close is not None else 0}일", flush=True)

            high_52w = close.max() if close is not None else info.get("fiftyTwoWeekHigh", "N/A")
            low_52w  = close.min() if close is not None else info.get("fiftyTwoWeekLow",  "N/A")

            def period_return(days):
                if close is not None and len(close) >= days:
                    return (current_price / close.iloc[-days] - 1) * 100
                return None

            ma5   = _calc_ma(close, 5)   if close is not None else None
            ma20  = _calc_ma(close, 20)  if close is not None else None
            ma60  = _calc_ma(close, 60)  if close is not None else None
            ma120 = _calc_ma(close, 120) if close is not None else None

            if ma20 is not None:
                if current_price > ma20 * 2 or current_price < ma20 * 0.5:
                    print(f"    ⚠️ [{ticker}] 현재가({round(current_price, 2)})와 MA20({round(ma20, 2)}) 괴리 50%+ — 오류 의심, None 처리", flush=True)
                    current_price = None
            if current_price is None:
                continue

            aligned = (
                all(v is not None for v in [ma5, ma20, ma60, ma120]) and
                ma5 > ma20 > ma60 > ma120
            )
            golden_cross = (ma5 is not None and ma20 is not None and ma5 > ma20)

            rsi = _calc_rsi(close) if close is not None else None
            macd_val, macd_signal = _calc_macd(close) if close is not None else (None, None)
            macd_golden = (
                macd_val is not None and macd_signal is not None and
                macd_val > macd_signal
            )
            vol_ratio = _calc_volume_ratio(volume) if volume is not None else None

            results[ticker] = {
                "현재가": round(current_price, 2),
                "52주_고": round(high_52w, 2) if isinstance(high_52w, (int, float)) else high_52w,
                "52주_저": round(low_52w,  2) if isinstance(low_52w,  (int, float)) else low_52w,
                "1개월_수익률": round(period_return(21),  2) if period_return(21)  is not None else "N/A",
                "3개월_수익률": round(period_return(63),  2) if period_return(63)  is not None else "N/A",
                "6개월_수익률": round(period_return(126), 2) if period_return(126) is not None else "N/A",
                "1년_수익률":   round(period_return(252), 2) if period_return(252) is not None else "N/A",
                "PER": info.get("trailingPE", "N/A"),
                "PBR": info.get("priceToBook", "N/A"),
                "시가총액": info.get("marketCap", "N/A"),
                "매출성장률": info.get("revenueGrowth", "N/A"),
                "영업이익률": info.get("operatingMargins", "N/A"),
                "ROE": info.get("returnOnEquity", "N/A"),
                "MA5": ma5, "MA20": ma20, "MA60": ma60, "MA120": ma120,
                "정배열": aligned,
                "골든크로스(MA5>MA20)": golden_cross,
                "RSI14": rsi,
                "MACD": macd_val,
                "MACD_시그널": macd_signal,
                "MACD_골든크로스": macd_golden,
                "거래량비율(5일/20일)": vol_ratio,
            }
            print(f"    [{ticker}] 완료 — 현재가: {round(current_price, 2)} / RSI: {rsi} / 정배열: {aligned}", flush=True)
        except Exception as e:
            print(f"    [{ticker}] 실패: {e}", flush=True)

    return results


# ── 자기학습 로그 ─────────────────────────────────────────────────────────────
LEARNING_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning_log.json")

def load_learning_log():
    try:
        with open(LEARNING_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_kr_ticker(ticker: str) -> bool:
    return ticker.endswith(".KS") or ticker.endswith(".KQ")

def _infer_currency(ticker: str) -> str:
    return "KRW" if _is_kr_ticker(ticker) else "USD"

def _normalize_action(action_str: str) -> str:
    s = str(action_str).strip()
    if any(k in s for k in ["강력매수", "추가매수", "분할매수", "신규매수", "매수", "💎", "🟢"]):
        return "매수"
    if any(k in s for k in ["즉시매도", "전량매도", "비중축소", "절반매도", "매도", "익절", "손절", "🔴", "🟠"]):
        return "매도"
    if any(k in s for k in ["보유", "홀딩", "관망", "추매금지", "매수금지", "알림", "🟡"]):
        return "홀딩"
    return "관찰"

def _classify_stock_type(ticker: str, portfolio_data: dict, current_price=None) -> str:
    HIGH_RISK = {"RGTI", "BEAM", "JOBY", "QBTS", "PWFL"}
    # 한미반도체는 손실률에 관계없이 항상 유형② 성장스윙으로 고정
    FORCE_SWING = {"042700.KS"}
    if any(ticker == it.get("ticker") for it in portfolio_data.get("category1", [])):
        return "핵심장기보유"
    if ticker in HIGH_RISK:
        return "고위험옵션"
    if ticker in FORCE_SWING:
        return "성장스윙"
    for it in portfolio_data.get("category2", []):
        if it.get("ticker") == ticker and current_price:
            avg = _safe_float(it.get("avg_price"))
            if avg and (current_price / avg - 1) * 100 <= -10:
                return "회복탈출관리"
    return "성장스윙"

def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _get_portfolio_tickers(portfolio):
    """portfolio category1/category2에서 실제 보유 종목 ticker set을 반환한다."""
    owned = set()
    for cat in ["category1", "category2"]:
        for it in portfolio.get(cat, []):
            t = it.get("ticker")
            if t:
                owned.add(t)
    return owned

def save_learning_log(log_entry: dict):
    log = load_learning_log()
    today = datetime.now().strftime("%Y-%m-%d")
    log[today] = log_entry
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    log = {k: v for k, v in log.items() if k >= cutoff}
    with open(LEARNING_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f"  learning_log.json 저장 완료 ({len(log)}일치)", flush=True)


def update_learning_log_returns(pf: dict) -> None:
    """learning_log.json에서 1일 경과한 추천 항목의 수익률과 성공여부를 업데이트"""
    log = load_learning_log()
    if not log:
        print("  [LEARNING] 로그 없음 — 스킵", flush=True)
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    changed = False
    updated = 0

    for date_str, entry in log.items():
        d_diff = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(date_str, "%Y-%m-%d")).days
        if d_diff < 1:
            continue
        for rec in entry.get("추천종목", []):
            if rec.get("1일후수익률") is not None:
                continue
            ticker = rec.get("ticker")
            rec_price = _safe_float(rec.get("당시가격") or rec.get("추천가"))
            if not ticker or not rec_price:
                continue
            try:
                batch = get_stock_data([ticker])
                current_price = _safe_float((batch.get(ticker) or {}).get("현재가"))
            except Exception:
                current_price = None
            if not current_price:
                continue
            pct = (current_price / rec_price - 1) * 100
            rec["1일후수익률"] = round(pct, 2)
            action = _normalize_action(rec.get("추천행동") or rec.get("방향") or "")
            if pct == 0.0:
                rec["성공여부"] = None
            elif action == "매수":
                rec["성공여부"] = pct > 0
            elif action == "매도":
                rec["성공여부"] = pct < 0
            elif action == "홀딩":
                rec["성공여부"] = abs(pct) <= 2.0
            else:
                rec["성공여부"] = None

            if rec.get("성공여부") is False:
                tags = []
                rsi = _safe_float(rec.get("당시RSI"))
                vol_ratio = _safe_float(rec.get("당시거래량비"))
                vix = _safe_float(rec.get("당시VIX"))
                stock_type = rec.get("종목유형", "")
                market_risk = rec.get("당시시장위험도", "")
                if rsi is not None:
                    if action == "매수" and rsi > 70:
                        tags.append("RSI_false_signal")
                    elif action == "매도" and rsi < 30:
                        tags.append("RSI_false_signal")
                if vol_ratio is not None and vol_ratio < 0.8:
                    tags.append("weak_volume")
                if vix is not None and vix > 20 and "높" in market_risk:
                    tags.append("macro_ignored")
                if stock_type == "고위험옵션":
                    tags.append("high_risk_overweight")
                if _is_kr_ticker(ticker) and action == "매수":
                    tags.append("korea_momentum_fail")
                if stock_type == "회복탈출관리" and action == "홀딩":
                    tags.append("loss_stock_bad_exit")
                if "높" in market_risk and action == "매수":
                    tags.append("risk_mode_ignored")
                if not tags:
                    tags.append("general_miss")
                rec["실패태그"] = tags
            else:
                rec.setdefault("실패태그", [])
            changed = True
            updated += 1

    if changed:
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        log = {k: v for k, v in log.items() if k >= cutoff}
        with open(LEARNING_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        print(f"  [LEARNING] 수익률 업데이트 완료 ({updated}건)", flush=True)
    else:
        print(f"  [LEARNING] 업데이트 대상 없음", flush=True)


def analyze_failure_patterns(days: int = 5) -> dict:
    """최근 N일간 추천 결과 패턴 분석"""
    from collections import Counter
    log = load_learning_log()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_recs = []
    for date_str, entry in log.items():
        if date_str < cutoff:
            continue
        for rec in entry.get("추천종목", []):
            if rec.get("성공여부") is None:
                continue
            all_recs.append(rec)

    if not all_recs:
        return {}

    total = len(all_recs)
    successes = sum(1 for r in all_recs if r.get("성공여부") is True)
    win_rate = successes / total * 100 if total > 0 else 0

    type_stats: dict = {}
    for rec in all_recs:
        t = rec.get("종목유형", "기타")
        type_stats.setdefault(t, {"win": 0, "total": 0})
        type_stats[t]["total"] += 1
        if rec.get("성공여부"):
            type_stats[t]["win"] += 1
    type_win_rates = {
        t: round(v["win"] / v["total"] * 100, 1) if v["total"] > 0 else 0
        for t, v in type_stats.items()
    }

    fail_tags: Counter = Counter()
    for rec in all_recs:
        if rec.get("성공여부") is False:
            for tag in rec.get("실패태그", []):
                fail_tags[tag] += 1

    rules = []
    if win_rate < 50:
        rules.append("전체 승률 50% 미만 — 오늘 판단 한 단계 보수적 조정")
    if fail_tags.get("RSI_false_signal", 0) >= 2:
        rules.append("RSI 극단값 매수/매도 신호 반복 실패 — RSI 70+ 매수 / RSI 30- 매도 주의")
    if fail_tags.get("weak_volume", 0) >= 2:
        rules.append("거래량 비율 0.8 미만 반복 — 거래량 미확인 매수 금지")
    if fail_tags.get("macro_ignored", 0) >= 2:
        rules.append("VIX 20+ 환경 무시 반복 — 고VIX 시 매수 판단 보수적 조정")
    if fail_tags.get("high_risk_overweight", 0) >= 2:
        rules.append("고위험 종목 반복 실패 — 고위험 종목 신규 매수 제한")
    if fail_tags.get("korea_momentum_fail", 0) >= 2:
        rules.append("한국 주식 모멘텀 판단 반복 실패 — 한국 시장 매수 조건 강화")
    if fail_tags.get("risk_mode_ignored", 0) >= 2:
        rules.append("시장 고위험 무시 반복 — 오늘 매수 판단 전체 보수적 조정")
    if not rules:
        rules.append("특이 패턴 없음 — 기존 전략 유지")

    return {
        "전체승률": round(win_rate, 1),
        "분석종목수": total,
        "유형별승률": type_win_rates,
        "주요실패태그": dict(fail_tags.most_common(5)),
        "반영규칙": rules,
    }


def _fmt_patterns(p5: dict, p20: dict) -> str:
    if not p5 and not p20:
        return "패턴 분석 데이터 없음 (수익률 미집계)"
    lines = []
    if p5:
        lines.append("[최근 5일 패턴]")
        lines.append(f"  전체승률: {p5.get('전체승률', 'N/A')}% ({p5.get('분석종목수', 0)}건)")
        for t, r in (p5.get("유형별승률") or {}).items():
            lines.append(f"  {t}: {r}%")
        if p5.get("주요실패태그"):
            lines.append(f"  주요실패: {', '.join(f'{k}({v})' for k, v in p5['주요실패태그'].items())}")
        lines.append("  반영규칙:")
        for rule in p5.get("반영규칙", []):
            lines.append(f"    - {rule}")
    if p20:
        lines.append("[최근 20일 패턴]")
        lines.append(f"  전체승률: {p20.get('전체승률', 'N/A')}% ({p20.get('분석종목수', 0)}건)")
        for t, r in (p20.get("유형별승률") or {}).items():
            lines.append(f"  {t}: {r}%")
        if p20.get("주요실패태그"):
            lines.append(f"  주요실패: {', '.join(f'{k}({v})' for k, v in p20['주요실패태그'].items())}")
        lines.append("  반영규칙:")
        for rule in p20.get("반영규칙", []):
            lines.append(f"    - {rule}")
    return "\n".join(lines)


def _parse_report_judgments(report: str, all_stock_data: dict) -> dict:
    """보고서 텍스트에서 종목별 판단 방향 추출 → {ticker: {"방향": "매수"/"매도"/"홀딩", "판단": str}}"""
    judgments = {}
    judgment_map = [
        ("🔴", "매도"), ("🟠", "매도"),
        ("🟢", "매수"), ("💎", "매수"),
        ("🟡", "홀딩"),
    ]
    judgment_labels = [
        "🔴 즉시매도", "🟠 비중축소", "🟡 홀딩", "🟢 추가매수", "💎 강력매수",
        "🔴즉시매도", "🟠비중축소", "🟡홀딩", "🟢추가매수", "💎강력매수",
    ]
    for ticker in all_stock_data:
        for line in report.split('\n'):
            if ticker not in line:
                continue
            for emoji, direction in judgment_map:
                if emoji in line:
                    판단_str = next((lbl for lbl in judgment_labels if lbl in line), emoji)
                    judgments[ticker] = {"방향": direction, "판단": 판단_str}
                    break
            if ticker in judgments:
                break
    return judgments


def build_yesterdays_verification(all_stock_data: dict, exchange_rate: float) -> str:
    log = load_learning_log()
    if not log:
        return "전일 보고서 없음 — 자기학습 생략"

    today_str = datetime.now().strftime("%Y-%m-%d")
    dates = sorted(log.keys())
    recent_date = dates[-1] if dates[-1] != today_str else (dates[-2] if len(dates) >= 2 else None)

    if not recent_date:
        return "전일 보고서 없음 — 자기학습 생략"

    entry = log[recent_date]
    lines = [f"[{recent_date} 추천 결과 검증]"]

    correct = 0
    total = 0

    for rec in entry.get("추천종목", []):
        ticker = rec.get("ticker")
        rec_price = rec.get("추천가")
        today_data = all_stock_data.get(ticker, {})
        today_price = today_data.get("현재가")

        if rec_price and today_price:
            pct = (float(today_price) / float(rec_price) - 1) * 100
            if pct == 0.0:
                lines.append(f"  {rec.get('name','?')}({ticker}): 추천가 {rec_price} → 오늘 {today_price} (0.0%) ⏸️ 장 미개장 또는 데이터 동일 — 다음 거래일 재확인")
                continue
            direction = rec.get("방향", "매수")
            if direction == "매도":
                correct_flag = "✅정확" if pct < 0 else "❌틀림"
                correct += 1 if pct < 0 else 0
            elif direction == "홀딩":
                correct_flag = "✅정확" if abs(pct) <= 2.0 else "❌틀림"
                correct += 1 if abs(pct) <= 2.0 else 0
            else:  # 매수
                correct_flag = "✅정확" if pct > 0 else "❌틀림"
                correct += 1 if pct > 0 else 0
            total += 1
            lines.append(f"  {rec.get('name','?')}({ticker}): 추천가 {rec_price} → 오늘 {today_price} ({pct:+.1f}%) {correct_flag}")
        else:
            lines.append(f"  {rec.get('name','?')}({ticker}): 비교 불가")

    for rec in entry.get("즉시매도", []):
        ticker = rec.get("ticker")
        rec_price = rec.get("매도가")
        today_data = all_stock_data.get(ticker, {})
        today_price = today_data.get("현재가")
        if rec_price and today_price:
            pct = (float(today_price) / float(rec_price) - 1) * 100
            correct_flag = "✅정확(계속하락)" if pct < 0 else "⚠️반등(매도타이밍재검토)"
            lines.append(f"  {rec.get('name','?')} 매도 후: {pct:+.1f}% {correct_flag}")

    market_call = entry.get("시장판단", "")
    if market_call:
        lines.append(f"  전일 시장 판단: {market_call}")

    if total > 0:
        accuracy = correct / total * 100
        lines.append(f"  종목 판단 정확도: {correct}/{total} ({accuracy:.0f}%)")

    all_correct = sum(d.get("판단정확도_맞춤", 0) for d in log.values())
    all_total   = sum(d.get("판단정확도_전체", 0) for d in log.values())
    if all_total > 0:
        lines.append(f"  누적 판단 정확도: {all_correct}/{all_total} ({all_correct/all_total*100:.0f}%) — {len(log)}일 누적")

    return "\n".join(lines)


def get_pending_actions_summary(portfolio_data: dict) -> str:
    """진행 중인 분할매수/매도 계획 요약 + 보유 주수 기반 자동 진행률 계산"""
    actions = portfolio_data.get("pending_actions", [])
    if not actions:
        return "진행 중인 분할 계획 없음"

    # 현재 보유 주수 맵 생성
    holdings = {}
    for cat in ["category1", "category2"]:
        for it in portfolio_data.get(cat, []):
            holdings[it["ticker"]] = it["shares"]

    lines = ["[진행 중인 분할 계획]"]
    for a in actions:
        if a.get("status") == "완료":
            continue
        ticker = a.get("ticker", "")
        name = a.get("name", ticker)
        total = a.get("total_units", 0)
        unit_shares = a.get("unit_shares", 0)
        start_shares = a.get("start_shares", 0)
        memo = a.get("memo", "")
        status = a.get("status", "진행중")
        created = a.get("created_date", "")

        # 현재 보유 주수 기반 자동 done 계산
        current_shares = holdings.get(ticker, start_shares)
        shares_added = current_shares - start_shares
        if unit_shares > 0:
            done = min(int(shares_added / unit_shares), total)
        else:
            done = a.get("done_units", 0)
        remaining = total - done

        if status == "일시중단":
            reason = a.get("pause_reason", "")
            resume = a.get("resume_condition", "미정")
            lines.append(
                f"  ⏸️  {name}({ticker}): {total}회 분할 중 {done}회 완료 "
                f"— 일시중단({reason}). 재개 조건: {resume}"
            )
        elif remaining <= 0:
            lines.append(f"  ✅ {name}({ticker}): {total}회 분할 완료")
        else:
            lines.append(
                f"  🔄 {name}({ticker}): {total}회 분할 중 {done}회 완료, "
                f"{remaining}회 남음 [{memo}] (시작: {created})"
            )

    return "\n".join(lines)


# ── 포트폴리오 사전 계산 ─────────────────────────────────────────────────────
def calc_portfolio_summary(portfolio_data, stock_data, exchange_rate):
    pf = portfolio_data
    er = exchange_rate
    lines = []

    def get_price(ticker):
        d = stock_data.get(ticker, {})
        p = d.get("현재가")
        if p and float(p) > 0:
            return float(p)
        return None

    total = 0

    lines.append("\n[카테고리1 계산]")
    cat1_sum = 0
    for it in pf.get("category1", []):
        price = get_price(it["ticker"])
        shares = it["shares"]
        avg = it["avg_price"]
        if price:
            value = price * shares * er if it["currency"] == "USD" else price * shares
            cost  = avg   * shares * er if it["currency"] == "USD" else avg   * shares
            pct   = (price / avg - 1) * 100
            lines.append(f"  {it['name']}({it['ticker']}): {price} × {shares}주 = {value:,.0f}원 (수익률 {pct:+.1f}%, 손익 {value-cost:+,.0f}원)")
            cat1_sum += value
        else:
            lines.append(f"  {it['name']}({it['ticker']}): 현재가 미수집 → 0원")
    lines.append(f"  카테고리1 소계: {cat1_sum:,.0f}원")
    total += cat1_sum

    lines.append("\n[카테고리2-한국 계산]")
    cat2_kr_sum = 0
    for it in pf.get("category2", []):
        if it["currency"] != "KRW":
            continue
        price = get_price(it["ticker"])
        shares = it["shares"]
        avg = it["avg_price"]
        if price:
            value = price * shares
            cost  = avg   * shares
            pct   = (price / avg - 1) * 100
            lines.append(f"  {it['name']}: {price:,.0f} × {shares}주 = {value:,.0f}원 (수익률 {pct:+.1f}%, 손익 {value-cost:+,.0f}원)")
            cat2_kr_sum += value
        else:
            lines.append(f"  {it['name']}: 현재가 미수집 → 0원")
    lines.append(f"  카테고리2-한국 소계: {cat2_kr_sum:,.0f}원")
    total += cat2_kr_sum

    lines.append("\n[카테고리2-미국 계산]")
    cat2_us_sum = 0
    for it in pf.get("category2", []):
        if it["currency"] != "USD":
            continue
        price = get_price(it["ticker"])
        shares = it["shares"]
        avg = it["avg_price"]
        if price:
            value = price * shares * er
            cost  = avg   * shares * er
            pct   = (price / avg - 1) * 100
            lines.append(f"  {it['name']}({it['ticker']}): ${price} × {shares}주 × {er:.0f} = {value:,.0f}원 (수익률 {pct:+.1f}%, 손익 {value-cost:+,.0f}원)")
            cat2_us_sum += value
        else:
            lines.append(f"  {it['name']}({it['ticker']}): 현재가 미수집 → 0원 (시가 미수집)")
    lines.append(f"  카테고리2-미국 소계: {cat2_us_sum:,.0f}원")
    total += cat2_us_sum

    lines.append("\n[현금]")
    cash = pf.get("cash", {})
    krw_cash = cash.get("krw", 0)
    usd_cash = cash.get("usd", 0)
    usd_cash_krw = usd_cash * er
    cash_total = krw_cash + usd_cash_krw
    lines.append(f"  원화: {krw_cash:,.0f}원")
    lines.append(f"  달러: ${usd_cash:,.2f} × {er:.0f} = {usd_cash_krw:,.0f}원")
    lines.append(f"  현금 소계: {cash_total:,.0f}원")
    total += cash_total

    lines.append(f"\n★ 총합계: {total:,.0f}원 (적용 환율: {er:.1f}원/달러)")
    return "\n".join(lines)


# ── 보고서 자동 검증 ──────────────────────────────────────────────────────────
def validate_report(report: str, pf: dict, all_stock_data: dict, exchange_rate: float) -> list:
    errors = []
    import re

    # 총자산 검증 (Python 계산값 vs 보고서 수치 5% 이상 차이 시 오류)
    calc_result = calc_portfolio_summary(pf, all_stock_data, exchange_rate)
    total_line = [l for l in calc_result.split("\n") if "총합계" in l]
    if total_line:
        match = re.search(r"([\d,]+)원", total_line[0])
        if match:
            calc_total = int(match.group(1).replace(",", ""))
            report_match = re.search(r"총합.*?([\d,]+)원", report)
            if report_match:
                report_total = int(report_match.group(1).replace(",", ""))
                diff_pct = abs(calc_total - report_total) / calc_total * 100
                if diff_pct > 5:
                    errors.append(f"[총자산 계산 오류] Python {calc_total:,}원 vs 보고서 {report_total:,}원 ({diff_pct:.1f}% 차이)")

    # 3. 주가 이상치 검증
    for ticker, data in all_stock_data.items():
        price = data.get("현재가")
        hist_avg = data.get("hist_avg")
        if price and hist_avg and abs(price / hist_avg - 1) > 0.3:
            errors.append(f"[주가 이상치] {ticker}: 현재가 {price} vs 5일평균 {hist_avg:.0f} ({(price/hist_avg-1)*100:+.1f}%)")

    return errors


# ── TDE helpers ───────────────────────────────────────────────────────────────
def _get_metric(summary, keys, default=None):
    """learning_summary에서 다양한 키 이름으로 값을 탐색한다."""
    if not summary:
        return default
    for k in keys:
        if k in summary:
            return summary[k]
    return default


def _normalize_pct(v):
    """0.306 → 30.6 / 30.6 → 30.6 / '30.6%' → 30.6 / None → None"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%")
        try:
            v = float(v)
        except ValueError:
            return None
    v = float(v)
    if 0 < abs(v) < 2.0:   # 0.306처럼 소수 비율이면 ×100
        return v * 100
    return v


# ── TDE (Trade Decision Engine) ───────────────────────────────────────────────
def build_trade_decision_engine(
    us_data,
    kr_data,
    portfolio,
    learning_summary,
    macro_data,
    news_data=None,
    earnings_data=None,
    exchange_rate=None,   # 2단계에서 추가 — 포트폴리오 비중 계산에 사용
):
    """
    종목별 trade_decision JSON을 생성한다.
    2단계: buy_grade A/B/C/D 계산 로직 추가.
    """
    all_data = {**us_data, **kr_data}
    _exr = _safe_float(exchange_rate) or 1400.0   # 환율 fallback

    # ── 포트폴리오 종목 목록 ────────────────────────────────────────────────
    pf_items = {}
    for cat in ["category1", "category2"]:
        for it in portfolio.get(cat, []):
            pf_items[it["ticker"]] = it
    pf_tickers = {t: it.get("name", t) for t, it in pf_items.items()}
    all_tickers_ordered = list(dict.fromkeys(list(pf_tickers.keys()) + list(all_data.keys())))
    _pf_owned_tickers = _get_portfolio_tickers(portfolio)

    _cat_map = {
        "핵심장기보유": "유형①",
        "고위험옵션":   "유형③",
        "회복탈출관리": "유형④",
        "성장스윙":     "유형②",
    }

    HIGH_RISK_TICKERS = {"RGTI", "BEAM", "JOBY", "PWFL", "QBTS"}
    DCA_TICKERS       = {"VOO", "GOOGL", "FCX"}
    CELLTRION_TICKER  = "068760.KS"

    # ── learning_summary 지표 추출 ──────────────────────────────────────────
    win_rate_5d  = _normalize_pct(_get_metric(
        learning_summary, ["전체승률", "win_rate_5d", "recent_5d_win_rate", "5일 승률"]))
    win_rate_20d = _normalize_pct(_get_metric(
        learning_summary, ["win_rate_20d", "20일 승률"]))
    prev_accuracy = _normalize_pct(_get_metric(
        learning_summary, ["previous_accuracy", "accuracy_1d", "전일정확도"]))

    # ── 포트폴리오 리스크 계산 ──────────────────────────────────────────────
    total_usd_est   = 0.0
    ticker_val_usd  = {}
    for t, d in all_data.items():
        price = _safe_float(d.get("현재가"))
        if price is None:
            continue
        it = pf_items.get(t, {})
        shares   = _safe_float(it.get("shares")) or 0.0
        currency = it.get("currency", "USD")
        val = price * shares / _exr if currency == "KRW" else price * shares
        ticker_val_usd[t] = val
        total_usd_est += val

    def _weight_pct(tickers_set):
        if total_usd_est <= 0:
            return None
        return sum(ticker_val_usd.get(t, 0.0) for t in tickers_set) / total_usd_est * 100

    high_risk_weight_pct  = _weight_pct(HIGH_RISK_TICKERS)
    celltrion_weight_pct  = _weight_pct({CELLTRION_TICKER})

    # ── 전역 강제 D 조건 ────────────────────────────────────────────────────
    win_rate_too_low  = win_rate_5d is not None and win_rate_5d < 30.0
    high_risk_exceeded = high_risk_weight_pct is not None and high_risk_weight_pct > 10.0

    # ── 임시 목표가/손절가 ──────────────────────────────────────────────────
    # TODO: 3단계 이후 price_targets.json 또는 portfolio.json의 전략 필드에서 stop/target을 읽도록 이전
    PRICE_TARGETS = {
        "VOO":       {"stop": None,      "target": 720.0},
        "GOOGL":     {"stop": 370.0,     "target": 410.0},
        "FCX":       {"stop": 60.0,      "target": 70.0},
        "NVDA":      {"stop": 200.0,     "target": 230.0},
        "RGTI":      {"stop": 22.0,      "target": 28.0},
        "BEAM":      {"stop": 28.0,      "target": 35.0},
        "042700.KS": {"stop": 280000.0,  "target": 360000.0},   # 한미반도체
    }

    def _not_na(v):
        if v is None or v == "N/A":
            return False
        return _safe_float(v) is not None

    results = []
    for ticker in all_tickers_ordered:
        data          = all_data.get(ticker, {})
        current_price = _safe_float(data.get("현재가"))
        name          = pf_tickers.get(ticker, ticker)

        raw_type = _classify_stock_type(ticker, portfolio, current_price)
        category = _cat_map.get(raw_type, "유형②")

        # ── data_status ─────────────────────────────────────────────────────
        price_collected = current_price is not None
        volume_collected = _safe_float(data.get("거래량비율(5일/20일)")) is not None
        technical_collected = (
            _safe_float(data.get("RSI14")) is not None
            or any(_safe_float(data.get(f"MA{p}")) is not None for p in [5, 20, 60, 120])
            or _safe_float(data.get("MACD")) is not None
        )
        fundamental_collected = (
            _not_na(data.get("PER")) or _not_na(data.get("PBR")) or _not_na(data.get("영업이익률"))
        )
        news_collected = bool(news_data and news_data.get(ticker))

        data_issues = []
        if not price_collected:       data_issues.append("price_missing")
        if not volume_collected:      data_issues.append("volume_missing")
        if not technical_collected:   data_issues.append("technical_missing")
        if not fundamental_collected: data_issues.append("fundamental_missing")
        if not news_collected:        data_issues.append("news_missing")

        if not price_collected:
            data_quality = "D"
        elif technical_collected and fundamental_collected and volume_collected and news_collected:
            data_quality = "A"
        elif technical_collected and fundamental_collected:
            data_quality = "B"
        else:
            data_quality = "C"

        # ── RSI 추출 및 유형별 lump_sum 차단 판정 ────────────────────────────
        rsi = _safe_float(data.get("RSI14"))

        if category in ("유형③", "유형④"):
            rsi_blocks_lump = True
        elif ticker == "FCX":                           # 유형① 원자재
            rsi_blocks_lump = rsi is not None and rsi > 68
        elif category == "유형①":                       # VOO, GOOGL
            rsi_blocks_lump = rsi is not None and rsi > 65
        else:                                           # 유형②
            rsi_blocks_lump = rsi is not None and rsi > 65

        # ── 자동 적립 vs 신규 목돈 매수 분리 ────────────────────────────────
        auto_dca_allowed = ticker in DCA_TICKERS
        auto_dca_reason  = (
            "기존 자동 적립 유지 (승률 무관)" if auto_dca_allowed else "자동 적립 대상 아님"
        )

        # ── risk_reward 계산 ─────────────────────────────────────────────────
        pt       = PRICE_TARGETS.get(ticker, {})
        stop_p   = _safe_float(pt.get("stop"))
        target_p = _safe_float(pt.get("target"))
        entry_p  = current_price

        rr_valid = False
        gain_pct = loss_pct = rr_ratio = None

        if entry_p and target_p and target_p > entry_p:
            gain_pct = (target_p - entry_p) / entry_p * 100
            if stop_p and stop_p < entry_p:
                loss_pct = (stop_p - entry_p) / entry_p * 100
                if abs(loss_pct) > 0:
                    rr_ratio  = gain_pct / abs(loss_pct)
                    rr_valid  = True

        if ticker == "VOO":
            rr_valid = False   # 장기 적립 — 명확한 손절가 없음

        # ── buy_grade A/B/C/D 계산 ──────────────────────────────────────────
        reason            = []
        buy_grade         = "D"
        lump_sum_allowed  = False

        if not price_collected:
            reason    += ["현재가 수집 실패 — 판단 불가", "data_quality D"]
            buy_grade  = "D"

        elif data_quality == "D":
            reason    += ["데이터 품질 D — 판단 불가", "주요 지표 미수집"]
            buy_grade  = "D"

        elif category == "유형④":
            reason    += [f"유형④ 회복/탈출 관리 — 신규 매수 대상 아님",
                          f"{name} 손절/회복 기준만 관리"]
            buy_grade  = "D"

        elif category == "유형③":
            reason    += ["유형③ 고위험 옵션성 — 신규 매수 금지 원칙"]
            if high_risk_exceeded:
                reason.append(f"고위험 합산 비중 {high_risk_weight_pct:.1f}% > 10% 초과")
            else:
                reason.append("정책 모멘텀은 보유 근거이나 추매 근거 아님")
            buy_grade  = "D"

        elif win_rate_too_low:
            reason    += [f"최근 5일 승률 {win_rate_5d:.1f}% < 30% — 신규 목돈 매수 D"]
            if ticker in DCA_TICKERS:
                reason.append(f"유형① 자동 적립({ticker})은 계속 진행 가능")
            else:
                reason.append("승률 회복 시 재평가")
            buy_grade  = "D"

        else:
            # ── 유형① 신규 목돈 매수 가격 게이트 ────────────────────────────
            # 자동 적립은 유지하되, 신규 목돈 매수 B+ 는 가격 조건 필요
            _price_gate_blocks_lump = False
            if ticker == "GOOGL" and current_price is not None and current_price > 370.0:
                _price_gate_blocks_lump = True
                reason.append(
                    f"GOOGL 현재가 {current_price:.2f}달러 > 370달러 — "
                    f"신규 목돈 매수 B등급 불가, 자동 적립만 유지"
                )
            elif ticker == "FCX" and current_price is not None and current_price > 60.0:
                _price_gate_blocks_lump = True
                reason.append(
                    f"FCX 현재가 {current_price:.2f}달러 > 60달러 — "
                    f"신규 목돈 매수 B등급 불가, 자동 적립만 유지"
                )
            elif ticker == "VOO" and current_price is not None:
                if not (rsi is None or rsi <= 60.0 or current_price <= 670.0):
                    _price_gate_blocks_lump = True
                    reason.append(
                        f"VOO 현재가 {current_price:.2f}달러 > 670달러이고 "
                        f"RSI {rsi:.1f} > 60 — 신규 목돈 매수 B등급 불가, 자동 적립만 유지"
                    )

            # ── A 조건 평가 ─────────────────────────────────────────────────
            a_ok = (
                data_quality in ("A", "B")
                and (win_rate_5d is not None and win_rate_5d >= 40.0)
                and (
                    (prev_accuracy is not None and prev_accuracy >= 70.0)
                    or (win_rate_20d is not None and win_rate_20d >= 45.0)
                )
                and (not high_risk_exceeded or category == "유형①")
                and (celltrion_weight_pct is None or celltrion_weight_pct <= 25.0)
                and rr_valid
                and (rr_ratio is not None and rr_ratio >= 2.0)
                and (gain_pct is not None and gain_pct >= 8.0)
                and (loss_pct is not None and abs(loss_pct) <= 7.0)
                and not rsi_blocks_lump
                and not _price_gate_blocks_lump
                and category != "유형③"
            )

            # ── B 조건 평가 ─────────────────────────────────────────────────
            b_ok = (
                data_quality in ("A", "B")
                and (
                    (win_rate_5d is not None and win_rate_5d >= 35.0)
                    or category == "유형①"
                )
                and rr_valid
                and (rr_ratio is not None and rr_ratio >= 1.5)
                and (loss_pct is not None and abs(loss_pct) <= 10.0)
                and (not high_risk_exceeded or category == "유형①")
                and category != "유형③"
                and not rsi_blocks_lump
                and not _price_gate_blocks_lump
            )

            if a_ok:
                buy_grade        = "A"
                lump_sum_allowed = True
                rsi_str          = f"{rsi:.1f}" if rsi is not None else "N/A"
                reason          += [
                    f"A등급 — RR {rr_ratio:.2f} / 5일승률 {win_rate_5d:.1f}%",
                    f"RSI {rsi_str} 허용 범위 내 / 고위험 비중 정상",
                ]
            elif b_ok:
                buy_grade        = "B"
                lump_sum_allowed = True
                rsi_str          = f"{rsi:.1f}" if rsi is not None else "N/A"
                reason          += [
                    f"B등급 — RR {rr_ratio:.2f} / 소액 분할 매수 검토",
                    f"RSI {rsi_str} / 5일승률 {win_rate_5d:.1f}% 조건 충족",
                ]
            else:
                buy_grade = "C"
                if rsi_blocks_lump:
                    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
                    reason.append(f"RSI {rsi_str} 과열 — 신규 목돈 매수 제한")
                if not rr_valid:
                    reason.append("리스크보상비 미확정 — 자동 적립만 유지, 목돈 매수 금지")
                elif rr_ratio is not None and rr_ratio < 1.5:
                    reason.append(f"RR {rr_ratio:.2f} < 1.5 — B등급 기준 미달")
                if win_rate_5d is not None and win_rate_5d < 40.0:
                    reason.append(f"5일 승률 {win_rate_5d:.1f}% < 40% — A등급 기준 미달")
                if not reason:
                    reason += ["조건 미달 — C등급 관찰", "조건 충족 시 B 상향 가능"]

        # reason 최소 2개 보장
        if len(reason) < 2:
            reason.append(f"buy_grade={buy_grade} 확정")

        buy_allowed  = lump_sum_allowed and buy_grade in ("A", "B")
        hold_grade   = "A" if data_quality in ("A", "B", "C") else "D"
        hold_allowed = price_collected and data_quality in ("A", "B", "C")

        failure_conditions = []
        if not price_collected:
            failure_conditions.append("price_missing")

        results.append({
            "symbol":   ticker,
            "name":     name,
            "category": category,
            "data_status": {
                "price_collected":       price_collected,
                "volume_collected":      volume_collected,
                "news_collected":        news_collected,
                "technical_collected":   technical_collected,
                "fundamental_collected": fundamental_collected,
                "data_quality":          data_quality,
                "data_issues":           data_issues,
            },
            "trade_eligibility": {
                "buy_grade":    buy_grade,
                "sell_grade":   "PENDING",   # 3단계에서 구현
                "hold_grade":   hold_grade,
                "buy_allowed":  buy_allowed,
                "sell_allowed": False,        # 3단계에서 구현
                "hold_allowed": hold_allowed,
                "buy_type": {
                    "auto_dca_allowed":     auto_dca_allowed,
                    "lump_sum_buy_allowed": lump_sum_allowed,
                    "auto_dca_reason":      auto_dca_reason,
                    "lump_sum_reason":      "; ".join(reason[:2]) if reason else "",
                },
                "reason": reason,
            },
            "risk_reward": {
                "entry_price":       entry_p,
                "stop_price":        stop_p,
                "target_price":      target_p,
                "expected_gain_pct": round(gain_pct, 2) if gain_pct is not None else None,
                "expected_loss_pct": round(loss_pct, 2) if loss_pct is not None else None,
                "reward_risk_ratio": round(rr_ratio, 2) if rr_ratio is not None else None,
                "valid":             rr_valid,
            },
            "position_action": {
                "today_action":       "",
                "conditional_action": "",
                "forbidden_action":   "",
                "position_size":      "",
                "cash_impact":        "",
            },
            "failure_conditions": failure_conditions,
            "portfolio_owned":    ticker in _pf_owned_tickers,
        })

    return results


# ── TDE/LLM 충돌 감지 ────────────────────────────────────────────────────────
def detect_tde_llm_conflicts(report, tde_results, price_trigger_results=None):
    """
    LLM 본문과 TDE 결과가 충돌하는지 탐지한다.
    긍정형 매수/추매 표현만 감지하고 금지 표현은 false positive 방지한다.
    충돌 목록을 반환한다.
    """
    import re as _re
    conflicts = []
    if not isinstance(report, str):
        return conflicts
    # tde_results가 비어도 price_trigger_results 충돌 체크는 실행한다
    if not tde_results:
        tde_results = []

    ab_count = sum(
        1 for t in tde_results
        if t["trade_eligibility"]["buy_grade"] in ("A", "B")
    )
    # 실제 실행 가능한 A/B: rr_valid=True AND lump_sum_buy_allowed=True
    _valid_lump_ab_count = sum(
        1 for t in tde_results
        if t["trade_eligibility"]["buy_grade"] in ("A", "B")
        and t["risk_reward"]["valid"]
        and t["trade_eligibility"]["buy_type"]["lump_sum_buy_allowed"]
    )

    # 0. 역방향 충돌 (LLM: 신규 매수 불가 / TDE: 실행 가능 후보 존재)
    if _valid_lump_ab_count > 0:
        _no_buy_pats = [
            r"신규\s*매수\s*[:\s]\s*D",
            r"신규\s*매수\s*불가",
            r"신규\s*매수\s*실행표\s*없음",
            r"B등급\s*이상\s*종목\s*없음",
            r"B등급\s*이상\s*종목\s*0개",
            r"수익형\s*모드\s*OFF",
            r"신규\s*매수\s*금지",
        ]
        for _pat in _no_buy_pats:
            if _re.search(_pat, report):
                conflicts.append({
                    "type": "역방향 충돌 (LLM 불가 ↔ TDE 후보 존재)",
                    "detail": (
                        f"LLM 본문은 신규 매수 불가/실행표 없음으로 판단했지만 "
                        f"TDE 기준 실행 가능 A/B 후보 {_valid_lump_ab_count}개가 존재합니다. "
                        f"TDE buy_grade 또는 본문 판단 중 하나를 수정해야 합니다."
                    ),
                })
                break

    # 1. 신규 매수 가능 표현 충돌 (A/B 0개인데 매수 가능성 암시) — 라인 단위 검사
    # 같은 라인에 금지/불가/없음/보류/부적합/원칙적 단어가 있으면 충돌 아님
    if ab_count == 0:
        _DENY_WORDS_BUY = {"금지", "불가", "없음", "보류", "부적합", "원칙적"}
        _POS_BUY_PATS = [
            r"신규\s*매수\s*가능",                      # "신규 매수 가능 여부: 제한적" 포함
            r"신규\s*매수[^\n]{0,20}제한적",            # "신규 매수 가능 여부: 제한적" 변형 대응
            r"제한적\s*매수",
            r"신규\s*매수\s*(?:허용|검토)",
            r"신규\s*진입\s*\d+%\s*축소",
            r"신규\s*진입\s*가능",
        ]
        for _line in report.splitlines():
            if any(_dw in _line for _dw in _DENY_WORDS_BUY):
                continue
            for _pat in _POS_BUY_PATS:
                if _re.search(_pat, _line):
                    conflicts.append({
                        "type": "신규 매수 표현 충돌",
                        "detail": (
                            "LLM 본문에 신규 매수 가능/제한적 표현이 있지만 "
                            "TDE 기준 신규 목돈 매수 A/B 후보 0개입니다. "
                            "'신규 목돈 매수 금지, 기존 자동 적립만 유지 가능'으로 표현해야 합니다."
                        ),
                    })
                    break
            else:
                continue
            break

    # 2. 고위험 추매 충돌 — 라인 단위 검사
    # 같은 라인에 금지/불가/보류/하지 단어가 있으면 충돌 아님
    _HIGH_RISK = {"RGTI", "BEAM", "JOBY", "PWFL"}
    _hr_d = {
        t["symbol"] for t in tde_results
        if t["symbol"] in _HIGH_RISK
        and (t["trade_eligibility"]["buy_grade"] == "D" or t["category"] == "유형③")
    }
    # 충돌 조건: 종목명 라인 + 금지성 단어 없음 + (매수 후보 존재 OR 추매/추가매수 + 가능/허용/진입)
    _DENY_WORDS_HR = {"금지", "불가", "보류", "하지", "축소", "매도", "유지", "보유"}
    _TRIGGER_HR    = ["추매", "추가매수", "추가 매수"]
    _AFFIRM_HR     = ["가능", "허용", "진입", "추천"]
    for _sym in _hr_d:
        for _line in report.splitlines():
            if _sym not in _line:
                continue
            if any(_dw in _line for _dw in _DENY_WORDS_HR):
                continue
            _has_buy_candidate     = "매수 후보" in _line
            _has_trigger_with_affirm = (
                any(_tr in _line for _tr in _TRIGGER_HR)
                and any(_af in _line for _af in _AFFIRM_HR)
            )
            if _has_buy_candidate or _has_trigger_with_affirm:
                conflicts.append({
                    "type": "고위험 추매 충돌",
                    "detail": f"{_sym}는 TDE D등급(유형③ 고위험)인데 본문에 추매/추가매수 가능 표현이 있습니다.",
                })
                break

    # 3. 셀트리온제약 물타기 충돌 — 블록 전체 문맥 판단
    _celltrion_d = any(
        t["symbol"] == "068760.KS" and t["category"] == "유형④"
        for t in tde_results
    )
    if _celltrion_d:
        _ct_search_keys = ["셀트리온제약", "셀트리온 제약", "068760"]
        _ct_lines = [
            _line for _line in report.splitlines()
            if any(_k in _line for _k in _ct_search_keys)
        ]
        if _ct_lines:
            _ct_block = "\n".join(_ct_lines)
            # 매수/물타기 긍정 신호 (이 표현이 블록에 있을 때만 검사)
            _ct_buy_pos = [
                "추매 가능", "물타기 가능", "추가매수 추천", "비중 확대",
                "하락 시 매수", "저점 매수", "분할매수", "신규 매수",
            ]
            # 방어/금지 표현 (하나라도 있으면 충돌 아님)
            _ct_deny = [
                "금지", "하지 말 것", "추매 금지", "물타기 금지",
                "신규 매수 금지", "축소", "리스크 재검토", "회복 전략 훼손",
                "전량 매도 금지", "반등 시 축소", "보유하되", "매수 금지",
                "추가매수 금지",
            ]
            _ct_has_pos  = any(_bp in _ct_block for _bp in _ct_buy_pos)
            _ct_has_deny = any(_bd in _ct_block for _bd in _ct_deny)
            if _ct_has_pos and not _ct_has_deny:
                conflicts.append({
                    "type": "셀트리온제약 물타기 충돌",
                    "detail": "셀트리온제약은 TDE 유형④(회복탈출관리)인데 본문에 추매/물타기 가능 표현이 있고 금지 표현이 없습니다.",
                })

    # 4. A/B 0개인데 오늘 주문 실행 가능 표현
    if ab_count == 0:
        for _pat in [r"오늘\s*실제\s*주문", r"신규\s*매수\s*실행", r"매수\s*실행"]:
            _m = _re.search(_pat, report)
            if _m:
                _ctx = report[max(0, _m.start() - 5): _m.end() + 20]
                if not _re.search(r"없음|금지|불가", _ctx):
                    conflicts.append({
                        "type": "주문 실행 표현 충돌",
                        "detail": (
                            "TDE 신규 목돈 매수 A/B 후보 0개인데 본문에 "
                            "오늘 주문/매수 실행 가능 표현이 있습니다."
                        ),
                    })
                    break

    # 5. 가격 조건 발동 표현 충돌 — TRIGGERED 종목인데 본문이 조건부 표현만 사용
    if price_trigger_results:
        for _tr in price_trigger_results:
            if _tr["status"] != "TRIGGERED":
                continue
            _cphrases = _tr.get("conflict_phrases", [])
            _aphrases = _tr.get("affirm_phrases", [])
            if not _cphrases or not _aphrases:
                continue
            _sym  = _tr["symbol"]
            _name = _tr["name"]
            _search_keys = [_name, _sym.replace(".KS", "").replace(".KQ", "")]
            # 종목명이 포함된 모든 라인을 블록으로 수집 (라인별 검사 → 블록 검사로 false positive 방지)
            _ticker_lines = [
                _line for _line in report.splitlines()
                if any(_k in _line for _k in _search_keys)
            ]
            if not _ticker_lines:
                continue
            _block = "\n".join(_ticker_lines)
            # 블록 전체에 발동 인정 표현이 하나라도 있으면 충돌 아님
            if any(_af in _block for _af in _aphrases):
                continue
            # 블록 전체에 조건부 표현이 있으면 충돌
            if any(_cf in _block for _cf in _cphrases):
                _cp_str = f"{_tr['condition_price']:,.0f}원" if (_sym.endswith(".KS") or _sym.endswith(".KQ")) else f"{_tr['condition_price']}달러"
                conflicts.append({
                    "type": "가격 조건 발동 표현 충돌",
                    "detail": (
                        f"{_name}은 {_cp_str} 기준을 이미 이탈했지만 "
                        f"본문이 조건부 표현만 사용했습니다. "
                        f"'{_tr.get('triggered_action', '이미 발동 — 즉시 재검토 필요')}'로 표현해야 합니다."
                    ),
                })

    return conflicts


def _short_tde_reason(tde_item):
    """TDE 항목에서 표 출력용 짧은 이유 문자열을 생성한다 (60자 이내)."""
    import re as _re2
    sym = tde_item["symbol"]
    cat = tde_item["category"]
    ds  = tde_item["data_status"]
    te  = tde_item["trade_eligibility"]

    if not ds["price_collected"]:
        return "현재가 미수집 — 매매 금지"

    raw_reasons = " ".join(te.get("reason", []))

    if sym == "GOOGL" and "370달러" in raw_reasons:
        return "370달러 이하 도달 — 모델 신뢰도 미회복+RR 미확정, 자동 적립만 유지"
    if sym == "FCX" and "60달러" in raw_reasons:
        return "60달러 초과+52주 고점권 — 자동 적립만 유지, 목돈 매수 금지"
    if sym == "VOO" and ("과열" in raw_reasons or "670달러" in raw_reasons):
        m = _re2.search(r"RSI\s*([\d.]+)", raw_reasons)
        rsi_str = m.group(1) if m else "?"
        return f"RSI {rsi_str} 과열/670달러 초과 — 자동 적립만 유지, 목돈 매수 금지"

    if cat == "유형④":
        return "유형④ 회복관리 — 물타기 금지"
    if cat == "유형③":
        return "유형③ 고위험 — 신규/추매 금지"
    if cat == "유형②":
        return "소액 스윙 — 비중 확대 금지"

    if te.get("reason"):
        r = te["reason"][0]
        return r[:50] if len(r) <= 50 else r[:47] + "..."
    return "-"


# ── 가격 조건 발동 판정 ───────────────────────────────────────────────────────

def evaluate_price_trigger_status(symbol, name, current_price, condition_price,
                                   condition_type, tolerance_pct=0.03):
    """현재가와 기준가를 비교해 가격 조건 상태를 판정한다.

    condition_type: "below" | "below_or_equal" | "above" | "above_or_equal"
    status 반환: "TRIGGERED" | "NEAR" | "NOT_REACHED" | "UNKNOWN"
    """
    base = {
        "symbol": symbol, "name": name,
        "current_price": current_price, "condition_price": condition_price,
        "condition_type": condition_type,
    }
    if current_price is None or condition_price is None or condition_price == 0:
        return {**base, "status": "UNKNOWN", "distance_pct": None,
                "message": "데이터 부족 — 수동 확인 필요"}

    if condition_type in ("below", "below_or_equal"):
        triggered = (current_price < condition_price) if condition_type == "below" \
                    else (current_price <= condition_price)
        # distance_pct: 양수 = 기준 위(아직 미도달), 음수 = 기준 아래(발동)
        distance_pct = (current_price - condition_price) / condition_price * 100
        if triggered:
            status, message = "TRIGGERED", "이미 발동 — 즉시 재검토 필요"
        elif distance_pct <= tolerance_pct * 100:
            status, message = "NEAR", "기준가 근접 — 알림 유지"
        else:
            status, message = "NOT_REACHED", "미도달 — 조건부 대기"
    elif condition_type in ("above", "above_or_equal"):
        triggered = (current_price > condition_price) if condition_type == "above" \
                    else (current_price >= condition_price)
        # distance_pct: 양수 = 기준 아래(아직 미도달), 음수 = 기준 위(발동)
        distance_pct = (condition_price - current_price) / condition_price * 100
        if triggered:
            status, message = "TRIGGERED", "이미 발동 — 즉시 재검토 필요"
        elif distance_pct <= tolerance_pct * 100:
            status, message = "NEAR", "기준가 근접 — 알림 유지"
        else:
            status, message = "NOT_REACHED", "미도달 — 조건부 대기"
    else:
        return {**base, "status": "UNKNOWN", "distance_pct": None,
                "message": "데이터 부족 — 수동 확인 필요"}

    return {**base, "status": status, "distance_pct": distance_pct, "message": message}


# 가격 조건 발동 감시 대상 종목 설정
_PRICE_TRIGGER_CONFIGS = [
    {
        "symbol": "068760.KS", "name": "셀트리온제약",
        "condition_price": 46500, "condition_type": "below",
        "triggered_action": "46,500원 기준 이미 이탈 — 회복 전략 훼손, 100~150주 축소 여부 즉시 재검토",
        "near_action": "46,500원 근접 — 알림 유지, 이탈 시 일부 축소 검토",
        "conflict_phrases": ["이탈 시", "도달 시", "알림 설정", "전까지 보유"],
        "affirm_phrases":   ["이미 발동", "즉시 재검토", "기준 이탈", "이미 이탈", "가격 발동", "회복 전략 훼손"],
    },
    {
        "symbol": "042700.KS", "name": "한미반도체",
        "condition_price": 280000, "condition_type": "below",
        "triggered_action": "280,000원 기준 이미 이탈 — 비중 1% 미만이므로 급매도보다 축소 여부 재검토",
        "near_action": "280,000원 근접 — 알림 유지",
        "conflict_phrases": ["이탈 시", "도달 시", "알림 설정", "전까지 보유"],
        "affirm_phrases":   ["이미 발동", "즉시 재검토", "기준 이탈", "이미 이탈", "가격 발동", "회복 전략 훼손"],
    },
    {
        "symbol": "RGTI", "name": "RGTI",
        "condition_price": 22, "condition_type": "below",
        "triggered_action": "22달러 기준 이미 이탈 — 절반 축소 검토",
        "near_action": "22달러 근접 — 손실 제한 감시 유지",
        "conflict_phrases": ["이탈 시", "도달 시", "알림 설정", "전까지 보유"],
        "affirm_phrases":   ["이미 발동", "즉시 재검토", "기준 이탈", "이미 이탈", "가격 발동"],
    },
    {
        "symbol": "BEAM", "name": "BEAM",
        "condition_price": 25, "condition_type": "below",
        "triggered_action": "25달러 기준 이미 이탈 — 절반 축소 검토",
        "near_action": "25달러 근접 — 손실 제한 감시 유지",
        "conflict_phrases": ["이탈 시", "도달 시", "알림 설정", "전까지 보유"],
        "affirm_phrases":   ["이미 발동", "즉시 재검토", "기준 이탈", "이미 이탈", "가격 발동"],
    },
    {
        "symbol": "035420.KS", "name": "네이버",
        "condition_price": 287000, "condition_type": "above_or_equal",
        "triggered_action": "287,000원 도달 — 5~10주 분할 익절 검토",
        "near_action": "287,000원 근접 — 익절 알림 유지",
        "conflict_phrases": [],
        "affirm_phrases":   [],
    },
    {
        "symbol": "GOOGL", "name": "GOOGL",
        "condition_price": 370, "condition_type": "below_or_equal",
        "triggered_action": "370달러 이하 도달 — 단, 모델 신뢰도/신규 매수 모드 조건 통과 전까지 목돈 매수 보류",
        "near_action": "370달러 근접 — 추가 일시매수 조건 감시",
        "conflict_phrases": [],
        "affirm_phrases":   [],
    },
    {
        "symbol": "VOO", "name": "VOO",
        "condition_price": 670, "condition_type": "below_or_equal",
        "triggered_action": "670달러 이하 도달 — 추가 일시매수 조건 충족, RSI/신규 매수 모드 병행 확인",
        "near_action": "670달러 근접 — 적립재개 조건 감시",
        "conflict_phrases": [],
        "affirm_phrases":   [],
    },
    {
        "symbol": "FCX", "name": "FCX",
        "condition_price": 60, "condition_type": "below_or_equal",
        "triggered_action": "60달러 이하 도달 — 추가 일시매수 조건 충족, RSI/신규 매수 모드 병행 확인",
        "near_action": "60달러 근접 — 추가 일시매수 조건 감시",
        "conflict_phrases": [],
        "affirm_phrases":   [],
    },
]


def build_price_trigger_table(all_stock_data: dict) -> list:
    """가격 조건 발동 상태를 평가해 결과 리스트를 반환한다."""
    results = []
    for cfg in _PRICE_TRIGGER_CONFIGS:
        sym = cfg["symbol"]
        data = all_stock_data.get(sym, {})
        current_price = _safe_float(data.get("현재가"))
        result = evaluate_price_trigger_status(
            symbol=sym, name=cfg["name"],
            current_price=current_price,
            condition_price=cfg["condition_price"],
            condition_type=cfg["condition_type"],
        )
        result["triggered_action"] = cfg["triggered_action"]
        result["near_action"]      = cfg["near_action"]
        result["conflict_phrases"] = cfg.get("conflict_phrases", [])
        result["affirm_phrases"]   = cfg.get("affirm_phrases", [])
        results.append(result)
    return results


def render_price_trigger_section(trigger_results: list) -> str:
    """가격 조건 발동 검증표 섹션 문자열을 생성한다 (코드 직접 생성)."""
    if not trigger_results:
        return ""

    _EMOJI = {
        "TRIGGERED":  "🚨 이미 발동",
        "NEAR":       "⚠️ 근접",
        "NOT_REACHED": "대기",
        "UNKNOWN":    "데이터 부족",
    }

    triggered_names = [r["name"] for r in trigger_results if r["status"] == "TRIGGERED"]
    lines = ["🚨 가격 조건 발동 검증", ""]

    if triggered_names:
        lines.append(f"⚠️ 가격 조건 발동 종목 있음: {', '.join(triggered_names)}")
        lines.append("")

    lines += [
        "| 종목 | 현재가 | 기준가 | 상태 | 판단 |",
        "| --- | --- | --- | --- | --- |",
    ]

    for r in trigger_results:
        sym  = r["symbol"]
        is_kr = sym.endswith(".KS") or sym.endswith(".KQ")
        unit  = "원" if is_kr else "달러"

        if r["current_price"] is not None:
            cur_str = (f"{r['current_price']:,.0f}{unit}" if is_kr
                       else f"{r['current_price']:.2f}{unit}")
        else:
            cur_str = "-"

        cp = r["condition_price"]
        cond_raw = (f"{cp:,.0f}{unit}" if is_kr else f"{cp}{unit}")
        _cond_label_map = {
            "below":          f"{cond_raw} 이탈",
            "below_or_equal": f"{cond_raw} 이하",
            "above":          f"{cond_raw} 초과",
            "above_or_equal": f"{cond_raw} 도달",
        }
        cond_label = _cond_label_map.get(r["condition_type"], cond_raw)
        status_str = _EMOJI.get(r["status"], r["status"])

        if r["status"] == "TRIGGERED":
            action = r.get("triggered_action", r["message"])
        elif r["status"] == "NEAR":
            action = r.get("near_action", r["message"])
        elif r["status"] == "NOT_REACHED":
            action = "감시 유지"
        else:
            action = "수동 확인 필요"

        lines.append(f"| {r['name']} | {cur_str} | {cond_label} | {status_str} | {action} |")

    lines.append("")
    return "\n".join(lines)


# ── TDE 보고서 섹션 렌더러 (검증 요약) ──────────────────────────────────────────
def render_tde_report_section(tde_results):
    """TDE 결과를 '🔍 TDE 최종 검증 요약' 축약 섹션으로 변환한다."""
    if not tde_results:
        return (
            "🔍 TDE 최종 검증 요약\n\n"
            "TDE 결과 없음 — 데이터 구조 확인 필요"
        )

    total   = len(tde_results)
    passed  = sum(1 for t in tde_results if t["data_status"]["data_quality"] in ("A", "B", "C"))
    ab_list = [
        t["name"] for t in tde_results
        if t["trade_eligibility"]["buy_grade"] in ("A", "B")
        and t["risk_reward"]["valid"]
        and t["trade_eligibility"]["buy_type"]["lump_sum_buy_allowed"]
    ]
    dca_list = [t["name"] for t in tde_results
                if t["trade_eligibility"]["buy_type"]["auto_dca_allowed"]]
    invalid_ab = [
        t for t in tde_results
        if t["trade_eligibility"]["buy_grade"] in ("A", "B")
        and not (t["risk_reward"]["valid"]
                 and t["trade_eligibility"]["buy_type"]["lump_sum_buy_allowed"])
    ]

    data_status = "정상" if passed == total else f"일부 미수집 ({total - passed}개)"
    ab_status   = f"{', '.join(ab_list)} 매수 가능" if ab_list else "신규 목돈 매수 금지"
    dca_str     = ", ".join(dca_list) if dca_list else "없음"

    lines = ["🔍 TDE 최종 검증 요약", ""]
    lines += [
        "| 항목 | 결과 | 판단 |",
        "| --- | --- | --- |",
        f"| 실시간 데이터 검증 | {passed}개 / {total}개 | {data_status} |",
        f"| 신규 목돈 매수 A/B 후보 | {len(ab_list)}개 | {ab_status} |",
        f"| 자동 적립(DCA) 가능 | {dca_str} | 단, 목돈 매수와 구분 |",
        "| TDE/LLM 충돌 | 아래 참고 | 충돌 있으면 실전 매매 금지 |",
    ]
    if ab_list:
        lines.append(f"| 오늘 매수 가능 종목 | {', '.join(ab_list)} | 수동 확인 후 실행 |")
    lines.append("")
    if invalid_ab:
        lines.append(
            f"⚠️ TDE 경고: A/B 등급인데 리스크보상비 미확정 종목 {len(invalid_ab)}개 — "
            "4단계 실패 조건 검증 필요."
        )
        lines.append("")
    lines.append("오늘 금지 행동/보유 종목 액션 — 상단 실행표 및 최종 실행 플랜 참고")
    lines.append("")

    return "\n".join(lines)


# ── 보고서 생성 ───────────────────────────────────────────────────────────────
def generate_report(us_data, kr_data, exchange_rate,
                    macro_data, fear_greed, insider_trades,
                    congress_trades, put_call_ratio, portfolio_data=None,
                    news_data=None, earnings_data=None, patterns_str=None,
                    price_trigger_str=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today        = _report_date_kst_str()       # KST 기준 날짜 (예: 2026년 06월 03일)
    generated_at = _report_timestamp_kst_str()  # KST 기준 타임스탬프 (예: 2026-06-03 07:17 KST)

    pf           = portfolio_data if portfolio_data is not None else _HARDCODED_PORTFOLIO
    _pf_section  = _portfolio_section_str(pf, exchange_rate)
    _restricted  = _restricted_tickers_list(pf)

    all_stock_data = {**us_data, **kr_data}
    portfolio_calc = calc_portfolio_summary(pf, all_stock_data, exchange_rate)
    yesterdays_verification = build_yesterdays_verification(all_stock_data, exchange_rate)

    # 전일 실행 내역 요약
    executed = pf.get("executed_actions", [])
    from datetime import datetime as _dt, timedelta as _td
    yesterday = (_dt.now() - _td(days=1)).strftime("%Y-%m-%d")
    today_str = _dt.now().strftime("%Y-%m-%d")
    recent_executed = [
        a for a in executed
        if a.get("date") in [yesterday, today_str]
    ]
    if recent_executed:
        exec_lines = ["[전일/오늘 실행된 매매 내역 — 보고서 판단에 반영하라]"]
        for a in recent_executed:
            action = a.get("action", "")
            name = a.get("name", "")
            ticker = a.get("ticker", "")
            shares = a.get("shares", 0)
            memo = a.get("memo", "")
            exec_lines.append(f"  {action} 완료: {name}({ticker}) {shares}주 — {memo}")
        executed_summary = "\n".join(exec_lines)
    else:
        executed_summary = "전일 실행 내역 없음"

    backtest_result = run_backtest(pf, all_stock_data, exchange_rate)

    static_system = """너는 세계 최상위 퀀트 트레이더, 포트폴리오 매니저, 리스크 매니저, 기업가치 분석가, 매크로 전략가 역할을 동시에 수행한다.
목표는 사용자가 실제로 돈을 버는 것이다. 큰 수익보다 큰 손실 회피를 항상 우선한다.

[퀀트 핵심 원칙 — 보고서 전반에 적용]
1. 대손실 회피 우선: 수익 기회보다 큰 손실 방지를 항상 우선한다. 손실 -20%를 복구하려면 +25%가 필요하다.
2. 모델 신뢰도 연동: 모델 정확도가 낮을수록 매수 규모를 자동 축소한다. 정확도 30% 미만 = 신규 목돈 매수 금지.
3. 반복 실패 패턴 금지: learning_log의 실패 패턴과 동일한 결정을 반복하지 않는다.
4. 현금도 포지션이다: 현금 보유는 소극적 행동이 아니라 능동적 리스크 관리다.
5. 확증 편향 금지: 매수하고 싶은 종목에 대해 긍정 근거만 찾지 않는다.
6. 비대칭 손익 인식: 기대수익/손실비 2:1 미만이면 진입하지 않는다.
7. TDE 우선 원칙: TDE 판단이 LLM 본문 판단보다 항상 우선한다. TDE 결과를 임의로 상향하지 않는다.

[출력 형식 원칙 — 반드시 준수]
이 보고서는 이메일로 발송된다. 마크다운 문법을 사용하지 마라.
## 헤더, --- 구분선, ``` 코드블록, ** 볼드 같은 마크다운 문법은 절대 사용 금지.
이모지와 일반 텍스트, 표(|로 구성)만 사용해라.
보고서 길이는 이메일에서 잘리지 않도록 핵심 내용 위주로 작성해라.
섹션 구분은 이모지로 해라. 예: 📊 📈 💼 ⭐ 💡

주의: 아래 [종목 분류 체계]에서 말하는 "고위험 옵션성 베팅"은 보고서 내 종목 위험 분류일 뿐이다.
기존 코드의 category3 또는 500만원→1억 프로젝트 섹션을 부활시키면 안 된다.
category3 자금 계산, category3_cash, category3_seed, cat3_total 출력은 전부 제거된 상태다.



[보고서 목적 — 듀얼 엔진]
이 보고서는 두 엔진을 동등한 비중으로 운영한다.
A. 보유 포트폴리오 관리 엔진: 손익/리스크/실행 전략
B. 신규 수익 기회 발굴 엔진: 퀀트 필터 통과 후보 탐색
둘 중 하나만 메인으로 두지 마라. 상단에서 A와 B를 동등하게 다뤄라.

[보고서 출력 순서 — 반드시 이 순서를 지켜라]
0. 📌 오늘의 결론 한 장 요약
0.5. 🚦 오늘 보고서 실전 사용 등급
1. 💼 내 포트폴리오 전체 현황
2. ✅ 오늘 보유 종목 실행표
3. 📰 실시간 뉴스/이슈 영향 분석
4. 📊 보유 종목별 상세 전략
5. 🎯 신규 수익 발굴 TOP 5
6. 🧠 퀀트 필터 통과/탈락
7. 💰 매수 가능 금액/포지션 크기
8. 📈 상승/하락 시나리오
9. ⚠️ 리스크 경고판
10. 🔍 데이터 신뢰도 검증
11. 📌 최종 실행 플랜

[종목 분류 체계 — 4단계, 전 섹션에 적용]
유형① — 핵심 장기 보유/적립: VOO, GOOGL, FCX
  조건: 장기 적립 기본. 하락 시 손절보다 적립/비중 조절.
유형② — 성장 스윙/고성장 관리: NVDA, PLTR, 네이버, 카카오, 한미반도체
  조건: 성장성 있으나 변동성 큼. 분할매수/매도 기준 필수.
유형③ — 고위험 옵션성 베팅: RGTI, BEAM, JOBY, PWFL
  조건: 적자/테마주. 신규/추매 엄격 제한. 합산 15% 이하.
유형④ — 회복/탈출 관리: 셀트리온제약
  조건: 신규 매수 금지. 손실 복구와 리스크 축소 우선.
표기 원칙: "유형①/②" 복합 표기 금지. 하나로만.

[전일 정확도 연동 — 모든 매수 판단에 적용]
- 70% 이상: 기준 유지
- 50~70%: 신규 진입 30% 축소
- 30~50%: 신규 진입 50% 축소, A등급 금지
- 30% 미만: 신규 목돈 매수 금지. "원칙적 금지" 또는 "관망"으로 표시. C 관찰 후보는 출력 가능.
- 10% 미만: 전체 관망, 보고서 상단 ⚠️ 모델 신뢰도 경고 표시

[섹션 0. 📌 오늘의 결론 한 장 요약]
보고서 맨 위에 반드시 출력. 사용자가 1분 안에 오늘 행동 전체를 이해해야 함.

📌 오늘의 결론 한 장 요약

| 구분 | 오늘 판단 | 핵심 이유 | 내가 할 일 |
|------|-----------|-----------|-----------|
| 보유관리 | (예: 셀트리온/RGTI 리스크 우선) | (가격 조건 발동 등) | (축소 여부 검토 등) |
| 신규수익발굴 | (예: A후보 없음 / C후보 N개) | (모델 신뢰도/시장위험) | (관찰 후보 추적) |
| 현금 운용 | (X% 유지/확대/축소) | (방어 모드/공격 모드) | (목돈 보류/분할 가능) |
| 오늘 매수 | (없음 or 종목 소액 가능) | (조건 충족 여부) | (0원 / 금액) |
| 오늘 금지 | (고위험 추매 금지 등) | (실패패턴/모델 신뢰도) | (구체 종목 나열) |
| 가격 조건 발동 | (발동 종목 or 없음) | (TRIGGERED 여부) | (즉시 재검토 or 없음) |

작성 원칙:
- 실제 수치 채울 것. 빈칸/대략 금지.
- 가격 조건 발동 종목이 있으면 반드시 종목명 명시.

[섹션 0.5. 🚦 오늘 보고서 실전 사용 등급]
보고서 상단에 오늘의 결론 직후 출력. 실전 매매에 쓸 수 있는 수준인지 판단.

🚦 오늘 보고서 실전 사용 등급

| 용도 | 등급 | 사용 가능 여부 | 이유 |
|------|------|-------------|------|
| 보유 포트폴리오 관리 | A/B/C/D | 가능/조건부/불가 | |
| 손실 제한 판단 | A/B/C/D | 가능/조건부/불가 | |
| 수익 실현/익절 판단 | A/B/C/D | 가능/조건부/불가 | |
| 신규 수익 후보 관찰 | A/B/C/D | 가능/조건부/불가 | |
| 신규 매수 실행 | A/B/C/D | 가능/조건부/불가 | |
| 자동매매 연결 | D | 불가 | 사용자 수동 확인 필수 |

등급 기준:
- A: 데이터 정상 + 논리 오류 없음 + 실행 조건 명확
- B: 데이터 대부분 정상 + 수동 확인 필요
- C: 관찰 가능하지만 실행 불가
- D: 사용 불가

모델 정확도 30% 미만이면: 신규 매수 실행 무조건 D. 신규 후보 관찰은 B/C 가능. 보유관리는 데이터 정상일 때 A/B 가능.

[섹션 1. 💼 내 포트폴리오 전체 현황]
[포트폴리오 사전 계산값]을 그대로 사용해라. 직접 계산 금지.

💼 내 포트폴리오 전체 현황

| 항목 | 금액/비중 | 판단 |
|------|-----------|------|
| 총 원금 | 확정 또는 "추정" 명시 | - |
| 현재 평가금액 | 원화 환산 | - |
| 총 평가손익 | 원화 | 수익/손실 |
| 총 수익률 | % | - |
| 현금 | 원화 + % | 비중 |
| 한국 주식 비중 | % | 편중 여부 |
| 미국 주식 비중 | % | - |
| 고위험(유형③) 합산 | % | 10% 초과 경고 |
| 최대 단일 종목 | 종목명 + % | 20%/25% 경고 |
| 오늘 매수 가능 예산 | 원화 | 모델 신뢰도 기준 |

보유 종목 전체표:

| 종목 | 유형 | 원금 | 현재잔액 | 평가손익 | 수익률 | 비중 | 최근등락률 | 뉴스영향 | 오늘 액션 |
|------|------|------|----------|----------|--------|------|-----------|---------|----------|

오늘 액션 단어: 보유 / 축소검토 / 익절검토 / 손절검토 / 추매금지 / 적립유지 / 데이터확인

규칙:
- portfolio.json에 평단/수량이 있으면 원금은 확정값으로 표시. 추정이면 "추정" 명시.
- 현재가 있으면 현재잔액은 확정 평가값. 없으면 "산출 불가"로 표시.
- 총자산/총손익/수익률은 "현재가 수집 완료 종목 기준"임을 명시.
- 현재가 미수집이면 총자산/비중 "산출 불가" 표시. 현금만으로 총자산 확정값 출력 금지.
- ⚠️ 단일 종목 20% 초과, 고위험 합산 10% 초과, 현금 5% 미만 자동 경고.
- 표 아래에 반드시 추가: ※ 총 원금/총손익은 현재가 수집 완료 종목 기준. 미수집 종목은 별도 표기.

[섹션 2. ✅ 오늘 보유 종목 실행표]
보유 종목 전체를 중요도 순 정렬. 가격 조건 발동 종목 맨 위.

✅ 오늘 보유 종목 실행표

| 우선순위 | 종목 | 현재 상태 | 오늘 할 일 | 조건부 행동 | 금지 행동 | 이유 |
|----------|------|-----------|-----------|-----------|----------|------|

규칙:
- 가격 조건 TRIGGERED 종목: "오늘 할 일"에 즉시 재검토 행동 명시.
- 자동매도 표현 금지. 데이터 미수집을 매도 근거로 쓰지 마라.
- 액션 없는 종목은 "보유 유지" 한 줄로.

[섹션 3. 📰 실시간 뉴스/이슈 영향 분석]
📰 실시간 뉴스/이슈 영향 분석

| 종목/시장 | 이슈 | 출처등급 | 신뢰도 | 가격 반영 여부 | 단기 영향 | 1~6개월 영향 | 행동 |
|-----------|------|---------|------|------------|---------|------------|------|

출처등급: A(공시/실적/정책/FDA) / B(목표가/기관/파트너십) / C(일반기사/블로그)
신뢰도: 높음 / 중간 / 낮음
가격 반영 여부: 미반영 / 일부 반영 / 대부분 반영 / 과잉 반영 / 확인 필요
영향 분류: 강한 호재 / 약한 호재 / 중립 / 약한 악재 / 강한 악재 / 혼조 / 확인 필요

포함 뉴스 유형: 기업/실적/수급/내부자거래/정책/정부지원/SNS 관심도/내한·이벤트/거시/섹터/경쟁사

규칙:
- 뉴스가 주가에 미칠 영향을 반드시 분석. "뉴스 있음"으로 끝내지 마라.
- 가격에 선반영됐는지/반영 여지가 있는지 판단.
- 뉴스가 좋아도 가격 급등했으면 "추격 금지/눌림목 대기"로 표시.
- 뉴스가 나빠도 과매도면 "즉시 매도"가 아니라 가격 조건 확인으로 표시.
- SNS/내한/이벤트성 이슈는 단기 모멘텀과 실적 연결 가능성을 분리해서 판단.
- 기업 뉴스와 거시 뉴스가 충돌하면 "혼조"로 표시.
- 단일 소스 뉴스는 "확인 필요" 표시.
- 뉴스 A등급: 4번 보유 종목별 상세 전략에 반드시 반영.

[섹션 4. 📊 보유 종목별 상세 전략]
📊 보유 종목별 상세 전략

| 종목 | 현재가 | 수익률 | 핵심 시나리오 | 상승 목표 | 하락 리스크 | 전략 | 이유 |
|------|--------|--------|-------------|---------|-----------|------|------|

각 종목별 반드시 포함:
- 왜 보유하는가 / 왜 축소/익절/손절/추매금지인가
- 목표가 / 손절·축소 기준 / 예상 상승률 / 예상 하락률 / 시간축(단기/중기/장기)
- 상승 시나리오: 기본 / 강세
- 하락 시나리오: 기본 / 약세

예상 상승률/하락률 원칙:
- 무조건 ±10% 이내로 제한하지 마라.
- 데이터/뉴스/거래량/저점/섹터 모멘텀이 강하면 +20~50% 이상 시나리오 허용.
- 적자·테마·고위험 종목은 -30~-70% 하락 리스크도 명시.
- 조건과 근거를 반드시 붙여라.

출력 예시:
GOOGL: 상승(기본) AI 인프라 재평가 +15~25% / 강세 리스크온 +30% / 하락 350이탈 -10~-18%
RGTI: 상승 양자 정부지원 확정 +30~80% / 하락 내부자매도 지속+19달러이탈 -30~-60%

[섹션 5. 🎯 신규 수익 발굴 TOP 5]
신규 후보가 없더라도 관찰 후보 TOP 5는 반드시 출력. "후보 없음"으로 끝내지 마라.

🎯 신규 수익 발굴 TOP 5

| 순위 | 종목 | 유형 | 구분 | 현재가 | 후보 이유 | 진입 조건 | 손절가 | 목표가 | 예상 상승률 | 예상 하락률 | RR | 데이터 검증 | 등급 |
|------|------|------|------|--------|---------|---------|--------|--------|-----------|-----------|-----|---------|------|

유형: 안정형 / 추세형 / 이벤트형 / 보유추가형 / 현금대체형
구분: 신규매수 / 추가매수 / 관찰후보 / 돌파후보 / 눌림목후보 / 이벤트후보 / 실적후보
등급: A(오늘 매수 가능) / B(소액 분할 가능) / C(관찰) / D(제외)
데이터 검증: 통과 / 단일소스 / 검증필요 / 실패

후보 유형 목표 배분:
- 안정형 1~2개: VOO, QQQ, SCHD, GOOGL, MSFT, FCX, GLD
- 추세형 2~3개: HD현대일렉트릭, LS ELECTRIC, 한화에어로스페이스, 현대로템, 두산에너빌리티, LG전자, SK하이닉스, 한미반도체, AVGO, AMD, TSM, ASML, NVDA, PLTR, META, AMZN
- 이벤트형 1개 이상: 정책/실적/수주/정부지원/내한/섹터 리레이팅 — 가격/거래량/수급 확인 필수

후보 유니버스:
미국: VOO, QQQ, SCHD, GOOGL, MSFT, AMZN, META, NVDA, AVGO, AMD, TSM, ASML, COST, LLY, UNH, JPM, XOM, CVX, NOC, LMT, FCX, GLD, TLT, PLTR
한국: 삼성전자, SK하이닉스, 한미반도체, 네이버, 카카오, LG전자, 현대차, 기아, 두산에너빌리티, HD현대일렉트릭, LS ELECTRIC, 한화에어로스페이스, 현대로템
보유 종목(추가매수 가능): GOOGL, VOO, FCX 등

선정 원칙:
- 꼭 대형주만 고르지 말 것. 제2의 SK하이닉스/LG전자/엔비디아처럼 추세가 강하게 붙을 수 있는 후보 탐색.
- 종목보다 로직 우선. 단일 스토리보다 검증된 조건 우선.
- 데이터 없으면 제외. A/B가 없으면 C 관찰 후보라도 5개 출력.
- 예상 상승률/하락률/RR 전부 필수. 없으면 실패.
- 모델 정확도 30% 미만이면 A/B 매수 금지. C 관찰 후보는 출력 가능. 권장 매수금액 전부 0원.
- 전략 분산: 안정형/추세형/이벤트형 고루.

TOP 5 제외 규칙:
- 셀트리온제약, RGTI, BEAM — 신규 수익 발굴 TOP 5 절대 금지. 보유관리 섹션만.
- 보유 종목이 전체 5개 중 3개를 초과하면 안 됨.
- 네이버 비중 18% 이상이면 신규 후보 아님 — 익절/관리 후보로 표시.
- VOO는 안정형/현금대체형으로만 분류.
- 데이터 미수집 종목은 TOP 5 절대 금지. "데이터 확인 필요" 부록에만 허용.
- 손절가 >= 현재가이면 A/B 금지.
- 목표가 <= 현재가이면 A/B 금지.
- 단일 소스 수치이면 A/B 금지, C 관찰만 허용.

수치 교차검증 결과 표 (TOP 5 아래 반드시 출력):

| 후보 | 가격 검증 | 등락률 검증 | 실적/뉴스 수치 검증 | 손절/목표 구조 | 최종 |
|------|---------|----------|------------|---------|------|

판정: 통과 / 단일소스 / 검증필요 / 실패

[섹션 6. 🧠 퀀트 필터 통과/탈락]
🧠 퀀트 필터 통과/탈락

| 종목 | 데이터 | 가격위치 | 거래량 | 뉴스/이슈 | 추세 | RR | 실패패턴 | 포트비중 | 최종 |
|------|--------|---------|--------|---------|------|-----|---------|---------|------|

필터 10개:
1. 데이터 정상 수집
2. 가격 조건 또는 추세 조건 충족
3. 거래량 증가 또는 수급 개선
4. 뉴스/이슈가 가격에 미칠 영향 명확
5. 손절가 < 현재가 < 목표가
6. RR 1.5 이상
7. 최근 실패 패턴 회피 (learning_log 기반)
8. 포트폴리오 비중 적합 (고위험 15% 이하, 단일 20% 이하)
9. 모델 신뢰도 반영
10. 거시환경 리스크 반영 (VIX/금리/달러)

[섹션 7. 💰 매수 가능 금액/포지션 크기]
💰 매수 가능 금액/포지션 크기

| 종목 | 등급 | 권장 매수금액 | 1차 | 2차 | 3차 | 최대 비중 | 이유 |
|------|------|------------|-----|-----|-----|---------|------|

포지션 크기 원칙:
- A 후보: 총자산 2~5%
- B 후보: 총자산 1~2%
- C 후보: 0원, 관찰만
- 모델 정확도 30% 미만: 신규 목돈 매수 0원
- 모델 정확도 30~50%: 최대 100만 원
- 고위험 적자기업 신규매수 금지 (RGTI/BEAM/셀트리온 제외)
- ETF/우량주는 분할매수 가능
- 현금 비중 15% 이하이면 신규매수 제한
- 시장위험 높으면 분할만 허용

[섹션 8. 📈 상승/하락 시나리오]
보유 종목과 신규 후보 모두 포함.

📈 상승/하락 시나리오

| 종목 | 기본 시나리오 | 강세 시나리오 | 약세 시나리오 | 트리거 |
|------|------------|------------|------------|--------|

규칙:
- 예상 상승률/하락률 명확히 표시. 조건 있으면 +20~50% 이상 가능.
- 테마주/적자주는 -30~-70% 리스크도 표시.
- 목표가/손절가가 현재가와 논리적으로 맞아야 함.
- 예상 상승률/하락률이 전부 10% 이내면 실패.

[섹션 9. ⚠️ 리스크 경고판]
현재 포트폴리오의 주요 리스크를 수치로 표시.

⚠️ 리스크 경고판

| 리스크 항목 | 현재 수준 | 경고 기준 | 판단 |
|-----------|---------|---------|------|
| 고위험 합산 비중 | X% | 10%/15% | 정상/경고/과다 |
| 단일 종목 집중 | 종목명 X% | 20%/25% | 정상/경고 |
| 손실 중인 종목 | N개 / X% 손실 | - | - |
| 현금 비중 | X% | 5%/10% | 정상/부족 |
| 모델 정확도 | X% | 30%/50% | 정상/저하/경보 |
| 시장 위험도 | VIX/거시 | - | 낮음/중간/높음 |
| 가격 조건 발동 | N개 종목 | - | 있음/없음 |

[섹션 10. 🔍 데이터 신뢰도 검증]
10번 섹션에 아래를 모두 출력해라:

(a) 데이터 신뢰도:
| 데이터 | 기준 시점 | 신뢰도 | 비고 |
|--------|---------|--------|------|
(미국 주식 실시간/전일/지연 구분, 한국 주식 전일 종가, 거시지표 수집 시점)

각 데이터 옆에 표시:
✅ 검증 완료 / 🟡 단일 소스 / ⚠️ 불일치 / ❌ 미수집

미국/한국 데이터 기준일 표기 원칙:
- 미국 주식 정규장 당일: "미국 주식: 정규장 종가/장중 데이터 — YYYY-MM-DD 기준"
- 미국 주식 주말/휴장일: "미국 주식: 최근 정규장 종가 기준 — YYYY-MM-DD"
- 날짜가 토요일/일요일이면 "전일 종가"라고 단정 금지.
- 한국 주식 휴장일이면: "한국 주식: 최근 정규장 종가 기준"

(b) TDE 실전 매매 판단 결과:
TDE 섹션은 코드가 계산한 결과이므로 LLM 본문 판단보다 우선. 임의 상향 금지.
- 신규 목돈 매수 A/B 후보 수
- 자동 적립 유지 가능 종목
- 가격 조건 발동 종목 및 상태

[섹션 11. 📌 최종 실행 플랜]
📌 최종 실행 플랜

1. 오늘 보유 종목 행동: (종목별 오늘 할 일 한 줄씩)
2. 오늘 신규 매수: (없음 or B후보 금액)
3. 오늘 관찰 후보: (종목 나열)
4. 오늘 금지: (구체 종목 + 행동)
5. 현금 운용: (비중 유지/변경)
6. 다음 매수 전환 조건: (모델 정확도 기준 + 후보 조건)

[보고서 길이 원칙]
- 7~9페이지 수준 목표
- 상단 2페이지 안에 오늘 결론/포트폴리오/신규 후보 요약이 보여야 함
- 같은 내용 2번 이상 표시 금지: 가격 조건은 상단 실행표+가격조건표에서만
- 셀트리온/RGTI 설명은 상세 전략에서 1회, 실행표에서 1회만
- TDE는 코드가 자동으로 하단에 추가하므로 본문에서 TDE 표 반복 금지
- "신규 매수 금지"는 상단 요약/포지션 크기/최종 실행 플랜에만
- 최종 실행 플랜은 요약형으로만

[보고서 실패 조건 — 하나라도 발생 시 마지막에 반드시 표시]
아래 조건이면 "❌ 보고서 실패 — 실전 매매 사용 금지"를 출력해라:
1. 신규 후보 TOP 5가 없음
2. 신규 후보 TOP 5에 진입가/손절가/목표가/RR/상승률/하락률 중 하나라도 없음
3. 수치 검증 실패 후보가 TOP 5에 남아 있음
4. 모델 정확도 30% 미만인데 신규 A/B 매수 제안 또는 매수금액 > 0
5. 셀트리온제약/RGTI/BEAM이 신규 수익 발굴 TOP 5에 들어감
6. 현재가 미수집 종목이 A/B/C 후보로 TOP 5에 들어감
7. 보유 포트폴리오 총액이 현금만으로 확정 표시됨
8. 뉴스가 있는데 주가 영향 분석이 없음
9. 예상 상승률/하락률이 전부 ±10% 이내
10. TDE/LLM 충돌이 있고 그 충돌이 미해결 상태
11. 손절가 >= 현재가인데 A/B 후보
12. 목표가 <= 현재가인데 A/B 후보
13. "오늘 매수"와 "오늘 금지"가 같은 종목에 충돌
14. 미국/한국 기준일이 휴장일인데 "전일 종가"로 잘못 표기
15. 단일 소스 수치를 확정값처럼 표시

경고로만 처리할 조건 (매수 실행으로 연결되면 실패):
- 모델 정확도 낮음
- 신규 A/B 후보 0개
- 데이터 단일소스
- 보유 고위험 비중 경고

[TDE 실전 매매 판단 엔진 우선 원칙]
TDE 섹션은 코드가 계산한 결과이므로, LLM 본문 판단보다 우선한다. LLM은 TDE의 buy_grade, 자동 적립 가능 여부, 신규 목돈 매수 금지 판단을 임의로 상향하거나 덮어쓰지 않는다.
TDE 기준 신규 목돈 매수 A/B 후보가 0개일 경우, '신규 매수 가능', '제한적 매수', '일부 신규 진입 가능'처럼 실행 가능성을 암시하는 표현을 쓰지 않는다.
자동 적립 가능(DCA 유지)과 신규 목돈 매수 가능(buy_grade A/B + risk_reward valid)은 다른 개념이다.

[가격 조건 발동 표현 원칙 — 반드시 준수]
user_content의 [가격 조건 발동 상태]에 TRIGGERED 종목이 있을 경우:
1. 해당 종목에 대해 "이탈 시", "도달 시", "알림 설정"만 사용하지 마라.
2. TRIGGERED 종목은 반드시 "이미 발동 — 즉시 재검토 필요" 또는 이에 준하는 표현을 사용해야 한다.
3. TRIGGERED가 곧 자동매도를 의미하지 않는다. 종목별 기존 전략에 따라 표현해라:
   - 셀트리온제약 46,500원 이탈: "46,500원 기준 이미 이탈 — 회복 전략 훼손, 100~150주 축소 여부 즉시 재검토"
   - 한미반도체 280,000원 이탈: "280,000원 기준 이미 이탈 — 비중 1% 미만이므로 급매도보다 축소 여부 재검토"
   - RGTI 22달러 이탈: "22달러 기준 이미 이탈 — 절반 축소 검토"
   - BEAM 25달러 이탈: "25달러 기준 이미 이탈 — 절반 축소 검토"
4. NOT_REACHED 또는 NEAR 상태에서만 미래형 표현("이탈 시", "도달 시")을 허용한다.
5. 실전 매매 실행표에서 TRIGGERED 상태 종목은 오늘 실행 또는 즉시 재검토 칸에 반드시 반영해라.
6. 보고서 상단 핵심 결론에 TRIGGERED 종목이 하나라도 있으면 "가격 조건 발동 종목 있음"을 명시해라.
"""

    static_system = (
        static_system
        .replace("__PORTFOLIO__", _pf_section)
        .replace("__RESTRICTED__", _restricted)
    )

    user_content = (
        f"[포트폴리오 사전 계산값 — 이 수치를 그대로 사용하라. 직접 계산 금지]\n{portfolio_calc}\n\n"
        f"[백테스팅 결과 — 보유 종목 vs VOO]\n{backtest_result}\n\n"
        f"[종목별 최신 뉴스]\n{_fmt_news(news_data)}\n\n"
        f"[종목별 실적 데이터]\n{_fmt_earnings(earnings_data)}\n\n"
        f"[전일 추천 검증 데이터 — 자기학습용]\n{yesterdays_verification}\n\n"
        f"[최근 실패/성공 패턴 요약 — 반영규칙 준수]\n{patterns_str or '패턴 데이터 없음'}\n\n"
        f"[실행 완료된 매매 내역]\n{executed_summary}\n\n"
        f"[가격 조건 발동 상태 — 반드시 본문에 반영]\n{price_trigger_str or '가격 조건 상태 데이터 없음'}\n\n"
        f"오늘 날짜: {today} (KST 기준)\n"
        f"데이터 기준: {generated_at} (한국 주식은 전일 종가 기준)\n"
        f"실시간 환율: {exchange_rate}원/달러\n\n"
        f"[거시경제 지표]\n{_fmt_macro(macro_data)}\n\n"
        f"[Fear & Greed Index]\n{_fmt_fear_greed(fear_greed)}\n\n"
        f"[Put/Call Ratio]\n{_fmt_pcr(put_call_ratio)}\n\n"
        f"[SEC 내부자 거래 (최근 7일)]\n{_fmt_insider(insider_trades)}\n\n"
        f"[정치인 거래 (최근)]\n{_fmt_congress(congress_trades)}\n\n"
        f"[미국 주식 데이터]\n{us_data}\n\n"
        f"[국내 주식 데이터]\n{kr_data}"
    )

    print("  [Claude API] 스트리밍 요청 시작...", flush=True)
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=24000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": static_system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}]
    ) as stream:
        for event in stream:
            event_type = type(event).__name__
            if event_type == "RawContentBlockStartEvent":
                block_type = getattr(getattr(event, "content_block", None), "type", "?")
                print(f"  [Claude API] 블록 시작: {block_type}", flush=True)
            elif event_type == "RawMessageDeltaEvent":
                usage = getattr(event, "usage", None)
                if usage:
                    print(f"  [Claude API] 출력 토큰: {usage.output_tokens}", flush=True)
        final = stream.get_final_message()

    print("  [Claude API] 응답 완료", flush=True)
    report = ""
    for block in final.content:
        if block.type == "text":
            report = block.text
            break

    # 목표가/손절가 파싱 후 반환
    import re
    targets = {}
    for cat in ["category1", "category2"]:
        for it in pf.get(cat, []):
            ticker = it["ticker"]
            name = it["name"]
            currency = it["currency"]

            # 해당 종목 관련 줄만 추출
            relevant_lines = []
            capture = False
            for line in report.split("\n"):
                if name in line or ticker in line:
                    capture = True
                if capture:
                    relevant_lines.append(line)
                    # 다음 종목 시작 시 중단
                    if len(relevant_lines) > 20:
                        break

            for line in relevant_lines:
                # 목표가 파싱 — 통화에 따라 분리
                if currency == "KRW":
                    # 원화: "단기 45,000원" 또는 "단기 45,000"
                    m = re.search(r"단기\s*([\d,]+)원", line)
                    if not m:
                        m = re.search(r"단기\s*([\d,]+)", line)
                    if m:
                        val = float(m.group(1).replace(",", ""))
                        if val > 1000:  # 원화는 최소 1000원 이상
                            if ticker not in targets:
                                targets[ticker] = {}
                            targets[ticker]["target_price"] = val
                else:
                    # 달러: "단기 $235" 또는 "단기 $11.5"
                    m = re.search(r"단기\s*\$?([\d.]+)", line)
                    if m:
                        val = float(m.group(1))
                        if val > 1:  # 달러는 최소 $1 이상
                            if ticker not in targets:
                                targets[ticker] = {}
                            targets[ticker]["target_price"] = val

                # 손절가 파싱 — 통화에 따라 분리
                if currency == "KRW":
                    m2 = re.search(r"손절[^:]*:\s*([\d,]+)원", line)
                    if not m2:
                        m2 = re.search(r"손절[^:]*:\s*([\d,]+)", line)
                    if m2:
                        val2 = float(m2.group(1).replace(",", ""))
                        if val2 > 1000:
                            if ticker not in targets:
                                targets[ticker] = {}
                            targets[ticker]["stop_loss"] = val2
                else:
                    m2 = re.search(r"손절[^:]*:\s*\$?([\d.]+)", line)
                    if m2:
                        val2 = float(m2.group(1))
                        if val2 > 1:
                            if ticker not in targets:
                                targets[ticker] = {}
                            targets[ticker]["stop_loss"] = val2

    return report, targets


# ── 이메일 발송 ───────────────────────────────────────────────────────────────
def _report_text_to_html(text: str) -> str:
    """보고서 텍스트를 이메일용 HTML로 변환"""
    import html as _html
    lines = text.split('\n')
    out = []
    i = 0
    EMOJI_STARTS = tuple('📊📈📉💼⭐💡🔴🟠🟡🟢💎⚠️✅❌🚫🔄⏸️📅🌍🏛️📋🔍💰')

    while i < len(lines):
        line = lines[i]

        # 표 블록: | 로 시작하는 연속 줄
        if line.startswith('|'):
            table_lines = []
            while i < len(lines) and lines[i].startswith('|'):
                table_lines.append(lines[i])
                i += 1
            out.append('<table style="border-collapse:collapse;width:100%;margin:8px 0;">')
            for j, tl in enumerate(table_lines):
                cells = [c.strip() for c in tl.strip('|').split('|')]
                # 구분선 행 (--- 포함) 스킵
                if all(set(c.replace('-','').replace(':','').replace(' ','')) == set() for c in cells):
                    continue
                tag = 'th' if j == 0 else 'td'
                style = ' style="border:1px solid #ddd;padding:6px 10px;background:#1a1a2e;color:white;"' if tag == 'th' else ' style="border:1px solid #ddd;padding:6px 10px;"'
                out.append('<tr>' + ''.join(f'<{tag}{style}>{_html.escape(c)}</{tag}>' for c in cells) + '</tr>')
            out.append('</table>')
            continue

        # 이모지로 시작하는 섹션 제목
        if line and line[0] in EMOJI_STARTS:
            out.append(f'<h3 style="color:#1a1a2e;margin:16px 0 6px;border-bottom:1px solid #eee;padding-bottom:4px;">{_html.escape(line)}</h3>')
            i += 1
            continue

        # 빈 줄
        if line.strip() == '':
            out.append('<br>')
            i += 1
            continue

        # 일반 줄
        out.append(_html.escape(line) + '<br>')
        i += 1

    return '\n'.join(out)


def send_email(report_content):
    today        = _report_date_kst_str()
    generated_at = _report_timestamp_kst_str()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 투자 분석 보고서 — {today}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    html_body = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; color: #333; }}
            h1, h2, h3 {{ color: #1a1a2e; }}
            table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }}
            th {{ background-color: #1a1a2e; color: white; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e);
                       color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
            .data-time {{ font-size: 12px; color: #ccc; margin-top: 6px; }}
            .footer {{ color: #888; font-size: 12px; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📈 포트폴리오 관리 + 신규 수익 기회 발굴 보고서</h1>
                <p>{today}</p>
                <p class="data-time">데이터 기준: {generated_at} (한국 주식은 전일 종가 기준)</p>
            </div>
            {_report_text_to_html(report_content)}
            <div class="footer">
                <p>본 보고서는 AI 분석 시스템이 자동 생성한 참고 자료입니다. 투자 결정은 본인의 판단과 책임 하에 이루어져야 합니다.</p>
            </div>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_body, "html"))

    print("  [이메일] smtp.gmail.com:465 연결 중...", flush=True)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        print("  [이메일] 로그인 중...", flush=True)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        print("  [이메일] 발송 중...", flush=True)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())

    print(f"  [이메일] 발송 완료 → {RECIPIENT_EMAIL}", flush=True)


# ── 텔레그램 발송 헬퍼 ────────────────────────────────────────────────────────

def _html_to_telegram(html: str) -> str:
    """HTML 보고서를 텔레그램 HTML 서브셋으로 변환."""

    # h2/h3 → bold 헤더
    html = re.sub(r'<h2[^>]*>(.*?)</h2>', r'\n\n<b>\1</b>\n', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<h3[^>]*>(.*?)</h3>', r'\n<b>\1</b>\n',   html, flags=re.DOTALL | re.IGNORECASE)

    # strong → b
    html = re.sub(r'<strong[^>]*>(.*?)</strong>', r'<b>\1</b>', html, flags=re.DOTALL | re.IGNORECASE)

    # table → 텍스트 그리드
    def _table_to_text(m):
        rows  = re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(0), re.DOTALL | re.IGNORECASE)
        lines = []
        for idx, row in enumerate(rows):
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL | re.IGNORECASE)
            texts = [unescape(re.sub(r'<[^>]+>', '', c)).strip().replace('\n', ' ')[:25] for c in cells]
            line  = ' | '.join(t for t in texts if t)
            if line:
                lines.append(line)
                if idx == 0:
                    lines.append('─' * min(60, len(line)))
        return '\n' + '\n'.join(lines) + '\n' if lines else ''

    html = re.sub(r'<table[^>]*>.*?</table>', _table_to_text, html, flags=re.DOTALL | re.IGNORECASE)

    # br → \n
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)

    # 블록 요소 → \n
    html = re.sub(
        r'</?(?:p|div|ul|ol|li|blockquote|section|article|header|footer|nav)[^>]*>',
        '\n', html, flags=re.IGNORECASE,
    )

    # 허용 태그(b/i/u/code/pre)에서 속성 제거
    for tag in ('b', 'i', 'u', 'code', 'pre'):
        html = re.sub(rf'<{tag}\s[^>]*>', f'<{tag}>', html, flags=re.IGNORECASE)

    # 허용 태그 외 모든 HTML 태그 제거 (내용은 유지)
    html = re.sub(r'<(?!/?(?:b|i|u|code|pre)>)[^>]+>', '', html, flags=re.IGNORECASE)

    # HTML 엔티티 디코딩
    html = unescape(html)

    # 공백 정리
    html = re.sub(r' {2,}', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)

    return html.strip()


def _split_message(text: str, max_len: int = 4096) -> list:
    """4096자 초과 시 단락 경계에서 분할."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', 0, max_len)
        if split_at == -1:
            split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip('\n')
    return [c for c in chunks if c.strip()]


def send_telegram(report_content: str):
    token   = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("  [텔레그램] TELEGRAM_BOT_TOKEN 없음, 발송 스킵", flush=True)
        return

    today  = _report_date_kst_str()
    header = f"<b>📈 투자 분석 보고서 — {today}</b>\n"
    body   = _html_to_telegram(report_content)
    chunks = _split_message(header + body)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    print(f"  [텔레그램] {len(chunks)}개 메시지 발송 시작...", flush=True)

    for i, chunk in enumerate(chunks, 1):
        payload = {"chat_id": chat_id, "text": chunk,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                print(f"  [텔레그램] {i}/{len(chunks)} 완료", flush=True)
            else:
                # HTML 파싱 오류 시 순수 텍스트로 재시도
                plain = re.sub(r'<[^>]+>', '', chunk)
                resp2 = requests.post(url, json={"chat_id": chat_id, "text": plain}, timeout=30)
                status = "완료(텍스트)" if resp2.status_code == 200 else f"실패({resp2.status_code})"
                print(f"  [텔레그램] {i}/{len(chunks)} {status}", flush=True)
        except Exception as e:
            print(f"  [텔레그램] {i}/{len(chunks)} 오류: {e}", flush=True)

        if i < len(chunks):
            time.sleep(0.5)

    print(f"  [텔레그램] 발송 완료 → chat_id={chat_id}", flush=True)


# ── 메인 실행 ─────────────────────────────────────────────────────────────────
def run_daily_report():
    print(f"\n{'='*50}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 보고서 생성 시작", flush=True)
    print(f"{'='*50}", flush=True)

    print(f"\n[0/11] 포트폴리오 로드", flush=True)
    portfolio_data = load_portfolio()

    print(f"\n[1/11] 환율 조회", flush=True)
    exchange_rate = get_exchange_rate()

    print(f"\n[2/11] 거시경제 지표 수집", flush=True)
    macro_data = get_macro_data()

    print(f"\n[3/11] Fear & Greed Index 수집", flush=True)
    fear_greed = get_fear_greed()

    print(f"\n[4/11] SEC 내부자 거래 수집", flush=True)
    insider_trades = get_insider_trades()

    print(f"\n[5/11] 정치인 거래 수집", flush=True)
    congress_trades = get_congress_trades()

    print(f"\n[6/11] Put/Call Ratio 수집", flush=True)
    vix_val = (macro_data.get("VIX") or {}).get("value")
    put_call_ratio = get_put_call_ratio(vix_value=vix_val)

    print(f"\n[7/11] 미국 주식 데이터 수집 ({len(US_TICKERS)}개 종목)", flush=True)
    us_data = get_stock_data(US_TICKERS)
    print(f"  → 수집 완료: {list(us_data.keys())}", flush=True)

    print(f"\n[DCA] 적립 자동 업데이트", flush=True)
    portfolio_data = update_dca_portfolio(portfolio_data, us_data, exchange_rate)

    # pending_actions 자동 진행률 업데이트
    try:
        actions = portfolio_data.get("pending_actions", [])
        changed = False

        # 현재 보유 주수 맵
        holdings = {}
        for cat in ["category1", "category2"]:
            for it in portfolio_data.get(cat, []):
                holdings[it["ticker"]] = it["shares"]

        for a in actions:
            if a.get("status") == "완료":
                continue
            ticker = a.get("ticker")
            total = a.get("total_units", 0)
            unit_shares = a.get("unit_shares", 0)
            start_shares = a.get("start_shares", 0)

            current_shares = holdings.get(ticker, start_shares)
            shares_added = current_shares - start_shares
            if unit_shares > 0:
                done = min(int(shares_added / unit_shares), total)
            else:
                done = a.get("done_units", 0)

            # done_units 자동 업데이트
            if done != a.get("done_units", 0):
                a["done_units"] = done
                changed = True
                print(f"  [PENDING] {a.get('name')} 진행률 자동 업데이트: {done}/{total}회", flush=True)

            # 완료 자동 처리
            if done >= total and a.get("status") != "완료":
                a["status"] = "완료"
                changed = True
                print(f"  [PENDING] {a.get('name')} 분할 계획 완료 처리", flush=True)

        if changed:
            with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
                json.dump(portfolio_data, f, ensure_ascii=False, indent=2)
            import subprocess
            repo = os.path.dirname(os.path.abspath(PORTFOLIO_FILE))
            subprocess.run(["git", "-C", repo, "add", "portfolio.json"], capture_output=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", "auto: pending_actions 진행률 자동 업데이트"], capture_output=True)
            subprocess.run(["git", "-C", repo, "push"], capture_output=True)
    except Exception as e:
        print(f"  [PENDING] 처리 실패: {e}", flush=True)

    print(f"\n[8/11] 국내 주식 데이터 수집 ({len(KR_TICKERS)}개 종목)", flush=True)
    kr_data = get_stock_data(KR_TICKERS)
    print(f"  → 수집 완료: {list(kr_data.keys())}", flush=True)

    all_tickers = US_TICKERS + KR_TICKERS

    print(f"\n[9/11] 뉴스 데이터 수집", flush=True)
    news_data = get_news_data(all_tickers, portfolio_data)

    print(f"\n[10/11] 실적 데이터 수집", flush=True)
    earnings_data = get_earnings_data(US_TICKERS)

    print(f"\n[LEARNING] 수익률/성공여부 업데이트", flush=True)
    try:
        update_learning_log_returns(portfolio_data)
    except Exception as e:
        print(f"  [LEARNING] 수익률 업데이트 실패: {e}", flush=True)

    print(f"\n[LEARNING] 실패/성공 패턴 분석", flush=True)
    try:
        p5  = analyze_failure_patterns(days=5)
        p20 = analyze_failure_patterns(days=20)
        patterns_str = _fmt_patterns(p5, p20)
        print(f"  [LEARNING] 5일 승률: {p5.get('전체승률','N/A')}% / 20일 승률: {p20.get('전체승률','N/A')}%", flush=True)
    except Exception as e:
        print(f"  [LEARNING] 패턴 분석 실패: {e}", flush=True)
        patterns_str = "패턴 분석 실패"

    # TDE Stage 2: buy_grade 계산 포함
    try:
        _p5  = p5  if isinstance(locals().get("p5"),  dict) else {}
        _p20 = p20 if isinstance(locals().get("p20"), dict) else {}
        _learning_summary = {**_p5}
        if _p20:
            _learning_summary["win_rate_20d"] = _p20.get("전체승률")
        tde_results = build_trade_decision_engine(
            us_data=us_data,
            kr_data=kr_data,
            portfolio=portfolio_data,
            learning_summary=_learning_summary,
            macro_data=macro_data,
            news_data=news_data,
            earnings_data=earnings_data if "earnings_data" in dir() else None,
            exchange_rate=exchange_rate,
        )
        # exchange_rate 실제 사용 값 확인
        _exr_val = _safe_float(exchange_rate)
        if _exr_val:
            print(f"  [TDE] exchange_rate used: {_exr_val}", flush=True)
        else:
            print(f"  [TDE][WARN] exchange_rate missing — fallback 1400.0 used. KRW position weight may be inaccurate.", flush=True)

        _tde_total   = len(tde_results)
        _tde_passed  = sum(1 for t in tde_results if t["data_status"]["data_quality"] in ("A", "B", "C"))
        _tde_missing = [t["symbol"] for t in tde_results if not t["data_status"]["price_collected"]]
        _grade_cnt   = {"A": 0, "B": 0, "C": 0, "D": 0, "PENDING": 0}
        _dca_syms, _ab_syms, _d_syms = [], [], []
        for _t in tde_results:
            _g = _t["trade_eligibility"]["buy_grade"]
            _grade_cnt[_g] = _grade_cnt.get(_g, 0) + 1
            if _t["trade_eligibility"]["buy_type"]["auto_dca_allowed"]:
                _dca_syms.append(_t["symbol"])
            if _g in ("A", "B"):
                _ab_syms.append(_t["symbol"])
            if _g == "D":
                _d_syms.append(_t["symbol"])
        print(f"  [TDE] 실시간 데이터 검증 통과: {_tde_passed}개 / {_tde_total}개", flush=True)
        print(f"  [TDE] buy_grade 분포: A={_grade_cnt['A']} / B={_grade_cnt['B']} / C={_grade_cnt['C']} / D={_grade_cnt['D']} / PENDING={_grade_cnt.get('PENDING',0)}", flush=True)
        print(f"  [TDE] 자동 적립 유지 가능: {_dca_syms}", flush=True)
        print(f"  [TDE] 신규 목돈 매수 A/B 후보: {_ab_syms}", flush=True)
        print(f"  [TDE] 신규 목돈 매수 금지(D): {_d_syms}", flush=True)
        if _tde_missing:
            print(f"  [TDE] price_missing: {_tde_missing}", flush=True)
    except Exception as _e:
        print(f"  [TDE] 실행 실패: {_e}", flush=True)
        tde_results = []

    # 가격 조건 발동 판정 (generate_report 전에 실행 — LLM user_content에 전달)
    _all_stock_pre = {**us_data, **kr_data}
    try:
        price_trigger_results = build_price_trigger_table(_all_stock_pre)
        _triggered = [r for r in price_trigger_results if r["status"] == "TRIGGERED"]
        _near      = [r for r in price_trigger_results if r["status"] == "NEAR"]
        print(f"  [PRICE-GATE] 발동 종목: {[r['name'] for r in _triggered]}", flush=True)
        print(f"  [PRICE-GATE] 근접 종목: {[r['name'] for r in _near]}", flush=True)
        # user_content에 전달할 문자열 생성
        _pt_lines = []
        for _r in price_trigger_results:
            _s = _r["status"]
            _emoji = {"TRIGGERED": "🚨 이미 발동", "NEAR": "⚠️ 근접",
                      "NOT_REACHED": "대기", "UNKNOWN": "데이터 부족"}.get(_s, _s)
            if _s == "TRIGGERED":
                _action = _r.get("triggered_action", _r["message"])
            elif _s == "NEAR":
                _action = _r.get("near_action", _r["message"])
            else:
                _action = _r["message"]
            _pt_lines.append(f"{_r['name']}({_r['symbol']}): [{_emoji}] {_action}")
        price_trigger_str = "\n".join(_pt_lines)
    except Exception as _pte:
        print(f"  [PRICE-GATE] 판정 실패: {_pte}", flush=True)
        price_trigger_results = []
        price_trigger_str = "가격 조건 판정 실패"

    print(f"\n[11/11] AI 분석 보고서 생성", flush=True)
    report, targets = generate_report(us_data, kr_data, exchange_rate,
                                      macro_data, fear_greed, insider_trades,
                                      congress_trades, put_call_ratio, portfolio_data,
                                      news_data, earnings_data, patterns_str,
                                      price_trigger_str=price_trigger_str)
    print(f"  → 보고서 생성 완료 ({len(report)}자)", flush=True)

    # 목표가/손절가 portfolio.json에 자동 저장
    try:
        changed = False
        for cat in ["category1", "category2"]:
            for it in portfolio_data.get(cat, []):
                ticker = it["ticker"]
                if ticker in targets:
                    for key, val in targets[ticker].items():
                        if it.get(key) != val:
                            it[key] = val
                            changed = True
                            print(f"  [TARGET] {it['name']}({ticker}) {key}: {val}", flush=True)
        if changed:
            with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
                json.dump(portfolio_data, f, ensure_ascii=False, indent=2)
            import subprocess
            repo = os.path.dirname(os.path.abspath(PORTFOLIO_FILE))
            subprocess.run(["git", "-C", repo, "add", "portfolio.json"], capture_output=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", f"auto: 목표가/손절가 업데이트 {datetime.now().strftime('%Y-%m-%d')}"], capture_output=True)
            subprocess.run(["git", "-C", repo, "push"], capture_output=True)
            print(f"  [TARGET] portfolio.json 저장 완료", flush=True)
    except Exception as e:
        print(f"  [TARGET] 저장 실패: {e}", flush=True)

    # 자동 검증 및 재시도
    all_stock_data = {**us_data, **kr_data}
    validation_errors = validate_report(report, portfolio_data, all_stock_data, exchange_rate)

    if validation_errors:
        print(f"\n⚠️  검증 오류 {len(validation_errors)}개 발견:", flush=True)
        for err in validation_errors:
            print(f"  - {err}", flush=True)

        print(f"  검증 오류 있음 — 원본 보고서 발송", flush=True)
    else:
        print(f"\n✅ 검증 통과 — 오류 없음", flush=True)

    # learning_log 저장
    try:
        all_stock_data = {**us_data, **kr_data}
        # VIX 값 및 시장위험도 계산
        _vix_val = _safe_float((macro_data.get("VIX") or {}).get("value"))
        if _vix_val is None:
            _market_risk = "불명"
        elif _vix_val < 15:
            _market_risk = "낮음"
        elif _vix_val <= 20:
            _market_risk = "보통"
        else:
            _market_risk = "높음"

        log_entry = {
            "추천종목": [],
            "즉시매도": [],
            "시장판단": "",
            "판단정확도_맞춤": 0,
            "판단정확도_전체": 0,
        }
        judgments = _parse_report_judgments(report, all_stock_data)
        ticker_name_map = {}
        for cat in ["category1", "category2"]:
            for it in portfolio_data.get(cat, []):
                ticker_name_map[it["ticker"]] = it.get("name", it["ticker"])

        existing_tickers = {rec["ticker"] for rec in log_entry["추천종목"]}
        for ticker, data in all_stock_data.items():
            if ticker in existing_tickers:
                continue
            price = _safe_float(data.get("현재가"))
            if not price:
                continue
            j = judgments.get(ticker, {})
            raw_action = j.get("판단", "")
            norm_action = _normalize_action(raw_action or j.get("방향", ""))
            stock_type = _classify_stock_type(ticker, portfolio_data, price)
            log_entry["추천종목"].append({
                "ticker": ticker,
                "name": ticker_name_map.get(ticker, ticker),
                "추천가": price,
                "방향": j.get("방향", norm_action),
                "판단": raw_action,
                "추천행동": norm_action,
                "원문추천행동": raw_action,
                "종목유형": stock_type,
                "당시가격": price,
                "당시RSI": _safe_float(data.get("RSI14")),
                "당시MA상태": "정배열" if data.get("정배열") else "역배열",
                "당시거래량비": _safe_float(data.get("거래량비율(5일/20일)")),
                "당시VIX": _vix_val,
                "당시시장위험도": _market_risk,
                "당시섹터": "",
                "1일후수익률": None,
                "5일후수익률": None,
                "20일후수익률": None,
                "성공여부": None,
                "실패태그": [],
                "반영규칙": "",
            })
        save_learning_log(log_entry)
    except Exception as e:
        print(f"  learning_log 저장 실패: {e}", flush=True)

    # TDE 섹션을 보고서 하단에 추가 (코드가 직접 생성 — LLM 본문 훼손 없음)
    try:
        if isinstance(report, str):
            _tde_list   = tde_results if tde_results else []
            _ptr        = price_trigger_results if price_trigger_results else []
            tde_section = render_tde_report_section(_tde_list)
            # LLM/TDE 충돌 감지 (가격 조건 발동 충돌 포함)
            _conflicts = detect_tde_llm_conflicts(report, _tde_list, price_trigger_results=_ptr)
            if _conflicts:
                _conf_lines = [
                    "", "⚠️ TDE/LLM 충돌 검증", "",
                    "| 충돌 항목 | 내용 |", "| --- | --- |",
                ]
                for _c in _conflicts:
                    _conf_lines.append(f"| {_c['type']} | {_c['detail']} |")
                tde_section = tde_section + "\n" + "\n".join(_conf_lines)
                print(f"  [TDE][CONFLICT] 충돌 {len(_conflicts)}건 감지", flush=True)
            else:
                tde_section = (
                    tde_section
                    + "\n\n⚠️ TDE/LLM 충돌 검증\n\n"
                    + "충돌 없음 — TDE와 LLM 본문 판단이 일치합니다."
                )

            # 가격 조건 발동 검증표 추가
            _pt_section = render_price_trigger_section(_ptr)
            if _pt_section:
                report = report + "\n\n" + tde_section + "\n\n" + _pt_section
                print(f"  [PRICE-GATE] 보고서 하단에 가격 조건 발동 검증표 추가 완료", flush=True)
            else:
                report = report + "\n\n" + tde_section
            print(f"  [TDE] 보고서 하단에 TDE 섹션 추가 완료 ({len(tde_section)}자)", flush=True)
        else:
            print(f"  [TDE][WARN] report가 문자열이 아님 — TDE 섹션 추가 생략 (type={type(report)})", flush=True)
    except Exception as _te:
        print(f"  [TDE][WARN] TDE 섹션 추가 실패: {_te}", flush=True)

    print(f"\n이메일 발송", flush=True)
    _force = os.environ.get("FORCE_SEND_REPORT", "").lower() == "true"
    if _force:
        print("  [SEND-GUARD] FORCE_SEND_REPORT=true — duplicate checks bypassed", flush=True)
    if not _force and already_sent_today("daily"):
        print("  [SEND-GUARD] already sent today — skip email", flush=True)
    elif not _force and gmail_report_exists_today("daily"):
        print("  [SEND-GUARD] Gmail already has today's report — skip email", flush=True)
    else:
        send_email(report)
        mark_sent_today("daily")

    print(f"\n{'='*50}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 전체 완료", flush=True)
    print(f"{'='*50}\n", flush=True)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        run_daily_report()
    else:
        run_daily_report()
        schedule.every().day.at("07:00").do(run_daily_report)
        print("스케줄러 시작 — 매일 07:00 자동 실행")
        while True:
            schedule.run_pending()
            time.sleep(60)
