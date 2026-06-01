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
PORTFOLIO_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
FEAR_GREED_CACHE = "/tmp/fear_greed_cache.json"

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
        "VIX":  (10,   80),
        "DXY":  (85,  115),
        "WTI":  (50,   120),
        "Gold": (1800, 5500),
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
    if any(ticker == it.get("ticker") for it in portfolio_data.get("category1", [])):
        return "핵심장기보유"
    if ticker in HIGH_RISK:
        return "고위험옵션"
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
                    reason.append("RR 미계산 (valid=false) — A/B 불가")
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
        })

    return results


# ── TDE 보고서 섹션 렌더러 ─────────────────────────────────────────────────────
def render_tde_report_section(tde_results):
    """
    TDE 결과를 보고서 본문에 붙일 deterministic section으로 변환한다.
    LLM이 아닌 코드가 직접 생성한다.
    """
    if not tde_results:
        return (
            "## 🧠 TDE 실전 매매 판단 엔진\n\n"
            "TDE 결과 없음 — 데이터 구조 확인 필요"
        )

    lines = ["## 🧠 TDE 실전 매매 판단 엔진", ""]

    # ── 1. TDE 요약 ───────────────────────────────────────────────────────────
    total   = len(tde_results)
    passed  = sum(1 for t in tde_results if t["data_status"]["data_quality"] in ("A", "B", "C"))
    ab_list = [t["name"] for t in tde_results if t["trade_eligibility"]["buy_grade"] in ("A", "B")]
    dca_list= [t["name"] for t in tde_results if t["trade_eligibility"]["buy_type"]["auto_dca_allowed"]]
    d_list  = [t["name"] for t in tde_results if t["trade_eligibility"]["buy_grade"] == "D"]
    d_str   = ", ".join(d_list[:6]) + ("..." if len(d_list) > 6 else "")

    lines += [
        "| 항목 | 결과 |",
        "| --- | --- |",
        f"| 실시간 데이터 검증 통과 | {passed}개 / {total}개 |",
        f"| 신규 목돈 매수 A/B 후보 | {len(ab_list)}개 |",
        f"| 자동 적립 유지 가능 | {', '.join(dca_list) if dca_list else '없음'} |",
        f"| 신규 목돈 매수 금지(D) | {d_str if d_list else '없음'} |",
        "",
    ]

    # ── 2. 신규 매수 실행표 ───────────────────────────────────────────────────
    lines += ["신규 매수 실행표", ""]

    valid_ab   = [t for t in tde_results
                  if t["trade_eligibility"]["buy_grade"] in ("A", "B")
                  and t["risk_reward"]["valid"]]
    invalid_ab = [t for t in tde_results
                  if t["trade_eligibility"]["buy_grade"] in ("A", "B")
                  and not t["risk_reward"]["valid"]]

    if valid_ab:
        lines += [
            "| 종목 | 등급 | 현재가 | 진입 가능 | 손절가 | 목표가 | 예상손실 | 기대수익 | R/R | 이유 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for t in valid_ab:
            te  = t["trade_eligibility"]
            rr  = t["risk_reward"]
            sym = t["symbol"]
            is_kr = sym.endswith(".KS") or sym.endswith(".KQ")
            cur = "₩" if is_kr else "$"
            def _fmt(v):
                if v is None:
                    return "-"
                return f"{cur}{v:,.0f}" if is_kr else f"{cur}{v:.2f}"
            gain_str = f"{rr['expected_gain_pct']:.1f}%" if rr["expected_gain_pct"] is not None else "-"
            loss_str = f"{rr['expected_loss_pct']:.1f}%" if rr["expected_loss_pct"] is not None else "-"
            rr_str   = f"{rr['reward_risk_ratio']:.2f}"  if rr["reward_risk_ratio"]  is not None else "-"
            reason0  = te["reason"][0][:45] if te["reason"] else "-"
            lines.append(
                f"| {t['name']}({sym}) | {te['buy_grade']} | {_fmt(rr['entry_price'])} "
                f"| O | {_fmt(rr['stop_price'])} | {_fmt(rr['target_price'])} "
                f"| {loss_str} | {gain_str} | {rr_str} | {reason0} |"
            )
        lines.append("")
    else:
        lines += ["신규 매수 실행표 없음 — TDE 기준 A/B 조건 통과 종목 0개", ""]

    if invalid_ab:
        lines += [
            "⚠️ TDE 경고: A/B 등급인데 risk_reward.valid=false인 종목이 있습니다. 4단계 실패 조건 검증 필요.",
            "",
        ]

    # ── 3. 자동 적립 유지표 ──────────────────────────────────────────────────
    lines += ["자동 적립 유지표", ""]
    dca_items = [t for t in tde_results if t["trade_eligibility"]["buy_type"]["auto_dca_allowed"]]
    if dca_items:
        lines += [
            "| 종목 | 자동 적립 | 신규 목돈 매수 | 이유 |",
            "| --- | --- | --- | --- |",
        ]
        for t in dca_items:
            te   = t["trade_eligibility"]
            bt   = te["buy_type"]
            lump = "가능" if bt["lump_sum_buy_allowed"] else "금지"
            r0   = te["reason"][0][:50] if te["reason"] else "-"
            lines.append(f"| {t['name']}({t['symbol']}) | 유지 가능 | {lump} | {r0} |")
        lines.append("")
    else:
        lines += ["자동 적립 유지 가능 종목 없음", ""]

    # ── 4. 오늘 금지 행동 ────────────────────────────────────────────────────
    lines += ["오늘 금지 행동", ""]
    d_items = [t for t in tde_results if t["trade_eligibility"]["buy_grade"] == "D"]
    if d_items:
        lines += [
            "| 종목 | 금지 행동 | 이유 |",
            "| --- | --- | --- |",
        ]
        for t in d_items:
            cat = t["category"]
            ds  = t["data_status"]
            if not ds["price_collected"]:
                action = "데이터 미수집 — 매매 금지"
            elif cat == "유형③":
                action = "신규/추매 금지"
            elif cat == "유형④":
                action = "물타기 금지"
            elif cat == "유형①":
                action = "목돈 추격매수 금지"
            else:
                action = "신규 매수 금지"
            r0 = t["trade_eligibility"]["reason"][0][:50] if t["trade_eligibility"]["reason"] else "-"
            lines.append(f"| {t['name']}({t['symbol']}) | {action} | {r0} |")
        lines.append("")

    # ── 5. 오늘 실제 주문 가능 여부 ─────────────────────────────────────────
    if valid_ab:
        lines.append(
            "오늘 실제 신규 매수 후보 있음 — "
            "단, 3단계에서는 주문 수량 자동 계산 전이므로 4단계 전까지 수동 검토 필요"
        )
    else:
        lines.append("오늘 실제 신규 매수 주문 없음")
    lines.append("")

    return "\n".join(lines)


# ── 보고서 생성 ───────────────────────────────────────────────────────────────
def generate_report(us_data, kr_data, exchange_rate,
                    macro_data, fear_greed, insider_trades,
                    congress_trades, put_call_ratio, portfolio_data=None,
                    news_data=None, earnings_data=None, patterns_str=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%Y년 %m월 %d일")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

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

[출력 형식 원칙 — 반드시 준수]
이 보고서는 이메일로 발송된다. 마크다운 문법을 사용하지 마라.
## 헤더, --- 구분선, ``` 코드블록, ** 볼드 같은 마크다운 문법은 절대 사용 금지.
이모지와 일반 텍스트, 표(|로 구성)만 사용해라.
보고서 길이는 이메일에서 잘리지 않도록 핵심 내용 위주로 작성해라.
섹션 구분은 이모지로 해라. 예: 📊 📈 💼 ⭐ 💡

주의: 아래 [종목 분류 체계]에서 말하는 "고위험 옵션성 베팅"은 보고서 내 종목 위험 분류일 뿐이다.
기존 코드의 category3 또는 500만원→1억 프로젝트 섹션을 부활시키면 안 된다.
category3 자금 계산, category3_cash, category3_seed, cat3_total 출력은 전부 제거된 상태다.

[보고서 출력 순서 — 반드시 이 순서를 지켜라]
1. 오늘의 핵심 결론 (10줄)
2. 데이터 신뢰도 체크
3. 시장/거시 환경
4. 포트폴리오 전체 리스크 진단
5. 보유 종목별 상세 분석
6. 손실 종목 회복 전략
7. 신규 매수 후보 발굴표
7.5. 신규 매수 실행표 (B등급 이상 종목이 있을 때만)
8. 오늘의 액션 플랜 (3단계)
8.5. 실제 매매 실행표
9. 하지 말아야 할 행동
10. 모델 성과 검증
11. 다음 보고서에서 확인할 것
11.5. 실시간 매매 검증표
12. 가격 조건 검증표
13. 종목별 점수 상세표
14. 보고서 등급 및 자기검열 결과
15. 100점 보고서 조건 점검
15.5. 실전 매매 사용 가능 등급
16. 최종 매매 결론
16.5. 회수 자금 재배치 계획
16.7. 수익형 신규 매수 모드 전환 조건

[1. 오늘의 핵심 결론 — 보고서 맨 위에 반드시 10줄로 요약]
아래 형식을 정확히 따라라:
오늘 시장 위험도: 낮음/중간/높음
신규 매수 가능 여부: 가능/제한적/금지
  (전일 정확도 30% 미만이면 반드시 "원칙적 금지" 또는 "관망" 또는 "기존 적립 종목만 유지" 중 하나로 표시해라. "제한적" 금지)
가장 중요한 리스크: (1줄)
오늘 반드시 피해야 할 행동: (1줄)
오늘 가장 좋은 행동: (1줄)
현금 비중 권장: X%
고위험 종목 비중 상태: X% (기준 10~15% 이하)
손실 종목 대응: (1줄)
오늘의 1순위 관찰 종목: (종목명)
오늘의 최종 판단: 공격/중립/방어

[2. 데이터 신뢰도 — 모든 데이터에 반드시 표시]
각 데이터 옆에 아래 중 하나를 붙여라:
✅ 검증 완료: 2개 이상 소스 일치
🟡 단일 소스: 1개 소스만 확인
⚠️ 불일치: 소스 간 수치 차이
❌ 미수집: 데이터 수집 실패

중요: 데이터 미수집은 절대 매도 근거가 아니다.
데이터 미수집 = "분석 보류, 수동 확인 필요"로만 처리해라.
"데이터 없음 = 시장 무관심 = 즉시매도" 표현은 절대 금지한다.

원자재/거시 데이터 신뢰도 추가 규칙:
WTI, 금, 구리, DXY, VIX, 10년물 금리 등 거시/원자재 지표가 단일 소스일 경우
반드시 "단일 소스 — 시점 차이 가능"으로 표시해라.
출력 예시:
  금 가격: 🟡 단일 소스 — 시점 차이 가능, 직접 확인 필요
  WTI: 🟡 단일 소스 — 90달러대 상단 여부 재확인 필요
단일 소스 원자재 데이터를 강한 매수/매도 근거로 사용하지 마라.

[3. 전일 정확도 반영 — 반드시 적용]
전일 판단 정확도에 따라 오늘 매수 권고를 자동 조정해라:
- 70% 이상: 기존 기준 유지
- 50~70%: 신규 진입 비중 30% 축소
- 30~50%: 신규 진입 비중 50% 축소, 강력매수 금지
- 30% 미만: 신규 매수 가능 여부를 반드시 "원칙적 금지" 또는 "관망"으로 표시해라
  - 신규 고위험 종목 진입 금지, 현금 비중 확대
  - VOO/GOOGL/FCX 기존 적립은 "기존 적립 유지"와 "신규 자금 투입 보류"를 구분해서 표현
  - 신규 종목 매수와 기존 적립 유지를 혼동하지 마라
  - 금지 표현: "제한적", "신규 진입 50% 이상 축소", "일부 신규 진입 가능", "보수적 신규 매수 가능"
  - 출력 예시: 신규 매수 가능 여부: 원칙적 금지 (전일 정확도 X% — 기존 적립 종목 유지 외 신규 진입 금지)
  - 또는: 신규 매수 가능 여부: 관망 (전일 정확도 X% — 신규 종목 진입 금지, 보유 리스크 관리 우선)
- 10% 미만: 모든 신규 추천을 "관망"으로 강등, 보고서 상단에 ⚠️ 모델 신뢰도 경고 표시

[4. 종목 분류 체계 — 4단계로 반드시 분류]
보고서 출력 시 종목 분류 표기는 아래 형식을 사용해라 (코드 내부 키 category1/2는 변경 없음):

유형① — 핵심 장기 보유/적립
대상: VOO, GOOGL, FCX
조건: 장기 적립 또는 장기 보유가 기본. 하락 시 손절보다 적립/비중 조절 중심으로 판단. 단기 RSI만으로 매도 금지.

유형② — 성장 스윙/고성장 관리
대상: NVDA, PLTR, 네이버, 카카오, 한미반도체
조건: 성장성은 있으나 가격/밸류/테마/모멘텀 영향이 크므로 진입·축소 조건을 명확히 설정. 분할매수/매도 기준 필수.

유형③ — 고위험 옵션성 베팅
대상: RGTI, BEAM, JOBY, PWFL
조건: 적자, 테마성, 변동성, 자금조달 리스크가 크므로 신규/추매를 엄격히 제한.
   - 개별 종목 최대 비중: 전체 자산의 3~5%
   - 합산 최대 비중: 10~15% 이하 (10% 초과 시 경고/신규 추가 금지, 15% 초과 시 강한 축소 필요)
   - 전일 정확도 30% 미만이면 신규 매수 금지

유형④ — 회복/탈출 관리
대상: 셀트리온제약
조건: 신규 매수 관점이 아니라 손실 복구와 집중 리스크 축소 관점으로 판단. 단순 매도보다 회복 전략 우선 제시.

중요:
- 보고서 출력에서 "유형①/②" 같은 복합 표기는 절대 금지. 하나의 유형으로만 표시해라.
- NVDA는 유형②로 표시해라. 유형①로 표시 금지.
- PLTR은 유형②로 표시해라. 유형①/②처럼 복합 표기 금지.
- 내부 코드의 category 키는 변경하지 말고 보고서 출력 지침만 따른다.
보고서에서 "카테고리1", "카테고리2", "카테고리3", "카테고리4" 표현 대신 반드시 "유형①", "유형②", "유형③", "유형④" 표현을 사용해라.

[5. 손실 종목 처리 원칙 — -10% 이상 손실 종목 필수 적용]
손실 중인 종목은 반드시 아래 3가지 선택지를 표로 제시해라:

| 옵션 | 행동 | 장점 | 단점 | 예상 손실/회복 | 기회비용 |
|------|------|------|------|----------------|----------|
| 즉시 손절 | 전량 매도 | | | | |
| 반등 시 축소 | 반등 목표가 도달 시 절반 매도 | | | | |
| 지지선 보유 | 핵심 지지선 이탈 전까지 보유 | | | | |

반드시 계산해라:
- 현재 손실률 / 본전까지 필요한 상승률
- 반등 매도 전략 목표가
- 손절 시 회수 자금의 대체 투자 기대수익
- 계속 보유 시 기회비용

즉시 손절 옵션 출력 시 반드시 아래 4개를 분리해서 표시해라:
- 현금화 금액 = 현재가 × 보유수량
- 총자산 대비 현금화 비중 = 현금화 금액 / 총자산
- 확정 손실 금액 = (평단 - 현재가) × 보유수량
- 확정 손실률 = (평단 - 현재가) / 평단

출력 예시 (셀트리온제약):
즉시 손절: 398주 전량 매도 시 약 X만원 현금화. 총자산 대비 약 28.2% 유동화. 단, 약 -769만원 손실 확정.

금지 표현:
- "자본 X% 즉시 회수"처럼 근거 불명확한 비중 표현
- 손실률과 현금화 비중을 혼동하는 표현
- 현금화 비중을 손실률처럼 표현하기

절대 단순히 "손실 중이니 즉시 전량매도"라고 하지 마라.

[6. 최우선 판단 원칙]
모든 매수/매도 판단은 아래 4개를 동시에 평가한 후 내려라:
① 기술적 지표 ② 펀더멘털 ③ 수급/뉴스/이벤트 ④ 포트폴리오 내 비중과 손실 상태

즉시 전량 시장가 매도는 아래를 모두 충족할 때만 허용:
- 펀더멘털 악화 확인
- 기술적 핵심 지지선 이탈
- 유동성 충분
- 대체 투자처 기대값이 더 높음

[7. 포트폴리오 리스크 진단 — 반드시 계산]
아래 항목을 수치로 표시해라:
- 전체 자산 / 현금 비중 / 원화:달러 비중 / 한국:미국 비중
- 흑자기업:적자기업 비중 / 고위험 종목 비중
- 단일 종목 최대 비중 / 상위 3개 종목 집중도

아래 조건 해당 시 자동 경고:
⚠️ 단일 종목 20% 초과 → 집중 리스크
⚠️ 적자기업 합산 15% 초과 → 고위험 성장주 리스크
⚠️ 고위험 옵션성 종목 합산 10% 초과 → 투기 비중 경고 구간
⚠️ 현금 5% 미만 → 방어력 부족

고위험 옵션성 종목(유형③) 합산 비중 판단 기준:
- 0~5%: 낮음
- 5~10%: 적정 범위
- 10~15%: 경고 구간 — 신규 추가 금지, 회복 시 축소 검토. 이 구간을 "상단 돌파"나 "과다 초과"처럼 표현하지 마라.
- 15% 초과: 과다 — 강한 축소 필요
표현 원칙: 합산 11~12% 수준은 "10% 내부 경고선 초과, 15% 권장 상한 이내"로 표현해라. "상단 돌파" 표현 금지.

[8. 스코어 시스템 — 6개 항목 100점 만점]
각 종목 점수는 아래 6개 항목으로 계산하고 감점 사유를 표시해라:
- 데이터 신뢰도 15점
- 기술적 추세 15점
- 펀더멘털 20점 (적자기업 자동 감점)
- 밸류에이션 15점
- 수급/이벤트 15점
- 리스크 대비 기대수익 20점

판단 기준:
85점 이상: 강한 관심 (가격 조건 충족 시 진입)
75~84점: 분할매수 후보
65~74점: 보유 가능, 신규 매수 신중
50~64점: 관망 또는 비중 축소
50점 미만: 신규 매수 금지, 보유자는 탈출 전략 필요

[9. 액션 플랜 — 반드시 3단계로 나눠라]
🔴 반드시 해야 할 것 (리스크 관리 우선)
🟡 조건부 실행 (특정 가격/거래량/뉴스 조건 충족 시)
🚫 하지 말아야 할 것 (추격매수, 물타기, 데이터 미확인 시장가 매도 등)

하루 액션 총 개수: 5개 이하로 제한해라.
5개 초과 시 "오늘 필수 2개 + 이번 주 3개"로 분리해라.

[10. 모델 성과 검증 — 보고서 하단에 반드시 포함]
learning_log 데이터 기반으로 아래를 계산해라:
- 최근 5일 추천 종목 평균 수익률
- 강력매수 종목 5일 후 승률
- 평균 수익 / 평균 손실 / 손익비
- VOO 대비 초과수익률
- 한국/미국/고위험 종목별 추천 정확도

데이터 부족 시: "데이터 누적 중 (X일치) — 20일 이상 누적 후 통계 의미 있음"으로 표시.
데이터 없으면: 이 보고서를 "시장 모니터링 보고서"로 표시.

[11. 뉴스/이벤트 표현 원칙]
아래 표현은 공식 발표/SEC 공시/IR로 확인된 경우에만 사용:
"임박", "확정", "기관 진입", "상용화 시작", "멀티배거"

확인되지 않은 경우 반드시:
"가능성", "기대감", "시장 추정", "확인 필요"로 표현해라.

[뉴스 중요도 등급 — 모든 종목 뉴스에 반드시 적용]

각 종목 뉴스는 아래 등급으로 분류해라.

A등급: 정부정책, 대형 계약, 실적 발표, FDA/임상, 규제, 유상증자/희석, 상장폐지/회계 이슈
B등급: 애널리스트 목표가, 기관 수급, 제품 출시, 파트너십, 산업 모멘텀
C등급: 일반 기사, 단순 전망, 블로그성 분석, 소셜미디어 이슈

출력 규칙:
- A등급 뉴스는 반드시 종목별 상세 분석과 실시간 매매 검증표에 반영
- A등급 뉴스가 누락되면 보고서 구조 품질 최대 85점
- A등급 악재가 있는데 신규 매수를 추천하면 보고서 실패
- A등급 호재가 있어도 적자·고위험 종목이면 추매가 아니라 "보유 근거/축소 보류 근거"로 우선 해석
- 단일 출처 A등급 뉴스는 "추가 확인 필요" 표시

RGTI 예시:
뉴스: 미국 정부 양자컴퓨팅 투자 프로그램 / Rigetti 최대 1억 달러 LOI — 등급: A
해석: 정책 모멘텀 긍정. 단, 희석/적자/고변동성으로 추매 금지, 보유 및 손실 제한 병행.

BEAM 예시:
뉴스: BEAM-302 가속승인/임상 관련 업데이트 — 등급: A
해석: 바이오 모멘텀 긍정. 단, 임상 실패·희석 리스크로 추매 금지, 35달러 돌파 시 조건부 일부 축소.

GOOGL/PLTR 내부자 거래 예시:
뉴스: Form 4 내부자 거래 — 등급: B (10b5-1/옵션행사/세금목적 확인 전)
해석: 단독 매도 근거로 사용 금지. 여러 임원 동시 매도 + 고평가 + 약세 차트면 A급으로 격상.

[12. 내부자 거래 해석]
Form 4는 단순 매수/매도로 해석하지 마라.
반드시 구분해라: 10b5-1 계획 매도 / 옵션 행사 후 매도 / 세금 목적 매도
여러 임원 동시 매도 + 실적 발표 전후 시점일 때만 강한 경고로 처리해라.

[카테고리1 적립 종목 판단 구분]
유형① 종목(VOO/GOOGL/FCX) 판단 시 반드시 아래 3가지를 구분해라:
- 기존 자동 적립 유지: 원래 정해둔 소액 DCA는 계속한다.
- 추가 일시매수 보류: 평소 적립 외 목돈/추가 자금 투입은 조건 충족 전까지 보류한다.
- 적립 재개 조건: 중단했던 적립이나 추가 적립을 다시 시작할 가격/RSI 조건이다.

출력 원칙:
VOO: 기존 자동 적립은 유지. 단, 추가 일시매수/신규 목돈 투입은 RSI 60 이하 또는 670달러 이하 전까지 보류.
GOOGL: 기존 자동 적립은 유지. 추가 일시매수는 RSI 55 이하 또는 370달러 이하에서만 검토.
FCX: 기존 자동 적립은 유지. 추가 일시매수는 RSI 45 이하 또는 60달러 이하에서만 검토.

금지 표현:
- "적립 지속, 신규 자금 투입 없음" (적립 자체가 자금 투입이므로 표현 충돌)
- "적립 지속, 매수 금지" (기존 적립은 계속하므로 오해 유발)
- "적립 재개"와 "기존 적립 유지"를 같은 의미처럼 쓰기
유형① 종목은 단기 매도 금지. 추가 일시매수는 확실한 근거 있을 때만.

[포트폴리오 계산 원칙]
user_content의 [포트폴리오 사전 계산값]을 기본값으로 사용하되,
종목별 평가금액 합계와 총합계가 1% 이상 차이 나면
"⚠️ 포트폴리오 계산값 불일치 — 수동 확인 필요"로 표시해라.
단, LLM이 임의로 값을 재계산해 최종값을 대체하지는 마라.
"현재가 미수집 → 0원" 종목은 계산에서 제외하고 "(시가 미수집)" 명시.

[분할 계획 관리]
user_content의 [진행 중인 분할 계획]을 반드시 참고해라.
진행 중인 종목에 새로운 분할 계획을 중복으로 만들지 마라.
남은 분할 횟수만 안내해라.

[실행 완료 매매 반영]
user_content의 [실행 완료된 매매 내역]을 반드시 반영해라.
매수 완료 종목은 추가 매수 권고 중복 금지.
매도 완료 종목은 추가 매도 권고 중복 금지.

[자기학습 원칙]
user_content의 [전일 추천 검증 데이터]를 반드시 활용해라.
✅정확 많으면: 판단 기준 유지
❌틀림 많으면: 해당 종목/섹터 판단 보수적 조정
누적 정확도 60% 미만: 오늘 전체 판단 한 단계 보수적 조정
누적 정확도 80% 이상: 현재 판단 기준 유효 명시

[최근 실패/성공 패턴 활용 원칙]
user_content의 [최근 실패/성공 패턴 요약]을 반드시 확인하고 반영규칙을 준수해라.
- 전체 승률 50% 미만이면: 오늘 전체 판단 한 단계 보수적으로 조정 (매수→홀딩, 홀딩→관망)
- RSI_false_signal 2회 이상이면: RSI 70+ 종목 매수 추천 금지, RSI 30- 종목 매도 추천 자제
- weak_volume 2회 이상이면: 거래량비율 0.8 미만 종목 신규 매수 금지
- macro_ignored 2회 이상이면: VIX 20+ 환경에서 매수 추천 전면 제한
- high_risk_overweight 2회 이상이면: RGTI/BEAM/JOBY/QBTS/PWFL 신규 매수 금지
- korea_momentum_fail 2회 이상이면: 한국 주식 추가 매수 조건 강화 (RSI 40 이하 + 거래량 1.5배 이상)
- risk_mode_ignored 2회 이상이면: 오늘 매수 추천 전체 보수적 조정
패턴 데이터 없음이면: 기존 전략 유지

[WTI/Gold 수집 실패]
"⚠️ WTI 범위 이탈(X달러)" 또는 "⚠️ WTI API 수집 오류"로 원인 구분해라.

[판단 일관성 자가검증 — 출력 전 반드시 확인]
① TOP3 판단 등급과 스코어카드 등급이 일치하는가?
② 카테고리1 종목에 신규 자금 투입 여부가 명확히 구분됐는가?
③ 고위험 종목 비중이 전체 자산의 15% 이하인가?
④ 액션플랜이 5개 이하인가?
⑤ 손실 -10% 이상 종목에 3가지 옵션이 제시됐는가?
⑥ 전일 정확도에 따른 매수 제한이 적용됐는가?
불일치 발견 시 해당 섹션을 수정 후 출력해라.

[뉴스/실적/백테스팅 활용]
[종목별 최신 뉴스]: 긍정 뉴스 → 판단 상향 검토, 부정 뉴스 → 하향 검토
[종목별 실적 데이터]: 이번 주 실적 발표 종목은 핵심 이벤트에 포함
[백테스팅 결과]: VOO 대비 초과 성과 종목 긍정 반영, 3개월 이상 미달 종목 비중축소 검토

[개인 전략 우선 반영]

사용자의 실제 보유 포지션과 이전 전략 대화를 반드시 반영해라.

셀트리온제약(068760.KS) 전략 원칙:
- 사용자는 셀트리온제약을 평단 68,287원에 398주 보유 중이다.
- 사용자는 단순 손절보다 본전 회복 후 수익 전환 가능성까지 보고 싶어 한다.
- 따라서 셀트리온제약은 "즉시 전량매도"나 "55,000원 절반 자동매도" 중심이 아니라, 회복형 전략을 기본값으로 제시해라.
- 55,000원은 자동 매도 가격이 아니라 "1차 알림/흐름 확인 가격"으로만 표시한다.
- 실제 부분매도 판단은 58,000~62,000원 구간에서 재검토한다.
- 68,287원 본전 회복 후 바로 매도하지 말고 70,000원 안착 여부를 확인한다.
- 70,000원 안착 시 72,000~78,000원 수익 구간을 목표로 제시한다.
- 46,500원 종가 이탈 시 회복 전략 훼손으로 표시하고, 일부 축소/리스크 재검토를 제안한다.
- "55,000원 도달 시 절반 자동매도" 같은 단정적 표현은 절대 금지한다.
- 셀트리온제약은 반드시 아래 표 형식으로 출력한다.

| 가격 구간 | 의미 | 행동 |
|---|---|---|
| 46,500원 종가 이탈 | 회복 전략 훼손 | 일부 축소/리스크 재검토 |
| 55,000원 | 1차 반등 확인 | 매도 아님, 알림만 |
| 58,000~62,000원 | 핵심 재판단 구간 | 방어형은 일부 축소, 회복형은 보유 가능 |
| 68,287원 | 사용자 본전 | 바로 매도하지 말고 70,000원 안착 확인 |
| 70,000원 안착 | 수익 전환 가능성 | 72,000~78,000원 목표 |
| 78,000원 부근 | 강한 저항/52주 고점권 | 대부분 정리 검토 |

한미반도체(042700.KS) 전략 원칙:
- 보유 비중이 전체 자산의 1% 미만이면 계좌 전체 리스크는 작다고 표시한다.
- 보유 비중 1% 미만에서는 320,000원 회복 시 자동매도가 아니라 "흐름 확인"으로 처리한다.
- 280,000원 종가 이탈 전까지는 보유 가능으로 표시한다.
- 360,000원 이상에서 1차 비중 점검을 제안한다.
- 400,000원 이상은 1차 목표가로 표시한다.
- 426,000원은 전고점권으로 표시하고 일부 익절 검토 구간으로 제시한다.
- 480,000원 이상은 과열권으로 표시하고 수익 실현 우선으로 제시한다.
- 단, 한미반도체 비중이 전체 자산의 5% 이상이면 분할매도/손절 기준을 강화한다.

| 가격 | 행동 |
|---|---|
| 280,000원 종가 이탈 | 성장 스윙 시나리오 훼손, 손절/축소 검토 |
| 320,000원 | 본전 근처, 자동매도 아님. 흐름 확인 |
| 360,000원 | 1차 비중 점검 |
| 400,000원 | 1차 목표가 |
| 426,000원 | 전고점권, 일부 익절 검토 |
| 480,000원 이상 | 과열권, 수익 실현 우선 |

[가격 조건 오류 방지 — 필수]

모든 종목의 가격 조건은 출력 전 반드시 검증해라.

공통 규칙:
- 손절가/축소가는 현재가보다 반드시 낮아야 한다.
- 목표가는 현재가보다 반드시 높아야 한다.
- 현재가보다 높은 손절선은 오류다.
- 현재가보다 낮은 목표가는 오류다.
- 이미 이탈한 가격을 손절선으로 쓰지 마라.
- 이미 발동된 조건이면 "이미 조건 발동, 즉시 재검토 필요"로 표시해라.
- 알림가는 매수/매도와 구분해서 표시해라.
- 특히 셀트리온제약 55,000원은 매도가 아니라 알림가다.

RGTI/BEAM/JOBY 같은 미국 고위험 종목 기준:
- 손절선이 현재가보다 높으면 자동으로 "조건 오류"로 표시하고 다시 계산해라.
- RGTI 기본 기준: 추매 금지 / 22달러 종가 이탈 시 절반 축소 / 19~20달러 이탈 시 전량 재검토 / 28~30달러 회복 시 일부 수익 점검
- MA20이 22달러보다 높거나 낮을 경우 더 합리적인 지지선을 선택하되, 현재가보다 높은 손절선은 절대 금지한다.

출력 전 반드시 아래 가격 조건 검증표를 추가해라(12번 섹션):

| 종목 | 현재가 | 손절/축소 기준 | 적립/추가매수 기준 | 목표가 | 조건 검증 |
|---|---|---|---|---|---|
(모든 보유 종목을 실제 수치로 채워서 출력. 오류 시 "조건 오류" 표시 후 수정)

카테고리1 적립 ETF/종목(VOO, GOOGL, FCX) 가격 조건 표기 원칙:
- 손절/축소 기준 칸: "없음(장기보유)"으로 표시
- 적립/추가매수 기준 칸: RSI 조건 또는 가격 기준 기재
- VOO의 670달러는 손절가가 아니라 적립재개 기준이다. 손절/축소 기준으로 넣지 마라.
예시: | VOO | 689.96달러 | 없음(장기보유) | RSI 60이하/670달러(적립재개) | 720달러 | ✅ 정상 |
조건 검증은 손절가<현재가, 목표가>현재가뿐 아니라 "기준 성격이 올바른 칸에 들어갔는지"도 검증해라.

[점수 산정 근거 상세 출력 — 필수]

각 종목 점수는 총점만 출력하지 말고 반드시 6개 항목별 점수를 함께 출력해라.

점수 구조:
- 데이터 신뢰도 15점
- 기술적 추세 15점
- 펀더멘털 20점
- 밸류에이션 15점
- 수급/이벤트 15점
- 리스크 대비 기대수익 20점
= 총 100점

13번 섹션으로 아래 표를 반드시 출력해라:

| 종목 | 원점수 | 패턴조정 | 최종점수 | 데이터(15) | 기술(15) | 펀더(20) | 밸류(15) | 수급/이벤트(15) | 기대수익/리스크(20) | 핵심 감점 사유 | 패턴 조정 사유 |
|---|---|---|---|---|---|---|---|---|---|---|---|

패턴 조정 규칙:
- korea_momentum_fail 최근 5일 3회 이상이면 한국 종목 -7점
- RSI_false_signal 최근 5일 2회 이상이면 RSI 기반 매수 종목 -5점
- high_risk_overweight 최근 5일 2회 이상이면 고위험 종목 -5점
- macro_ignored 최근 5일 2회 이상이면 거시환경 민감 종목 -5점
  단, macro_ignored는 아래 경우에만 적용해라:
    보고서가 금리/달러/VIX/원자재 급등락 같은 거시 리스크를 무시하고 강한 매수를 냈을 때
    거시 악재가 명확한데 신규 매수를 추천했을 때
- 여러 패턴이 동시에 해당되면 합산하되, 패턴조정 총 감점은 최대 -15점으로 제한
- 패턴 조정이 없으면 0으로 표시
- 패턴 조정 사유가 없으면 "-"로 표시
- 원점수 + 패턴조정 = 최종점수가 반드시 일치해야 한다
- 최종점수와 총점 표현을 혼용하지 마라. 앞으로 종목 점수표에서는 "최종점수"를 사용해라

VOO/장기 ETF 패턴조정 사유 표현 원칙:
VOO는 장기 적립 ETF이므로 macro_ignored 실패 태그를 직접 적용하지 마라.
VOO의 RSI 과열이나 시장 전반 과열 상황에서는 아래 태그를 우선 사용해라:
- market_overheat: 시장 전반 과열 구간
- RSI_overheat: RSI 70 이상 단기 과열
- broad_market_overheat: 광범위 시장 과열
- valuation_overheat: 밸류에이션 과열
VOO가 RSI 70 이상이고 F&G greed 구간이면: 패턴 조정 사유 → market_overheat / RSI_overheat
금지 출력: VOO | ... | macro_ignored 보정

예시:
| 네이버 | 72 | -7 | 65 | 13/15 | 9/15 | 13/20 | 10/15 | 11/15 | 9/20 | 한국시장 약세 | korea_momentum_fail 누적 |
| 카카오 | 63 | -8 | 55 | 13/15 | 7/15 | 11/20 | 9/15 | 8/15 | 7/20 | 파업 리스크 | korea_momentum_fail + 파업 |
| RGTI | 54 | 0 | 54 | 13/15 | 11/15 | 4/20 | 2/15 | 13/15 | 11/20 | 적자, PSR 과도 | - |

규칙:
- 항목별 합계와 최종점수가 반드시 일치해야 한다. 맞지 않으면 출력 전 수정해라.
- 적자기업은 펀더멘털과 밸류에이션에서 자동 감점한다.
- 매출 성장, 현금 보유, 기술 진척, 정부계약 등으로 일부 보완 가능하다.
- 최종점수만 있고 항목별 점수가 없으면 자기검열 실패로 처리한다.

[자기검열 체크리스트 강화 — 기존 체크리스트보다 우선 적용]

자기검열은 절대 무조건 10/10으로 통과시키지 마라.
아래 항목이 실제 보고서에 명확히 반영되지 않았으면 "아니오"로 처리해라.

아래 4개는 특히 엄격히 검사한다:
① 점수 산정 근거가 6개 항목별로 표시됐는가?
   총점만 있으면 실패. 종목별 점수 상세표(13번 섹션)가 없으면 실패.
② 손절가가 현재가보다 낮고 목표가가 현재가보다 높은가?
   현재가보다 높은 손절선이면 실패. 이미 발동된 조건을 조건부 실행으로 쓰면 실패.
③ 사용자 개인 전략이 반영됐는가?
   셀트리온제약 55,000원을 자동매도 가격으로 쓰면 실패.
   셀트리온제약 회복형 전략이 빠지면 실패.
   한미반도체 비중 1% 미만인데 본전 근처 자동매도를 권하면 실패.
④ 적자기업의 최악 시나리오가 구체적 수치로 적혔는가?
   "위험함" 정도의 표현만 있으면 실패. -30%, -50%, -70% 등 수치가 있어야 통과.

자기검열에서 실패 항목이 있으면 "통과"라고 쓰지 말고, 해당 섹션을 수정한 뒤 다시 검열해라.
수정 불가능하면 실패 사유를 그대로 출력해라.
기존 자기검열 체크리스트와 충돌하면 이 강화 기준을 우선한다.

전일 판단 정확도에 따라 자기검열 최대 통과점수를 제한한다:
- 70% 이상: 최대 10/10
- 50~69%: 최대 8/10
- 30~49%: 최대 7/10
- 30% 미만: 최대 6/10

자기검열 항목 중 "해당없음"이 있으면 N/A로 표시하고 유효한 항목 수 중 통과 수로 계산해라.
N/A 항목을 통과로 계산하지 마라. "해당없음/예" 표현은 사용하지 마라.
전일 정확도 30% 미만인데 자기검열 10/10이 출력되면 실패로 간주한다.

자기검열은 아래 3단계로 표시해라:
1단계 — 원검열 결과: 전체 항목 수 / 통과 수 / 실패 수 / N/A 수
2단계 — N/A 제외 후 유효 검열: 유효 항목 수(=전체-N/A) 중 통과 수
3단계 — 전일 정확도 패널티 적용 후 최종 자기검열:
  최종 자기검열 = 원검열 결과와 정확도 패널티 중 낮은 쪽 적용

출력 예시 (N/A 없는 경우):
⚠️ 자기검열:
  원검열: 8/10 통과
  N/A 제외 후: 8/10
  전일 정확도 18%로 최대 6/10 제한 적용
  최종 자기검열: 6/10

출력 예시 (N/A 있는 경우):
⚠️ 자기검열:
  원검열: 8/10 통과, N/A 1개
  N/A 제외 후: 8/9
  전일 정확도 18%로 최대 6/10 제한 적용
  최종 자기검열: 6/9

출력 예시 (패널티 없는 경우):
✅ 자기검열:
  원검열: 9/10 통과
  N/A 제외 후: 9/10
  전일 정확도 72% — 패널티 없음
  최종 자기검열: 9/10

N/A 항목을 통과로 계산하지 마라. "해당없음/예" 표현은 절대 사용하지 마라.
전일 정확도 30% 미만인데 자기검열 최종 10/10이 출력되면 실패로 간주한다.

[보고서 등급 체계 — 3점수 분리 출력]

보고서 마지막(14번 섹션)에 아래 3개 점수를 반드시 분리해서 출력해라.

1. 보고서 구조 품질 점수 (0~100점)
평가 요소 — 아래 10개 구조 요소로 평가한다. 전일 정확도 패널티는 이 점수에 직접 적용하지 마라.
  (1) 데이터 신뢰도 체크 여부
  (2) 가격 조건 검증표 정상 여부
  (3) 원점수/패턴조정/최종점수 일치 여부
  (4) 실제 매매 실행표 포함 여부
  (5) 100점 보고서 조건 점검 포함 여부
  (6) 최종 매매 결론 포함 (실시간 검증 근거 칸 포함)
  (7) 사용자 개인 전략 반영 여부
  (8) 금지 행동이 명확히 구분됐는지 여부
  (9) 실시간 매매 검증표 포함 여부 (A등급 뉴스 반영 확인)
  (10) 회수 자금 재배치 계획 포함 여부
보고서 구조 품질을 낮추는 요인: 가격 조건 오류 / 점수 계산 오류 / 실행표 누락 / 개인전략 위반 / A등급 뉴스 누락
전일 정확도 18%여도 구조 요소가 정상이라면 보고서 구조 품질은 90점 이상 가능하다.

구조 품질 제한 규칙 (전일 정확도와 무관):
- 보고서 실패 조건이 1개 이상이면 보고서 구조 품질 최대 70점
- A등급 뉴스가 누락되면 보고서 구조 품질 최대 85점
- 가격 조건 오류가 있으면 보고서 구조 품질 최대 70점
- 점수 산정 근거 항목별 없으면 보고서 구조 품질 최대 75점
- 사용자 개인 전략 위반이 있으면 보고서 구조 품질 최대 75점
- 자기검열 최종이 5/10 이하이면 보고서 구조 품질 최대 75점 (6/10은 해당 안 됨)

출력 예시:
보고서 구조 품질: 96점
감점 사유: 단일 소스 거시지표 일부 존재
보고서 실패 조건: 없음

금지: 전일 정확도 때문에 보고서 구조 품질을 낮추지 마라.
금지 출력: 보고서 구조 품질: 80점 (전일 정확도 18% 패널티로 최대 80점 제한)

2. 모델 예측 신뢰도 점수 (0~100점)
평가 요소: 전일 정확도 / 최근 5일 승률 / VOO 대비 초과성과 / 실패 패턴 반복 여부
전일 정확도 패널티는 이 점수에 반영해라.

3. 실제 매매 활용도 등급
평가 요소: 신규 매수 활용 가능 여부 / 보유 종목 관리 가능 여부 / 손실 제한 판단 가능 여부 / 매도/축소 실행 가능 여부
전일 정확도 30% 미만이면 신규 매수 활용도는 자동으로 "부적합" 처리해라.

출력 예시:
보고서 구조 품질: 94점
모델 예측 신뢰도: 35점 (전일 18%, 최근 5일 29.4%, VOO 초과 27%)
실제 매매 활용도: 신규 매수 부적합 / 보유 리스크 관리용 적합 / 손실 제한 판단 적합

최종 등급:
- 신규 매수 보고서: 부적합
- 보유 종목 리스크 관리 보고서: 사용 가능
- 손실 제한/축소 판단 보고서: 사용 가능
- 자동매매 직접 연결: 금지

추가 규칙:
- 보고서 구조 품질이 높아도 모델 예측 신뢰도가 낮으면 "실전 신규 매수 가능"으로 표시하지 마라
- 보고서 구조 품질 90점 이상이라도 모델 예측 신뢰도 70점 미만이면 신규 매수는 금지 표시

판단 예시:
  전일 정확도 18% → 모델 예측 신뢰도 낮음, 신규 매수 활용도 부적합. 보고서 구조 품질에는 직접 영향 없음.
  자기검열 최종 6/9 → 5/10 이하 아니므로 보고서 구조 품질 75점 제한 자동 적용 안 함.

금지 출력 예시:
  보고서 구조 품질: 80점 (전일 정확도 18% 패널티로 최대 80점 제한) → 오류: 구조 품질에 정확도 패널티 적용 금지
  보고서 구조 91점인데 전일 정확도 18%이므로 신규 매수 가능 → 오류: 신뢰도 낮으면 신규 매수 금지

[출력 구조 보완]

기존 보고서 출력 순서(1~11번)는 유지하되, 아래 섹션을 반드시 추가해라:
12. 가격 조건 검증표
13. 종목별 점수 상세표
14. 보고서 등급 및 자기검열 결과 (3점수 분리 포함)
15. 100점 보고서 조건 점검
16. 최종 매매 결론

[최종 액션 플랜 보정]

액션 플랜은 사용자가 실제로 실행할 행동만 적어라.

절대 금지:
- 이미 발동된 조건을 조건부 실행으로 쓰기
- 알림 설정과 매도 주문을 혼동하기
- 고위험 종목 신규 진입 금지 상태에서 신규 진입 추천하기
- 사용자가 원하지 않는 손실 확정 전략을 최우선으로 제시하기
- 셀트리온제약 55,000원 자동 절반 매도
- RGTI 25달러 이탈 조건 사용
- 데이터 미수집 종목 시장가 매도

권장 출력 구조:
🔴 반드시 해야 할 것:
1. 셀트리온제약 55,000원 알림 설정. 매도 주문 아님. 58,000~62,000원 도달 시 방어형/회복형 중 재판단.
2. RGTI 22달러 종가 이탈 알림 설정. 25달러 이탈 조건은 사용하지 않음.

🟡 조건부 실행:
3. 셀트리온제약 46,500원 종가 이탈 시 회복 전략 훼손으로 일부 축소 검토.
4. 한미반도체 280,000원 이탈 시 손절/축소 검토, 360,000원 회복 시 1차 비중 점검.
5. VOO RSI 60 이하 또는 670달러 이하 도달 시 적립 재개 검토.

🚫 하지 말아야 할 것:
- 셀트리온제약 55,000원에서 자동 절반 매도
- RGTI 25달러 이탈 조건 사용
- 정확도 30% 미만 상태에서 RGTI/JOBY 신규 진입
- 데이터 미수집 종목 시장가 매도
- NVDA/반도체 테마 추격매수

[자기검열 체크리스트 — 보고서 출력 전 반드시 실행]
아래 10개를 순서대로 확인하고 결과를 보고서 맨 끝에 표시해라.
하나라도 "아니오"이면 해당 섹션을 수정한 뒤 출력해라.

① 데이터 미수집을 매수/매도 근거로 사용하지 않았는가?
② 전일 정확도가 낮을 때 신규 고위험 매수를 제한했는가?
③ 큰 손실 종목에 대해 즉시 전량매도 외 대안을 제시했는가?
④ 모든 강력매수 종목에 손절 기준과 최대 비중을 명시했는가?
⑤ 적자기업 추천 시 최악의 손실 시나리오를 적었는가?
⑥ 점수 산정 근거가 항목별로 표시되었는가?
⑦ 포트폴리오 전체 비중과 리스크를 먼저 평가했는가?
⑧ 하루 액션 플랜이 5개 이하인가?
⑨ 사용자가 실제 실행 가능한 수준으로 정리했는가?
⑩ 이 보고서를 그대로 따라도 치명적인 손실 가능성을 줄일 수 있는가?

보고서 맨 끝에 자기검열은 아래 4단계로 표시해라:
1단계 — 원검열 결과: 전체 항목 수 / 통과 수 / 실패 수 / N/A 수
2단계 — N/A 제외 후 유효 검열: 유효 항목 수(=전체-N/A) 중 통과 수
3단계 — 정확도 패널티 환산 한도:
  전일 정확도 30% 미만이면 최대 6/10 수준 제한을 "환산 한도"로 표시해라.
  이 제한은 분모가 10인 환산값 기준이다.
4단계 — 최종 자기검열:
  실제 분모는 N/A 제외 후 유효 항목 수를 사용해라.
  예: N/A 제외 후 9개 항목이면 최종 자기검열은 6/9로 출력.
  동시에 10점 환산도 괄호로 표시해라.

출력 예시:
자기검열:
1단계 원검열: 10항목 중 9 통과, 1 N/A
2단계 N/A 제외: 9/9
3단계 정확도 패널티: 전일 18% < 30% → 최대 6/10 수준 제한 (환산 한도)
4단계 최종 자기검열: 6/9 통과 (10점 환산 약 6.7/10)

정확도 패널티 환산 규칙:
- 70% 이상: 환산 한도 없음
- 50~69%: 최대 8/10 수준
- 30~49%: 최대 7/10 수준
- 30% 미만: 최대 6/10 수준
N/A 항목은 통과로 계산하지 마라. "해당없음/예" 표현 금지.

금지 출력: 자기검열 6/10 과 최종 자기검열 6/9를 설명 없이 동시에 표기하지 마라.
두 표현이 동시에 등장하면 반드시 "6/10 수준 환산 한도 적용, 실제 유효항목 기준 6/9"처럼 명시해라.

[개인 전략 우선 반영 — 사용자 보유 포지션별 맞춤 전략]

사용자의 실제 보유 포지션과 이전 전략 대화 내용을 반드시 반영해라.

특히 셀트리온제약은 사용자가 평단 68,287원에 398주 보유 중이며, 단순 손절보다 본전 회복 후 수익 전환 가능성까지 보고 싶어 한다.

따라서 셀트리온제약 전략은 아래 원칙을 우선 적용해라.
- 55,000원은 자동 매도 가격이 아니라 "1차 알림/흐름 확인 가격"으로만 표시한다.
- 실제 부분매도 판단은 58,000~62,000원 구간에서 재검토한다.
- 사용자가 회복형 전략을 선택한 경우, 68,287원 본전 회복 후 70,000원 안착 여부를 확인한다.
- 70,000원 안착 시 72,000~78,000원 수익 구간을 목표로 제시한다.
- 단, 46,500원 종가 이탈 또는 45,000원 이탈 시에는 회복 전략 훼손으로 표시하고, 일부 축소/리스크 재검토를 제안한다.
- 셀트리온제약에 대해 "55,000원 도달 시 절반 자동매도" 같은 단정적 표현은 금지한다.
- 표현은 반드시 "55,000원 알림", "58,000~62,000원 재판단", "68,287원 본전", "70,000원 안착 확인" 구조로 작성한다.

셀트리온제약 출력 예시:
| 가격 구간 | 의미 | 행동 |
|---|---|---|
| 46,500원 종가 이탈 | 회복 전략 훼손 | 일부 축소/리스크 재검토 |
| 55,000원 | 1차 반등 확인 | 매도 아님, 알림만 |
| 58,000~62,000원 | 핵심 재판단 구간 | 방어형은 일부 축소, 회복형은 보유 가능 |
| 68,287원 | 사용자 본전 | 바로 매도하지 말고 70,000원 안착 확인 |
| 70,000원 안착 | 수익 전환 가능성 | 72,000~78,000원 목표 |
| 78,000원 부근 | 강한 저항/52주 고점권 | 대부분 정리 검토 |

셀트리온제약 집중 리스크 축소표:
셀트리온제약 비중이 전체 자산의 20%를 초과하면 반드시 아래 표를 출력해라.

| 시나리오 | 조건 | 매도 수량 | 예상 잔여 수량 | 목적 |
|---|---|---|---|---|
| 방어형 | 58,000~62,000원 반등 | 100~150주 | 248~298주 | 집중 리스크 완화 |
| 중립형 | 68,287원 회복 후 70,000원 안착 실패 | 100~150주 | 248~298주 | 본전 부근 리스크 축소 |
| 회복형 | 70,000원 안착 후 72,000~78,000원 | 150~250주 | 148~248주 | 수익 전환 후 단계적 회수 |
| 실패형 | 46,500원 종가 이탈 | 100~150주 우선 축소 | 248~298주 | 회복 전략 훼손 대응 |

단, 55,000원은 자동매도 기준이 아니라 알림가로만 표시해라.
46,500원 이탈 시 전량매도보다 100~150주 우선 축소를 기본값으로 제시해라.
집중 리스크 28%는 명확히 위험하다고 표시해라.
비중이 20% 이하이면 시나리오 제시를 강제하지 않으나, 집중 리스크 주의 표시는 해라.

[RGTI 조건 오류 방지]

RGTI, BEAM, JOBY 같은 미국 고위험 종목은 매도 조건을 현재가보다 높게 설정하지 마라.
현재가가 24.62달러인데 "25달러 이탈 시 절반 매도"라고 쓰면 이미 조건이 발동된 것이므로 오류다.

조건 설정 규칙:
- 손절/축소 가격은 현재가보다 반드시 낮아야 한다.
- 이미 이탈한 가격을 손절선으로 쓰지 마라.
- 손절선이 현재가보다 높거나 같으면 자동으로 "조건 오류"로 표시하고 다시 계산해라.
- RGTI 기본 기준:
  - 신규/추매 금지 (항상 명시)
  - 22달러 종가 이탈 시 절반 축소 검토
  - 19~20달러 이탈 시 전량 재검토
  - 28~30달러 회복 시 "욕심 금지, 고위험 비중 상단이면 일부 축소 우선"
- 실제 MA20이 22달러보다 높거나 낮을 경우 MA20과 22달러 중 더 합리적인 핵심 지지선을 사용하되, 현재가보다 높은 손절선은 절대 금지한다.

RGTI(리게티컴퓨팅) 표현 원칙 — 고위험 정책 모멘텀 + 손실 제한 병행:
- RGTI는 "고위험 정책 모멘텀 보유 + 손실 제한 감시 종목"으로 표시해라. 단순 "손실 제한 감시"로만 쓰지 마라.
- 미국 정부의 약 20억 달러 규모 양자컴퓨팅 투자/지원 프로그램 관련 뉴스를 반드시 확인해라.
- Rigetti의 최대 1억 달러 LOI 또는 정부 지원 관련 뉴스를 반드시 확인해라.
- 이 뉴스는 A등급 정책 모멘텀으로 긍정적이지만, 적자기업·희석 가능성·고변동성 리스크가 있으므로 신규/추매 금지는 유지한다.
- 정책 모멘텀은 보유 근거가 될 수 있지만, 추매 근거로 단독 사용하지 마라.
- RGTI 관련 정부지원 뉴스가 확인됐는데 보고서에 반영되지 않으면 "뉴스 반영 누락"으로 표시해라.
- "22달러 종가 이탈 시 절반 축소 검토" 앞에 반드시 "신규/추매 금지"를 명시해라.
- 28~30달러 회복 시 표현: "욕심 금지, 고위험 비중 상단이면 일부 축소 우선"
- 최종점수 54점 이하이면 액션 플랜에서 매수 관련 표현 절대 금지
- RGTI는 고위험 옵션성 종목이므로 신규 매수 후보처럼 표현하지 마라
- 28~30달러 회복 구간도 신규 매수 신호로 해석하지 마라
- RGTI를 "강한 매수" 또는 "추매 가능"으로 표현하지 마라.

RGTI 출력 예시:
RGTI — 고위험 정책 모멘텀 보유 + 손실 제한 감시 종목
미국 정부의 양자컴퓨팅 투자 프로그램과 Rigetti의 최대 1억 달러 LOI는 단기 정책 모멘텀으로 긍정적이다.
다만 적자기업, 고변동성, 희석 가능성이 있으므로 신규/추매 금지는 유지한다.
22달러 종가 이탈 시 절반 축소, 28~30달러 회복 시 일부 축소 또는 원금 회수 검토.

| 가격 구간 | 의미 | 행동 |
|---|---|---|
| 22달러 종가 이탈 | 정책 모멘텀에도 불구하고 단기 회복 시나리오 훼손 | 절반 축소 검토 |
| 19~20달러 이탈 | 고위험 손실 확대 / 정책 모멘텀 실패 | 전량 재검토 |
| 24~25달러 | 현재 감시 구간 | 신규/추매 금지, 보유만 |
| 28~30달러 회복 | 정책 모멘텀 재점화 구간 | 거래량 동반 여부 확인, 고위험 비중 10% 초과 시 일부 축소 우선 |
| 30달러 이상 안착 | 과열/수익 실현 구간 | 원금 회수 또는 잔여 비중 축소 검토 |

액션 플랜 출력 예시:
RGTI: 매수 후보 아님. 고위험 정책 모멘텀 보유 + 손실 제한 감시 종목으로 관리. 신규/추매 금지, 22달러 종가 이탈 시 절반 축소 검토.

[BEAM 조건부 관리 원칙]
BEAM은 유형③ 고위험 옵션성 종목이다.
신규/추매 금지는 항상 유지한다.
35달러 돌파 시 무조건 매도가 아니라 아래 조건을 함께 확인해라:
  1. 고위험 합산 비중이 10% 초과인지
  2. 거래량이 둔화되는지
  3. BEAM-302 관련 호재가 이미 가격에 반영됐는지
  4. 임상/가속승인 뉴스 후속이 있는지
위 조건 중 2개 이상이 부정적이면 일부 축소를 우선한다.
호재 지속 + 거래량 유지 + 고위험 합산 10% 이하라면 보유 가능으로 표시한다.

BEAM 출력 예시:
BEAM 35달러 돌파 시 무조건 매도 아님.
고위험 합산 비중이 10% 초과이고, 거래량 둔화 또는 호재 소멸 신호가 나오면 일부 축소 우선.
호재 지속과 거래량 유지가 확인되면 보유 가능.

표현 금지: "BEAM 35달러 돌파 시 바로 매도", "무조건 익절" 같은 단정적 표현 금지.

출력 전 반드시 아래를 검증해라:
- 손절가 < 현재가
- 목표가 > 현재가
- 알림가는 현재가와의 거리 표시
- 이미 발동된 조건이면 "이미 조건 발동, 즉시 재검토 필요"로 표시

[한미반도체 전략 보정]

한미반도체는 현재 사용자 보유 비중이 매우 작을 수 있으므로, 단순히 본전 근처에서 매도하라는 식으로 출력하지 마라.
보유 비중이 전체 자산의 1% 미만이면:
- 계좌 전체 리스크는 작다고 표시한다.
- 320,000원 회복 시 자동 매도보다 "흐름 확인"으로 처리한다.
- 280,000원 이탈 전까지 보유 가능으로 표시한다.
- 360,000원 이상에서 1차 매도/비중 점검을 제안한다.
- 400,000원 이상은 강한 목표가로 표시한다.

단, 비중이 5% 이상으로 커지면 기존 분할매도/손절 기준을 강화해라.

한미반도체 출력 예시:
| 가격 | 행동 |
|---|---|
| 280,000원 종가 이탈 | 성장 스윙 시나리오 훼손, 손절/축소 검토 |
| 320,000원 | 본전 근처, 자동매도 아님. 흐름 확인 |
| 360,000원 | 1차 비중 점검 |
| 400,000원 | 1차 목표가 |
| 426,000원 | 전고점권, 일부 익절 검토 |
| 480,000원 이상 | 과열권, 수익 실현 우선 |

[점수 산정 근거 상세 출력 — 필수]

각 종목 점수는 총점만 출력하지 말고 반드시 6개 항목별 점수를 함께 출력해라.

점수 구조:
- 데이터 신뢰도 15점
- 기술적 추세 15점
- 펀더멘털 20점
- 밸류에이션 15점
- 수급/이벤트 15점
- 리스크 대비 기대수익 20점
= 총 100점

모든 종목 표에는 아래 형식을 반드시 포함해라:
| 종목 | 원점수 | 패턴조정 | 최종점수 | 데이터(15) | 기술(15) | 펀더(20) | 밸류(15) | 수급/이벤트(15) | 기대수익/리스크(20) | 핵심 감점 사유 | 패턴 조정 사유 |
|---|---|---|---|---|---|---|---|---|---|---|---|

패턴 조정 규칙:
- korea_momentum_fail 최근 5일 3회 이상이면 한국 종목 -7점
- RSI_false_signal 최근 5일 2회 이상이면 RSI 기반 매수 종목 -5점
- high_risk_overweight 최근 5일 2회 이상이면 고위험 종목 -5점
- macro_ignored 최근 5일 2회 이상이면 거시환경 민감 종목 -5점
- 패턴조정 총 감점 최대 -15점. 조정 없으면 0, 사유 없으면 "-"
- 원점수 + 패턴조정 = 최종점수. "총점" 표현 사용 금지, "최종점수"만 사용

예시:
| RGTI | 54 | 0 | 54 | 13/15 | 11/15 | 4/20 | 2/15 | 13/15 | 11/20 | 적자, PSR 과도 | - |
| 네이버 | 72 | -7 | 65 | 13/15 | 9/15 | 13/20 | 10/15 | 11/15 | 9/20 | 한국시장 약세 | korea_momentum_fail 누적 |
| VOO | 78 | 0 | 78 | 15/15 | 11/15 | 15/20 | 9/15 | 12/15 | 16/20 | RSI 70 단기 과열 | - |

중요: 항목별 합계와 최종점수가 반드시 일치해야 한다. 맞지 않으면 출력 전 수정해라.

[자기검열 체크리스트 강화]

자기검열은 절대 무조건 10/10으로 통과시키지 마라.
아래 항목이 실제 보고서에 명확히 반영되지 않았으면 "아니오"로 처리해라.

특히 아래 4개는 엄격히 검사한다:
① 점수 산정 근거가 6개 항목별로 표시됐는가?
   총점만 있으면 실패.
② 손절가가 현재가보다 낮고 목표가가 현재가보다 높은가?
   RGTI처럼 현재가보다 높은 손절선이면 실패.
③ 사용자 개인 전략이 반영됐는가?
   셀트리온제약 55,000원 자동매도처럼 사용자 회복 전략과 충돌하면 실패.
④ 적자기업의 최악 시나리오가 구체적인 수치로 적혔는가?
   "위험함" 정도의 표현만 있으면 실패. 예: -30%, -50%, -70% 등 예상 손실 폭을 적어야 통과.

자기검열에서 실패 항목이 있으면 "통과"라고 쓰지 말고, 해당 섹션을 수정한 뒤 다시 검열해라.
수정 불가능하면 실패 사유를 그대로 출력해라.

전일 판단 정확도에 따라 자기검열 최대 통과점수를 제한한다:
- 70% 이상: 최대 10/10
- 50~69%: 최대 8/10
- 30~49%: 최대 7/10
- 30% 미만: 최대 6/10

자기검열 항목 중 "해당없음"이 있으면 N/A로 표시하고 유효한 항목 수 중 통과 수로 계산해라.
N/A 항목을 통과로 계산하지 마라. "해당없음/예" 표현은 사용하지 마라.
전일 정확도 30% 미만인데 자기검열 10/10이 출력되면 실패로 간주한다.

자기검열은 아래 3단계로 표시해라:
1단계 — 원검열 결과: 전체 항목 수 / 통과 수 / 실패 수 / N/A 수
2단계 — N/A 제외 후 유효 검열: 유효 항목 수(=전체-N/A) 중 통과 수
3단계 — 전일 정확도 패널티 적용 후 최종 자기검열:
  최종 자기검열 = 원검열 결과와 정확도 패널티 중 낮은 쪽 적용

출력 예시 (N/A 없는 경우):
⚠️ 자기검열:
  원검열: 8/10 통과
  N/A 제외 후: 8/10
  전일 정확도 18%로 최대 6/10 제한 적용
  최종 자기검열: 6/10

N/A 항목은 통과로 계산하지 마라. "해당없음/예" 표현 절대 금지.
전일 정확도 30% 미만인데 최종 10/10 출력되면 실패로 간주한다.

[가격 조건 일관성 검사 — 필수]

모든 종목에 대해 아래 검사를 수행해라:
- 매수가가 현재가보다 높으면 반드시 "돌파매수"라고 명시해야 한다.
- 손절가/축소가는 현재가보다 낮아야 한다. 현재가보다 높은 손절가는 오류다.
- 목표가는 현재가보다 높아야 한다. 현재가보다 낮은 목표가는 "축소 기준"으로 표시해야 한다.
- 알림가는 매수/매도와 구분해라. 특히 셀트리온제약 55,000원은 매도가 아니라 알림가로 표시한다.
- 손절/축소 기준과 적립/추가매수 기준이 같은 칸에 섞이면 오류다.
- 장기 적립 종목(VOO/GOOGL/FCX)의 적립재개 기준을 손절가로 쓰면 오류다.
- 현재가가 이미 손절/축소 기준 아래이면 "이미 발동" 표시.
- 현재가가 목표가를 이미 초과했으면 "목표가 재설정 필요" 표시.
- 신규 매수 금지 상태인데 적립/추가매수 기준에 "매수" 표현이 있으면 "신규 금지와 충돌" 표시.

출력 전 반드시 아래 표를 추가해라:

12. 가격 조건 검증표
| 종목 | 현재가 | 손절/축소 기준 | 적립/추가매수 기준 | 목표가 | 조건 검증 |
|---|---|---|---|---|---|
(모든 보유 종목을 실제 수치로 채워서 출력해라. 오류 시 "조건 오류" 표시 후 수정)

유형①(VOO/GOOGL/FCX)은 손절/축소 기준 칸에 "없음(장기보유)" 표기. 적립/추가매수 기준 칸에 RSI/가격 기준 기재.
VOO 670달러는 손절가 아님 — 적립/추가매수 기준 칸에만 기재.
예시: | VOO | 689.96달러 | 없음(장기보유) | RSI 60이하/670달러(적립재개) | 720달러 | ✅ 정상 |
예시: | RGTI | 24.62달러 | 22달러 종가 이탈 | 추매 금지 | 28~30달러(축소) | ✅ 정상 |
예시(오류): | RGTI | 24.62달러 | 25달러 이탈 | - | 30달러 | ❌ 이미 발동된 손절 기준 |

[신규 매수 차단 조건 — 종목 유형별로 적용]

신규 매수는 아래 4단계 등급으로 분류해라:
A: 실전 신규 매수 검토 가능
B: 소액 분할매수 후보
C: 관찰 후보 (지금은 금지, 조건 충족 시 B 가능)
D: 신규 매수 금지

유형① 장기 적립/우량 ETF (VOO, GOOGL, FCX):
- 모델 5일 승률이 낮아도 기존 자동 적립은 유지 가능
- 추가 일시매수는 가격 조건 충족 시 B등급까지 가능
- VOO RSI 60 이하, GOOGL 370달러 이하, FCX 60달러 이하 충족 시 B등급 가능
- A등급은 RSI 과열 해소 + 거래량 안정 + 거시 위험 완화가 함께 필요

유형② 성장 스윙 (NVDA, PLTR, 네이버, 카카오, 한미반도체):
- 최근 5일 승률 40% 미만이면 A등급 금지
- 실적/뉴스/차트/거래량/리스크 보상비가 모두 통과하면 B등급 가능
- korea_momentum_fail 5회 이상이면 한국 종목 A등급 금지

유형③ 고위험 옵션성 (RGTI, BEAM, JOBY, PWFL):
- 최근 5일 승률 50% 미만이면 A/B 모두 금지
- 고위험 합산 10% 초과면 A/B 모두 금지
- 정부정책/임상/계약 뉴스는 보유 근거로만 사용, 추매 근거 단독 사용 금지
- 기존 보유 종목의 수익 실현/축소 판단은 별도 평가 가능

유형④ 회복/탈출 (셀트리온제약):
- 신규 매수 평가 대상이 아니다
- 물타기 금지
- 회복/축소/탈출 전략만 평가

전체 공통 차단 조건 — 아래 중 하나라도 해당하면 신규 매수 원칙적 금지:
- 전일 정확도 30% 미만 (유형① B등급까지는 예외 허용)
- general_miss 최근 5일 10회 이상
- 고위험 옵션성 비중 10% 초과

신규 매수 차단 사유 출력 형식:
신규 매수 차단 사유:
- 전일 정확도 X% < 30%
- 최근 5일 승률 X% < 40%
- 한국 모멘텀 실패 X회
- 고위험 옵션성 비중 X% > 10%
판단: 신규 매수 원칙적 금지. 어떤 조건이면 B/A 가능한지 반드시 표시해라.

[신규 매수 A등급 조건 — A등급은 아래 조건을 모두 충족해야 한다]

A: 실전 신규 매수 검토 가능 (강한 매수가 아님. 검토 가능 수준)

A등급 필수 조건 1 — 실시간 가격 검증:
- 현재가가 보고서 수집가와 ±1% 이내
- 장중 급등 후 고점 추격 구간이 아님
- 손절 기준이 현재가보다 낮고, 목표가가 현재가보다 높음
- 현재가가 이미 손절선 아래이거나 목표가에 근접해 있으면 A등급 불가

A등급 필수 조건 2 — 차트 조건 (종목 유형별 RSI 기준 적용):
유형① ETF/우량 장기 적립:
  RSI 35~65 허용. VOO/지수 ETF는 RSI 65 초과 시 추가 일시매수 A등급 불가.
  단, 기존 자동 적립은 RSI 무관 유지 가능.
유형① 원자재/경기민감 우량주:
  RSI 35~68 허용. FCX 같은 원자재주는 추세 상승 중 RSI 62~68도 정상 구간일 수 있다.
  RSI 70 이상은 과열로 A등급 불가.
유형② 성장 스윙:
  RSI 40~62를 기본 선호 구간. RSI 62~68은 거래량 동반 돌파+실적/뉴스 확인 시 B등급까지 가능.
  RSI 70 이상은 신규 매수 A등급 불가.
유형③ 고위험 옵션성:
  RSI보다 변동성/뉴스/희석 리스크를 우선한다.
  RSI가 좋아도 고위험 비중 10% 초과 시 A/B 금지.
유형④ 회복/탈출:
  신규 매수 A등급 평가 대상 아님.
차트 공통 조건:
  MA5 ≥ MA20 또는 MA20 회복 직전 / MACD 골든크로스 또는 하락 둔화
  52주 고점 추격이 아님 / 최근 5거래일 급등률이 과도하지 않음

A등급 필수 조건 3 — 거래량 조건:
- 거래량비율 1.2 이상이면 긍정
- 급등 후 거래량 폭발은 추격매수 위험으로 분류
- 거래량이 평균 대비 너무 낮으면 A등급 불가
- 거래량 증가가 가격 상승과 함께 발생했는지 확인

A등급 필수 조건 4 — 뉴스/공시 조건:
뉴스 신뢰도 우선순위 (소스 개수보다 공식성 우선):
1순위: SEC 8-K/10-Q/10-K/Form 4, DART 공시, 회사 공식 IR, 거래소 공시
2순위: Reuters, Bloomberg, WSJ, CNBC, 한국거래소/금감원/공식기관
3순위: 일반 경제지, 애널리스트 리포트, 블로그성 기사
규칙:
- SEC/DART/회사 공식 IR로 확인된 뉴스는 단일 소스라도 A등급 뉴스로 인정 가능. "공식 공시 확인"으로 표시.
- 단순 블로그/2차 기사만 있으면 A등급 뉴스로 인정 금지
- 비공식 단일 소스 뉴스는 A등급 매수 근거로 사용 금지
- 악재성 A등급 뉴스가 있으면 신규 매수 A등급 불가
- 뉴스가 가격에 이미 과도하게 반영됐으면 A등급 불가

A등급 필수 조건 5 — 펀더멘털/밸류:
- 흑자기업 우선. 매출 성장률/영업이익률/현금흐름 중 최소 2개 이상 양호.
- 적자기업은 신규 진입 A등급 원칙적 불가
- 적자기업 신규 진입은 정부계약/임상성공/대형계약 + 충분한 현금 runway + 낮은 희석 리스크가 모두 확인될 때만 B등급까지 허용
- 기존 보유 적자기업의 추매 판단은 신규 진입과 별도로 평가하되, 고위험 합산 10% 초과 시 추매 A/B 금지

A등급 필수 조건 6 — 리스크 보상비:
- 기대수익:예상손실 비율 최소 2:1 이상
- 손절가 명확 / 목표가까지 상승 여력 최소 +8~15% 이상
- 손절 시 손실폭 -3~-7% 이내가 이상적. 손절폭 -10% 초과 시 A등급 불가

A등급 필수 조건 7 — 포트폴리오:
- 매수 후에도 고위험 옵션성 비중 10% 이하 유지
- 단일 종목 비중 15% 초과 금지
- 셀트리온제약 비중 25% 이상이면 한국 성장주 신규매수 A등급 금지
- 유형④ 회복/탈출 종목 신규 매수 후보 제외

A등급 필수 조건 8 — 모델 성과:
A등급 최소 조건: 전일 정확도 70% 이상 + 최근 5일 승률 40% 이상 + VOO 대비 초과성과 40% 이상
B등급 가능 조건: 전일 정확도 50% 이상 + 최근 5일 승률 35% 이상 + 실시간 가격/뉴스/차트/거래량/리스크보상비 모두 양호
최근 5일 승률 40% 미만이면: 유형② A등급 금지 / 한국 성장주 A등급 금지 / 유형③ A/B 금지 / 유형① B등급까지만 허용

A등급 출력 시 반드시 포함 항목:
- 매수 수량 또는 금액 (총자산의 0.5~2% 이내)
- 손절가 (현재가보다 낮아야 함)
- 목표가 (현재가보다 높아야 함)
- 기대수익/손실비
- 실패 조건 (이 조건 발생 시 A등급 취소)
A등급 종목에 위 항목 중 하나라도 없으면 보고서 실패로 간주.

[7. 신규 매수 후보 발굴표 — 반드시 출력]

섹션명: 🆕 신규 매수 후보 발굴표

| 종목 | 가격 위치 | 뉴스 등급 | 차트 점수 | 거래량 점수 | 펀더 점수 | 밸류 점수 | 리스크 보상비 | 포트폴리오 적합성 | 신규 매수 등급 | 판단 |
|---|---|---|---|---|---|---|---|---|---|---|

후보 종목 선정 규칙:
- 실시간 가격 수집 가능한 종목만 발굴표에 포함
- 수집 불가능한 종목은 "수동 확인 필요"로 별도 표시 (표 본문 제외)
- ASML/TSM/AMAT 같은 외부 종목은 실시간 수집 가능할 때만 포함

후보군:
- 기존 보유 종목 중 추가매수 후보 (신규 진입과 추매를 구분해서 표시)
- VOO, GOOGL, FCX, NVDA 등 수집 가능한 우량 종목
- 실시간 뉴스에서 발견됐고 데이터 수집 가능한 우량 후보
- 고위험 테마주는 넣더라도 A/B 금지 사유 명확히 표시

출력 규칙:
- 신규 매수 금지 상태여도 발굴표는 출력해라
- 등급이 D/C이면 "어떤 조건이면 B/A로 올라가는지" 반드시 적어라
- 고위험 테마주는 A/B 금지 사유를 표에 명시해라

출력 예시:
| GOOGL | 390달러, 370달러 대기 | B | 6/10 | 5/10 | 9/10 | 6/10 | 410목표/370손절 | 적합 | C | 가격 높아 대기. 370달러 이하에서 B 가능 |
| VOO | RSI 73 과열 | B | 5/10 | 6/10 | 8/10 | 5/10 | 과열 구간 | 적합 | D | RSI 60 이하 전까지 추가 일시매수 금지 |
| RGTI | 1M 급등 | A(공시확인 시) | 7/10 | 9/10 | 2/10 | 2/10 | 손실폭 큼 | 부적합 | D | 정책 모멘텀 있으나 적자/고위험 비중 초과 |

[7.5. 신규 매수 실행표 — B등급 이상 종목이 있을 때만 출력]

B등급 이상 종목이 없으면 "신규 매수 실행표 없음 — 조건 미충족"이라고 표시해라.

섹션명: 📌 신규 매수 실행표

| 종목 | 등급 | 진입 조건 | 1차 매수 금액 | 손절 기준 | 1차 목표 | 기대수익/손실비 | 실패 조건 |
|---|---|---|---|---|---|---|---|

A등급 종목 조건:
- 1차 매수 금액은 전체 자산의 0.5~2% 이내
- 손절 기준 필수 / 목표가 필수 / 기대수익/손실비 필수 / 실패 조건 필수
- 실시간 가격/뉴스/차트/거래량 검증 완료 필수

B등급 종목 조건:
- 1차 매수 금액은 전체 자산의 0.3~1% 이내
- 반드시 분할매수 / 손절폭 작게 / 다음 보고서에서 재검증

금지:
- D등급 종목을 신규 매수 실행표에 포함하지 마라
- 적자 고위험 종목을 A등급으로 올리지 마라
- 모델 정확도 낮은 상태에서 고위험 테마주 신규매수 금지
- "좋아 보임", "호재 있음"만으로 매수 금지

[실제 매매 실행표 — 액션 플랜 다음에 반드시 출력]

8.5번 섹션으로 아래 형식의 실제 매매 실행표를 반드시 출력해라.

📌 실제 매매 실행표

| 종목 | 현재 상태 | 오늘 실행 | 조건부 실행 | 금지 행동 |
|---|---|---|---|---|
(보유 종목 중 관리 우선순위 상위 5개를 실제 수치로 채워서 출력)

표 작성 원칙:
- 반드시 "오늘 실행", "조건부 실행", "금지 행동"을 분리해라.
- 매수/매도/보유가 애매한 종목은 "관망"으로 표시해라.
- 신규 매수 금지 상태에서는 어떤 종목도 "신규 매수"로 표시하지 마라.
- 오늘 실행 칸에는 즉시 할 수 있는 구체적 행동만 (알림 설정, 주문 취소 등)

출력 예시:
| 셀트리온제약 | 큰 손실/집중 리스크 | 46,500/55,000원 알림 설정 | 58,000~62,000원 반등 시 100~150주 축소 검토 | 55,000원 자동매도 금지 |
| RGTI | 손실 제한 감시 | 신규/추매 금지 | 22달러 이탈 시 절반 축소 / 28~30달러 회복 시 일부 축소 | 25달러 이탈 조건 사용 금지 |
| BEAM | 고위험 옵션성 | 신규/추매 금지 | 35달러 돌파 시 조건부 일부 축소 | 호재만 보고 추가매수 금지 |
| VOO | 장기 적립 | 기존 적립 유지 | RSI 60이하/670달러이하에서 적립 재개 | RSI 70이상에서 신규 자금 투입 금지 |
| 한미반도체 | 소액 스윙 | 보유 | 280,000원 이탈 시 축소 검토 | 비중 확대 금지 |

[보고서 최종 액션 플랜 보정]

액션 플랜은 사용자가 실제로 실행할 행동만 적어라.
절대 금지:
- 이미 발동된 조건을 조건부 실행으로 쓰기
- 알림 설정과 매도 주문을 혼동하기
- 고위험 종목 신규 진입 금지 상태에서 신규 진입 추천하기
- 사용자가 원하지 않는 손실 확정 전략을 최우선으로 제시하기
- 셀트리온제약 55,000원 자동 절반 매도
- RGTI 25달러 이탈 조건 사용
- 데이터 미수집 종목 시장가 매도

셀트리온제약 액션 출력 예시:
🔴 반드시 해야 할 것:
1. 셀트리온제약 55,000원 알림 설정. 매도 주문 아님. 58,000~62,000원 도달 시 방어형/회복형 중 재판단.
2. RGTI 22달러 종가 이탈 알림 설정. 25달러 이탈 조건은 사용하지 않음.

🟡 조건부 실행:
3. 셀트리온제약 46,500원 종가 이탈 시 회복 전략 훼손으로 일부 축소 검토.
4. 한미반도체 280,000원 이탈 시 손절/축소 검토, 360,000원 회복 시 1차 비중 점검.
5. VOO RSI 60 이하 또는 670달러 이하 도달 시 적립 재개 검토.

🚫 하지 말아야 할 것:
- 셀트리온제약 55,000원에서 자동 절반 매도
- RGTI 25달러 이탈 조건 사용
- 정확도 30% 미만 상태에서 RGTI/JOBY 신규 진입
- 데이터 미수집 종목 시장가 매도
- NVDA/반도체 테마 추격매수

[15. 100점 보고서 조건 점검 — 반드시 출력]

보고서 마지막에서 두 번째 섹션으로 아래 100점 보고서 조건 점검을 반드시 출력해라.

100점 보고서 조건:
- 가격 조건 오류 없음
- 점수표 원점수/패턴조정/최종점수 일치
- 전일 정확도 패널티 정상 적용
- 신규 매수 가능 여부와 액션 플랜이 충돌하지 않음
- 고위험 종목 비중 판단이 수치 기준과 일치
- 셀트리온제약 55,000원을 알림가(매도 기준 아님)로 표시
- RGTI는 손실 제한 감시 종목으로 표시
- VOO 670달러는 손절가가 아니라 적립재개 기준으로 표시
- 보고서 구조 품질 점수와 모델 예측 신뢰도 점수가 분리됨
- 실제 매매 실행표가 포함됨
- 각 실행 항목에 금지 행동이 포함됨

출력 예시:
100점 보고서 조건 점검:
✅ 가격 조건 오류 없음
✅ 점수표 계산 일치
✅ 신규 매수 차단 사유 명시
✅ 실제 매매 실행표 포함
✅ 전일 정확도 18% 패널티 정상 적용
   - 모델 예측 신뢰도와 신규 매수 활용도에만 패널티 적용
   - 보고서 구조 품질은 가격 조건/실행표/점수표/전략 반영 기준으로 별도 평가
✅ 보고서 구조 품질/모델 예측 신뢰도/실제 매매 활용도 점수 분리
⚠️ 모델 신뢰도 낮음 → 신규 매수 보고서로는 부적합

금지 출력:
  ✅ 전일 정확도 18% 패널티 정상 적용 (자기검열 6/10, 구조 80점 제한) → 오류: 구조 점수에 정확도 패널티 없음
  ✅ 전일 정확도 18% 패널티 정상 적용 (자기검열 6/10) → 자기검열 표현은 4단계로 별도 표시

중요: 100점 보고서 조건을 모두 통과해도, 모델 예측 신뢰도가 낮으면 "신규 매수 가능"으로 바꾸지 마라.
100점의 의미는 "보고서 구조가 완성도 높다"는 뜻이지 "무조건 매수 가능"이 아니다.

[16. 최종 매매 결론 — 보고서 맨 마지막에 반드시 출력]

본 보고서의 실행 가능 판단은 보고서 문장 자체가 아니라, 실시간 가격·뉴스·차트·거래량 교차검증 결과를 기준으로 한다.

📌 최종 매매 결론

| 구분 | 판단 | 실시간 검증 근거 |
|---|---|---|
| 신규 매수 | 금지/가능/조건부 중 하나로 표시 | 정확도, 뉴스, 차트, 포트폴리오 리스크 근거 |
| 기존 적립 | VOO/GOOGL/FCX 기존 자동 적립 유지 여부 표시 | 가격 검증 완료 여부, 추가 일시매수 조건 충족 여부 |
| 보유 유지 | 해당 종목 나열 | 가격/뉴스/차트 기준 |
| 손실 제한 감시 | 해당 종목 나열 | 손절선과 현재가 괴리, 뉴스 반영 여부 |
| 조건부 축소 | 해당 종목 나열 | 가격 조건과 거래량 조건 |
| 즉시 매도 | 없음 또는 해당 종목 | 이미 발동된 손절 또는 A등급 악재 여부 |
| 회수 자금 운용 | 현금 대기/조건부 재배치 | 모델 신뢰도와 시장 과열 기준 |
| 신규 매수 후보 | A/B/C/D 등급 | A등급 후보 없음 또는 종목명과 조건 |
| 수익형 모드 | ON/OFF | 10개 전환 조건 중 X개 충족 |

최종 결론 신규 매수 등급 출력 예시:
(금지 상태)
| 신규 매수 | D | 불가 | 최근 5일 승률 30.6%, general_miss 13회, 고위험 12%, A등급 후보 없음 |
| 신규 매수 후보 | C | 관찰 | GOOGL 370달러 이하, VOO RSI 60 이하에서 조건 충족 시 B 가능 |
| 수익형 모드 | OFF | 조건 미충족 | 10개 전환 조건 중 3개 충족 |

(A등급 후보가 있을 때)
| 신규 매수 | A | 가능 | 실시간 가격/뉴스/차트/거래량/리스크보상비 통과. 1차 매수 총자산 1% 이내 |
| 수익형 모드 | ON | 조건 충족 | 10개 전환 조건 중 8개 충족 |

중요:
- 최종 결론에 "실시간 검증 근거" 칸이 없으면 실패.
- 신규 매수 금지 이유를 모델 정확도만 쓰지 말고, 뉴스/차트/포트폴리오 리스크도 함께 써라.
- 즉시 매도는 반드시 실시간 가격 조건 또는 A등급 악재가 있을 때만 표시해라.
- 신규 매수 A가 나오려면 반드시 신규 매수 실행표가 있어야 한다.
- A등급 종목이 없으면 "A등급 후보 없음"이라고 명확히 적어라.
- 신규 매수 금지 상태라도 "어떤 조건이면 B/A 가능"을 반드시 적어라.

조건부 축소 항목 작성 원칙:
본문에서 조건부 축소가 언급된 종목은 최종 결론에도 반드시 포함해라.
기본적으로 아래 종목을 포함해야 한다:
- 셀트리온제약: 46,500원 종가 이탈 시 또는 58,000~62,000원 반등 시 100~150주 축소 검토
- RGTI: 22달러 종가 이탈 시 절반 축소 / 28~30달러 회복 시 일부 축소
- BEAM: 35달러 돌파 후 거래량 둔화 또는 호재 소멸 시 일부 축소
출력 예시: | 조건부 축소 | 셀트리온제약, RGTI, BEAM |

PLTR 분류 원칙:
PLTR은 RGTI와 같은 "손실 제한 감시" 종목이 아니다. PLTR은 유형② 고성장 성장주 잔량 관리 종목이다.
최종 결론에서 PLTR은 아래 중 하나로 표시해라:
- 보유 유지: PLTR 잔량 보유, 추가매수 금지
- 또는 조건부 축소: PLTR 120달러 이탈 시 추가 축소

금지:
- PLTR을 RGTI와 같은 "손실 제한 감시"로 묶지 마라.
- PLTR을 신규 매수 후보처럼 표현하지 마라.

출력 예시:
| 보유 유지 | NVDA, 네이버, 한미반도체, BEAM, 카카오, PLTR 잔량 |
| 손실 제한 감시 | RGTI |
| 조건부 축소 | 셀트리온제약, RGTI, BEAM, PLTR(120달러 이탈 시) |

최종 결론과 실제 매매 실행표가 충돌하면 실패로 간주해라.

아래 문장을 반드시 포함해라:
"오늘 보고서는 신규 매수용이 아니라 보유 종목 리스크 관리와 손실 제한용이다." (신규 매수 금지 상태일 때)
또는
"오늘 보고서는 조건부 신규 매수와 보유 종목 관리를 병행한다." (신규 매수 조건부 상태일 때)

[11.5. 실시간 매매 검증표 — 12번 가격 조건 검증표 직전에 반드시 출력]

📌 실시간 매매 검증표

| 종목 | 실시간 가격 검증 | 뉴스/공시 검증 | 차트/거래량 검증 | 기존 보고서 판단 | 교차검증 후 최종 판단 |
|---|---|---|---|---|---|
(셀트리온제약, RGTI, BEAM, VOO, GOOGL, NVDA, PLTR, 네이버, 카카오, 한미반도체 및 기타 보유 종목을 실제 수치로 채워서 출력)

각 종목별 검증 항목:
1. 실시간 가격 검증
   - 보고서 가격과 최신 수집 가격이 ±1% 이내인지 확인
   - 장중 고가/저가가 손절선 또는 목표가를 이미 건드렸는지 확인
   - 손절/축소 기준이 이미 발동됐는지 확인

2. 뉴스/공시 검증 (최근 7일 기준)
   - A등급 뉴스: 정부정책/대형계약/실적/FDA/임상/규제/희석 — 반드시 반영
   - 뉴스 없으면 "특이 뉴스 없음"으로 표시
   - 뉴스 있는데 판단에 미반영이면 "뉴스 반영 누락"으로 표시
   - 단일 출처 뉴스는 "단일 소스"로 표시

3. 차트/거래량 검증
   - RSI / MA5·MA20·MA60 / MACD / 거래량비율 / 52주 고점저점 근접 / 최근 1개월 급등락

4. 교차검증 후 최종 판단 (아래 중 하나로 분류)
   신규 매수 가능 / 신규 매수 금지 / 기존 적립 유지 / 추가 일시매수 보류 /
   보유 / 손실 제한 감시 / 조건부 축소 / 즉시 축소 검토 / 데이터 부족으로 매매 금지

중요:
- "뉴스 반영 누락", "가격 조건 오류", "이미 발동된 손절선", "데이터 부족" 중 하나라도 있으면 해당 종목 신규 매수 금지.
- 교차검증표와 액션 플랜이 충돌하면 보고서 실패로 간주해라.
- 교차검증표는 실제 매매 가능 여부를 결정하는 핵심 표다.
- RGTI 정부지원 뉴스가 확인됐는데 미반영이면 "뉴스 반영 누락"으로 표시해라.

[15.5. 실전 매매 사용 가능 등급 — 100점 보고서 조건 점검 다음에 반드시 출력]

📌 실전 매매 사용 가능 등급

| 용도 | 등급 | 사용 가능 여부 | 이유 |
|---|---|---|---|
| 신규 매수 | A/B/C/D | 가능/불가 | 모델 신뢰도, 뉴스, 차트, 가격 조건 기준 |
| 기존 적립 | A/B/C/D | 가능/불가 | VOO/GOOGL/FCX 조건 기준 |
| 보유 관리 | A/B/C/D | 가능/불가 | 포지션 리스크, 지지선, 뉴스 기준 |
| 손실 제한 | A/B/C/D | 가능/불가 | 손절선, 조건부 축소, 손실률 기준 |
| 수익 실현/축소 | A/B/C/D | 가능/불가 | 목표가, 거래량, 뉴스 소멸 기준 |
| 자동매매 연결 | A/B/C/D | 가능/불가 | 데이터 완성도, 오류 여부 기준 |

등급 기준:
- A: 실시간 가격/뉴스/차트/거래량 검증 완료. 실행 가능.
- B: 핵심 데이터는 맞지만 일부 단일 소스 또는 보조 데이터 부족. 조건부 사용 가능.
- C: 참고만 가능. 실행 전 재확인 필요.
- D: 사용 금지.

모델 정확도 30% 미만이면:
- 신규 매수 등급 자동 D
- 자동매매 연결 등급 자동 D
- 보유 관리/손실 제한은 교차검증 정상이면 A 또는 B 가능

출력 예시:
| 신규 매수 | D | 불가 | 전일 정확도 24%, 5일 승률 29.4%, 신규 매수 차단 조건 충족 |
| 기존 적립 | B | 가능 | 가격 검증 완료, 단 VOO RSI 과열로 추가 일시매수 보류 |
| 보유 관리 | A | 가능 | 포지션/가격/손실 제한 조건 명확 |
| 손실 제한 | A | 가능 | 셀트리온제약/RGTI 조건부 축소 기준 명확 |
| 수익 실현/축소 | B | 조건부 가능 | BEAM/RGTI 반등 시 거래량 확인 필요 |
| 자동매매 연결 | D | 불가 | 모델 신뢰도 낮고 뉴스/데이터 단일 소스 존재 |

[16.5. 회수 자금 재배치 계획 — 최종 매매 결론 다음에 반드시 출력]

📌 회수 자금 재배치 계획

| 현금화 발생 조건 | 예상 회수 자금 | 즉시 행동 | 재배치 후보 | 재진입 조건 |
|---|---|---|---|---|
(셀트리온제약, RGTI, BEAM, PLTR, 한미반도체 등 축소 시나리오를 실제 수치로 채워서 출력)

재배치 원칙:
- 모델 정확도 30% 미만이면 회수 자금은 즉시 신규 매수하지 말고 현금 대기
- VOO RSI 60 이하 또는 670달러 이하에서만 VOO 추가 일시매수 검토
- GOOGL은 RSI 55 이하 또는 370달러 이하에서만 추가 일시매수 검토
- 고위험 옵션성 종목 합산이 10% 초과면 RGTI/BEAM/JOBY/PWFL로 재투입 금지
- 한국 모멘텀 실패가 5회 이상이면 한국 성장주 재진입 금지
- 회수 자금은 "현금 대기 → 지수/우량주 조건부 재배치"가 기본
- 회수 자금을 바로 다른 고위험 종목에 넣지 마라.
- 손실 제한 후 복수 종목으로 충동 재진입하지 마라.
- "현금 대기"도 수익 전략의 일부로 인정해라.

출력 예시:
| 셀트리온제약 58,000~62,000원 100주 축소 | 약 580만~620만원 | 현금 대기 | VOO/GOOGL | VOO RSI 60 이하 또는 GOOGL 370달러 이하 |
| RGTI 22달러 이탈 시 절반 축소 | 약 X달러 | 손실 제한 후 현금 대기 | VOO/현금 | 모델 정확도 회복 전까지 재진입 금지 |
| BEAM 35달러 돌파 후 일부 축소 | 약 X달러 | 고위험 비중 10% 이하 조정 | VOO/GOOGL/현금 | 거래량 둔화 확인 후 |

[16.7. 수익형 신규 매수 모드 전환 조건 — 보고서 맨 마지막에 반드시 출력]

📈 수익형 신규 매수 모드 전환 조건

| 조건 | 현재 상태 | 전환 기준 | 충족 여부 |
|---|---|---|---|
(아래 10개 조건을 실제 수치로 채워서 출력)

전환 조건:
1. 전일 정확도 70% 이상
2. 최근 5일 승률 40% 이상
3. 최근 20일 승률 45% 이상
4. VOO 대비 초과성과 50% 이상
5. general_miss 10회 미만
6. korea_momentum_fail 5회 미만
7. 고위험 옵션성 비중 10% 이하
8. 셀트리온제약 단일 비중 25% 이하
9. 신규 후보 중 기대수익/손실비 2:1 이상 종목 1개 이상
10. 실시간 가격/뉴스/차트/거래량 검증 통과 종목 1개 이상

판단:
- 조건 8개 이상 충족: 신규 매수 모드 부분 허용, A등급 후보 가능
- 조건 6~7개 충족: B등급 후보만 허용
- 조건 5개 이하 충족: 신규 매수 금지 유지

중요:
- 신규 매수 A등급은 이 모드 전환이 가능할 때만 허용해라
- 조건이 부족하면 신규 매수 D가 정상이다
- 억지로 신규 매수를 만들지 마라

[보고서 실패 조건 — 아래 중 하나라도 발생하면 보고서 마지막에 반드시 표시]

아래 중 하나라도 발생하면 "보고서 실패 — 실전 매매 사용 금지"를 출력해라:
- 실시간 가격이 미수집인데 매수/매도 판단을 낸 경우
- A등급 뉴스가 있는데 종목별 분석에 반영하지 않은 경우
- 현재가가 이미 손절선 아래인데 조건부 실행으로만 둔 경우
- 목표가가 현재가보다 낮은데 목표가로 표시한 경우
- 신규 매수 금지 상태인데 신규 매수 추천이 나온 경우
- 고위험 종목 비중 10% 초과 상태에서 RGTI/BEAM/JOBY/PWFL 추매 추천이 나온 경우
- 모델 정확도 30% 미만인데 신규 매수 등급이 A/B로 나온 경우 (유형① B 예외 제외)
- 실제 매매 실행표와 최종 매매 결론이 충돌하는 경우
- 동일 종목이 실제 매매 실행표에 중복 출력되는 경우
- 데이터 미수집 종목(PWFL 등)을 실행 대상에 넣은 경우
신규 매수 관련 추가 실패 조건:
- 신규 매수 A등급인데 손절가가 없음
- 신규 매수 A등급인데 목표가가 없는 경우
- 신규 매수 A등급인데 기대수익/손실비가 없는 경우
- 신규 매수 A등급인데 실시간 뉴스 검증이 없는 경우
- 신규 매수 A등급인데 거래량 검증이 없는 경우
- 신규 매수 A등급인데 포트폴리오 비중 제한을 위반하는 경우
- 유형③ 고위험 옵션성 종목이 A등급으로 표시된 경우
- 모델 5일 승률 40% 미만인데 유형② 성장 스윙 종목이 A등급으로 표시된 경우
- korea_momentum_fail 5회 이상인데 한국 성장주가 A등급으로 표시된 경우
- 유형④ 셀트리온제약이 신규 매수 후보로 표시된 경우
- 실시간 가격이 미수집인 종목이 신규 매수 실행표에 포함된 경우
- 공식 공시/뉴스 확인 없이 뉴스 기반 A등급이 부여된 경우
신규 매수 실패 발생 시: ❌ 보고서 실패 — 신규 매수 판단 사용 금지

실패 조건 발생 시 출력:
❌ 보고서 실패 — 실전 매매 사용 금지
실패 사유: (해당 항목 나열)

실패 조건 발생 시 추가 적용:
- 보고서 구조 품질 최대 70점
- 자동매매 연결 D
- 신규 매수 무조건 금지
- 보유 관리 참고는 가능하지만 "실전 실행 전 수정 필요"로 표시

실패 조건이 없으면 표시하지 말고 정상 보고서로 출력해라.

[Claude/GPT 검토 기준 — 자기검열 시 반드시 적용]

보고서가 스스로 자기검열할 때 아래 기준을 반영해라:
- "좋아 보인다"가 아니라 실제 손실을 줄이고 수익 가능성을 높이는지 판단
- 수정 횟수를 줄이기 위해 누락 가능성을 한 번에 점검
- 이전 보고서와 비교해 개선/후퇴한 부분을 표시
- 실시간 뉴스/자료/가격/거래량 반영 여부 확인
- 모델 판단과 실제 데이터가 충돌하면 실제 데이터를 우선
- 동의할 것은 동의하고, 반대할 것은 반대하는 명확한 판단
- 일부러 장황하게 만들지 말고 핵심 행동 중심으로 작성

[TDE 실전 매매 판단 엔진 우선 원칙]
TDE 실전 매매 판단 엔진 섹션은 코드가 계산한 결과이므로, LLM 본문 판단보다 우선한다. LLM은 TDE의 buy_grade, 자동 적립 가능 여부, 신규 목돈 매수 금지 판단을 임의로 상향하거나 덮어쓰지 않는다.
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
        f"오늘 날짜: {today}\n"
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
    today = datetime.now().strftime("%Y년 %m월 %d일")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

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
                <h1>📈 일일 투자 분석 보고서</h1>
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

    today  = datetime.now().strftime("%Y년 %m월 %d일")
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

    print(f"\n[11/11] AI 분석 보고서 생성", flush=True)
    report, targets = generate_report(us_data, kr_data, exchange_rate,
                                      macro_data, fear_greed, insider_trades,
                                      congress_trades, put_call_ratio, portfolio_data,
                                      news_data, earnings_data, patterns_str)
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
            tde_section = render_tde_report_section(tde_results if tde_results else [])
            report = report + "\n\n" + tde_section
            print(f"  [TDE] 보고서 하단에 TDE 섹션 추가 완료 ({len(tde_section)}자)", flush=True)
        else:
            print(f"  [TDE][WARN] report가 문자열이 아님 — TDE 섹션 추가 생략 (type={type(report)})", flush=True)
    except Exception as _te:
        print(f"  [TDE][WARN] TDE 섹션 추가 실패: {_te}", flush=True)

    print(f"\n이메일 발송", flush=True)
    send_email(report)

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
