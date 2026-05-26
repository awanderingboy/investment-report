import anthropic
import yfinance as yf
import smtplib
import schedule
import time
import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
    "category3": [],
    "category3_cash": 5000000,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
SEC_HEADERS = {"User-Agent": "investment-report-bot/1.0 xogus5512@gmail.com"}


# ── 포트폴리오 로드 ──────────────────────────────────────────────────────────
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


def _portfolio_section_str(pf: dict) -> str:
    lines = ["[포트폴리오 카테고리 - 3가지로 완전히 분리]\n"]

    lines.append("카테고리 1 - 주식 모으기 중 (매일 자동 적립, 절대 단기 매도 금지):")
    for it in pf.get("category1", []):
        avg = f"${it['avg_price']:.2f}" if it["currency"] == "USD" else f"{it['avg_price']:,.0f}원"
        daily = f" / 매일 ${it['daily_buy']} 적립" if it.get("daily_buy") else ""
        lines.append(f"- {it['name']}({it['ticker']}): 평단 {avg} / {it['shares']}주{daily}")

    lines.append("\n카테고리 2 - 현재 보유 중 (적립 없음, 매매 판단 필요):")
    for it in pf.get("category2", []):
        avg = f"${it['avg_price']:.2f}" if it["currency"] == "USD" else f"{it['avg_price']:,.0f}원"
        lines.append(f"- {it['name']}({it['ticker']}): 평단 {avg} / {it['shares']}주")

    cat3_cash     = pf.get("category3_cash", 5000000)
    cat3_holdings = pf.get("category3", [])
    lines.append("\n카테고리 3 - 500만원 프로젝트 (다른 포트폴리오와 완전 분리):")
    lines.append(f"- 시드 현금: {cat3_cash:,}원")
    if cat3_holdings:
        lines.append("- 현재 보유:")
        for it in cat3_holdings:
            avg = f"${it['avg_price']:.2f}" if it["currency"] == "USD" else f"{it['avg_price']:,.0f}원"
            lines.append(f"  * {it['name']}({it['ticker']}): 평단 {avg} / {it['shares']}주")
    else:
        lines.append("- 현재 미투자 (AI가 종목 추천)")
    lines.append("- 목표: 6개월마다 2배, 최종 목표 1억")
    lines.append("- 현재 단계: 1단계 (500만원 → 1,000만원)")

    cash = pf.get("cash", {})
    krw  = cash.get("krw", 0)
    usd  = cash.get("usd", 0)
    lines.append(f"\n보유 현금: {krw:,}원 + ${usd:,} (500만원 프로젝트 시드 별도)")
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
    results = {}
    for name, ticker in macro_tickers.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", auto_adjust=True)
            if hist.empty:
                print(f"  [{name}] 데이터 없음", flush=True)
                results[name] = None
                continue
            close = hist["Close"].astype(float).dropna()
            current = close.iloc[-1]
            change_pct = round((current / close.iloc[-2] - 1) * 100, 2) if len(close) >= 2 else None
            results[name] = {"value": round(current, 2), "change_pct": change_pct}
            chg_str = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
            print(f"  [{name}] {round(current, 2)}{chg_str}", flush=True)
        except Exception as e:
            print(f"  [{name}] 수집 실패: {e}", flush=True)
            results[name] = None
    return results


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

    try:
        price = stock.fast_info.last_price
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: fast_info", flush=True)
            return price
    except Exception:
        pass

    for key in ("currentPrice", "regularMarketPrice"):
        price = info.get(key)
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: info[{key}]", flush=True)
            return price

    if is_kr:
        price = info.get("regularMarketPreviousClose")
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: regularMarketPreviousClose (KR 폴백)", flush=True)
            return price

    if not hist.empty:
        print(f"    [{ticker}] 현재가 출처: hist 폴백", flush=True)
        return hist["Close"].iloc[-1]

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


# ── 보고서 생성 ───────────────────────────────────────────────────────────────
def generate_report(us_data, kr_data, exchange_rate,
                    macro_data, fear_greed, insider_trades,
                    congress_trades, put_call_ratio, portfolio_data=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%Y년 %m월 %d일")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    pf           = portfolio_data if portfolio_data is not None else _HARDCODED_PORTFOLIO
    _pf_section  = _portfolio_section_str(pf)
    _restricted  = _restricted_tickers_list(pf)

    static_system = """너는 500만원에서 시작해 자산 10억 이상을 달성한 전문 퀀트 트레이더이자 포트폴리오 매니저다.
목표는 단 하나 — 사용자가 최대한 많은 돈을 버는 것.
감정 없이 냉정하게, 데이터와 확률 기반으로 분석하라.
손실 = 손절이 아님을 명심하라. 근거가 살아있으면 홀딩, 근거가 무너지면 손절.

__PORTFOLIO__

아래 형식으로 보고서를 작성하라. 반드시 HTML로 작성하라.

<h2>📊 오늘의 시장 총평</h2>
미국/국내 시장 전체 분위기, 주요 이슈, 섹터 흐름 3~5줄.
오늘 주목해야 할 핵심 변수 1가지를 <strong>굵게</strong> 강조하라.

<h2>🌍 거시경제 & 시장 심리</h2>
수집된 실제 데이터를 기반으로 아래 항목을 빠짐없이 작성하라:
- Fear & Greed Index: 수치, 등급, 의미 해석
- VIX: 수치와 시장 공포 수준 (20 이하=안정, 20~30=주의, 30 이상=공포)
- Put/Call Ratio: 수치와 기관 방향성 해석 (1.0 이상=하락 베팅 우세)
- DXY 달러 인덱스: 수치, 강달러/약달러 여부, 주식시장 영향
- 미국 10년 국채 금리: 수치, 성장주/가치주에 미치는 영향
- 원유(WTI): 수치, 인플레이션 및 에너지 섹터 영향
- 금: 수치, 안전자산 수요 해석
- 데이터 수집 실패 항목은 ⚠️ 표시 후 수동 확인 안내
종합 판단: 지금 시장이 아래 셋 중 하나로 명확하게 결론 내려라
  🟢 매수 우위 / 🟡 관망 / 🔴 리스크 회피
- 🟢/🟡/🔴 판단을 내릴 때 반드시 "판단 근거 지표 3개"를 명시하라.
  예: "🟡 관망 — VIX 16.59(안정)+금 신고가(후기사이클)+RSI 69(과열) 3개 시그널 충돌"
- 판단과 함께 "이 판단이 바뀌는 조건"도 한 줄 추가하라.
  예: "VIX 25 돌파 시 🔴로 전환 / Fear&Greed 45 이하 시 🟢로 전환"

<h2>🏛️ 내부자 거래 & 정치인 동향</h2>
수집된 실제 데이터 기반으로:
- SEC 내부자 거래: 종목별 CEO/임원 자사주 매수/매도 내역
  매수 = 🟢 강한 상승 신호, 매도 = 🔴 주의 신호로 해석
- 정치인 거래: 최근 거래 내역, 방향성 해석
- 보유 종목(NVDA, GOOGL, FCX, PLTR, BEAM, PWFL, VOO) 관련 내용은
  반드시 ⚠️ 표시하고 굵게 강조
- 데이터 수집 실패 항목은 ⚠️ 표시 후 수동 확인 안내
- 내부자 거래 방향(매수/매도)이 확인되지 않은 경우:
  * "방향 미확인" 으로 표시하되 투자 판단에 반영하지 마라.
  * 단, 동일 종목에서 동일 날짜 3건 이상 신고 = 매도 패턴 확률 70% 이상으로 간주하고
    해당 종목 판단에 "내부자 매도 의심" 경고를 반드시 추가하라.
  * 방향이 확인된 경우에만 🟢 상승 신호 또는 🔴 주의 신호로 표시하라.

<h2>💼 내 포트폴리오 진단</h2>
보고서 작성 순서:
① 먼저 전체 종목을 훑어 🔴즉시매도와 💎강력매수 해당 종목을 파악한다.
② 섹션 맨 위에 "⚡ 오늘 즉시 행동할 것" 박스를 만들어
   🔴즉시매도 종목과 💎강력매수 종목만 굵게 요약한다. (3줄 이내)
③ 그 다음 전체 표를 작성한다.
이렇게 하면 보고서를 처음 읽는 사람이 가장 중요한 판단을 먼저 본다.

카테고리 1과 카테고리 2를 각각 별도 표로 작성.
각 종목마다 아래 항목을 빠짐없이 모두 작성하라.
데이터가 없으면 "N/A"라고 써라. 절대 항목을 생략하지 마라:
- 종목명
- 평단가
- 현재가
- 수익률(%)
- 평가손익 (원화 환산, 환율 적용)
- RSI14 수치
- MA 정배열 여부 (True/False)
- 거래량 비율 (최근 5일/20일 평균 대비, 예: 1.3배)
- 영업이익률 (%, 없으면 N/A)
- 매출성장률 (%, 없으면 N/A)
- PER (없으면 N/A)
- 판단: 반드시 아래 5가지 중 하나로만 표현
  🔴 즉시매도 / 🟠 비중축소 / 🟡 홀딩 / 🟢 추가매수 / 💎 강력매수
- 판단 근거: 2~3줄 핵심만
- 기회비용: 이 종목 보유 중 포기하는 다른 기회 명시
- 손절 트리거: "이 조건이 깨지면 손절" (가격 기준 아닌 근거 기반)
- 목표가: 단기(3개월) / 중기(1년)

판단 원칙:
- 🔴즉시매도와 💎강력매수는 반드시 굵은 글씨로 강조하고 이유 상세히 작성
- "검토해볼 만하다" "고려해볼 수 있다" 같은 모호한 표현 절대 금지
- 매도해야 하면 "지금 당장 매도하라"
- 매수해야 하면 "지금 바로 매수하라"
- 손실 확정이 필요한 종목은 "기회비용 X원, 지금 정리하고 Y종목으로 이동 권고"로 명시
- 장기 보유가 필요한 종목은 "최소 X개월 보유 전략"으로 명시
- 🟡홀딩 판단을 내릴 때는 반드시 아래 두 가지를 함께 명시하라:
  * "이 조건이 충족되면 계속 홀딩한다" (홀딩 유지 조건)
  * "이 조건이 깨지면 즉시 매도한다" (홀딩 → 매도 전환 트리거)
- "전환점 대기", "반등 모멘텀 대기" 같은 표현 금지.
  반드시 "X 지표가 Y 수준 도달 시" 처럼 구체적 숫자로 명시하라.

PWFL(파워플리트) 판단 원칙:
- 현재가·RSI·거래량비·영업이익률·매출성장률 중 3개 이상 데이터 미수집 시:
  판단은 반드시 🔴 즉시매도로 고정하라.
  근거: "데이터 없음 = 시장 관심 없음 = 유동성 함정"
- "다음 데이터 확인 후 결정" 같은 판단 유보 표현 절대 금지.
- 기회비용을 원화로 계산해서 반드시 명시하라.
  예: "1200주 × $3.68 × 1517원 = 약 670만원. 이 금액이 RGTI에 있었다면 현재 +X% 수익 중."

전체 포트폴리오 총 평가금액 (실시간 환율 적용), 총 손익, 현금 포함 총자산 계산.

총자산 계산 원칙:
- 반드시 아래 형식으로 종목별 계산을 한 줄씩 보여주고 합산하라:
  [종목명] 현재가 × 보유수량 × 환율(달러 종목만) = 원화금액
  예: NVDA $215.33 × 10주 × 1517원 = 3,266,551원
- 계산 후 항목별 소계를 명시하라:
  카테고리 1 소계: XXX원
  카테고리 2 소계(한국): XXX원
  카테고리 2 소계(미국): XXX원
  현금 소계: XXX원
  총합: XXX원
- 현재가가 N/A인 종목(PWFL 등)은 0원으로 처리하고 "(시가 미확인)" 명시.
- 이전 보고서와 총자산 차이가 20% 이상이면 계산을 다시 검토하고
  차이 원인을 한 줄 설명하라.

<h2>🔍 기술적 분석 기반 추천 종목 (단기 스윙)</h2>
수집된 실제 데이터 기반으로 기술적으로 매력적인 종목 3개.
각 종목마다 아래 항목을 빠짐없이 작성하라. 없으면 N/A:
- 종목명 및 현재가
- RSI14 실제 수치 및 해석
- MA5/MA20/MA60/MA120 실제 값 및 정배열 여부
- 거래량 비율 (실제 계산값, 20일 평균 대비 몇 배)
- 영업이익률 / 매출성장률
- MACD 골든크로스/데드크로스 여부
- 주요 지지/저항 구간
- 왜 지금인가 (위 데이터 종합 결론)
- 매수가 / 1차 목표가 / 2차 목표가
- 손절 트리거 (근거 기반)
- 예상 수익률 및 기간

<h2>📰 중장기 성장주 발굴</h2>
시장에 덜 알려진 소형~중형주 발굴.
엔비디아/SK하이닉스처럼 초기 저평가됐지만 폭발적 성장한 유형이 목표.
AI/반도체/바이오/에너지전환/방산/양자컴퓨팅/UAM 등 메가트렌드 수혜주 우선.
삼성전자, 애플, 구글, MS 같은 대형주는 이 섹션에 넣지 마라.
각 종목마다 빠짐없이 작성하라. 없으면 N/A:
- 종목명 및 현재가
- 시총 / PER / PSR
- 영업이익률 / 매출성장률
- RSI14 / 거래량 비율
- 왜 지금 저평가인가 (구체적 수치)
- 핵심 성장 스토리
- 트리거: 언제 주가가 움직이는가 (구체적 이벤트)
- 목표가 (6개월 / 1년 / 3년)
- 손절 트리거 (근거 기반)
- 리스크 요인
- 멀티배거 가능성 있으면 반드시 🔥 표시
- 현재가·시총·기본 데이터가 수집되지 않은 종목은 이 섹션에 넣지 마라.
- "상장 여부 확인 필요" "티커 미확인" 종목은 섹션 하단에
  [📋 모니터링 대기] 별도 항목으로만 한 줄 언급하고 분석하지 마라.
  예: "[📋 모니터링 대기] STARCLOUD — 정치인 거래 포착, 티커·상장 여부 수동 확인 필요"
- 3개 추천 종목이 모두 데이터 있는 종목으로 채워지지 않으면
  수집된 미국/국내 주식 데이터에서 조건(소형~중형, 메가트렌드 수혜)에 맞는 종목을 발굴하라.

<h2>🚀 500만원 → 1억 프로젝트 (카테고리 3)</h2>
목표: 시드 500만원으로 1억 달성.
전략: 6개월 단위 2배 복리.
1단계(~6개월): 500만원 → 1,000만원
2단계(~12개월): 1,000만원 → 2,000만원
3단계(~18개월): 2,000만원 → 4,000만원
4단계(~24개월): 4,000만원 → 1억

이 섹션은 카테고리 1, 2와 완전히 분리해서 분석하라.
기존 보유 종목은 언급하지 말고, 오직 500만원으로 새로 진입할 종목만 추천하라.
각 추천 종목마다 빠짐없이 작성하라. 없으면 N/A:
- 종목명 및 현재가
- 시총 / PER / PSR
- 영업이익률 / 매출성장률
- RSI14 / 거래량 비율
- 핵심 성장 스토리 (2~10배 가능한 이유)
- 진입가 / 6개월 목표가 / 1년 목표가
- 손절 트리거 (근거 기반)
- 투입 비중 추천 (500만원 중 몇 %)
- 멀티배거 가능성 있으면 🔥 표시
현재 단계 진단 및 이번 달 전략 포함.
리스크 시나리오: 시드 반토막 시 대응 전략.

카테고리 3 절대 규칙:
- 카테고리 1, 2에 있는 모든 종목(__RESTRICTED__)은
  이 섹션에 단 한 글자도 쓰지 마라. 언급 자체를 금지한다.
- "대체 종목" "제외하고 대신" 같은 수정 흔적을 보고서에 절대 노출하지 마라.
  처음부터 새 종목만 깔끔하게 추천하라.
- 추천 종목 선정 전 내부적으로 아래 체크리스트를 반드시 통과한 종목만 작성하라:
  ① 카테고리 1, 2 종목 아닌가? → 아니면 제외
  ② 현재가 데이터가 수집됐는가? → 없으면 제외하고 다른 종목으로 대체
  ③ 6개월 내 2배 가능성이 있는가? → 없으면 제외
  위 3가지를 모두 통과한 종목만 추천하라.
- 현재가 데이터가 수집되지 않은 종목은 종목 추천에 포함하지 마라.
  "현금 대기" "데이터 확인 후 진입" 형태로도 특정 종목명을 언급하지 마라.
  데이터 없는 종목은 그냥 "현금 X만원 대기 — VIX XX 이하 시 추가 진입"으로만 써라.
- 카테고리 3 추천 종목이 3개 미만이 되더라도 데이터 없는 종목을 채워 넣지 마라.
  차라리 2개 종목 + 현금 비중으로 구성하라.

<h2>⭐ 종합 추천 TOP 3</h2>
종합 추천 TOP 3 선정 원칙:
- 반드시 아래 조건을 모두 충족한 종목만 선정하라:
  ① 해당 종목의 포트폴리오 판단이 🟢 추가매수 또는 💎 강력매수인 종목
  ② 또는 카테고리 3·기술적분석·중장기발굴 섹션에서 신규 추천한 종목
  ③ 🟡 홀딩 판단 종목은 TOP 3에 절대 포함하지 마라.
     홀딩은 "현상 유지"이지 "지금 사야 할 이유"가 아니다.

- TOP 3 작성 순서:
  ① 기술적+펀더멘털+거시경제 종합 점수가 높은 순서로 배치
  ② 동점이면 단기 모멘텀(거래량비, RSI 방향) 우세 종목 우선
  ③ 멀티배거 가능 종목은 🔥 표시 필수

- 각 종목마다 반드시 포함할 것:
  * 추천 이유 한 줄 (포트폴리오 판단 등급과 일치해야 함)
  * 매수 전략 (분할/일괄, 구체적 가격)
  * 목표가 3단계
  * 손절 트리거
  * 리스크 한 줄 요약

<h2>⚡ 단타 추천 (오늘~1주일)</h2>
실제 기술적 데이터 기반 단기 종목 2~3개.
각 종목: 진입가 / 목표가 / 손절 트리거 / 예상 수익률 / 근거

<h2>🚫 지금 피해야 할 것들</h2>
위험 종목, 과열 섹터, 주의 이슈. 이유 포함.

<h2>💡 오늘의 액션 플랜</h2>
지금 당장 해야 할 행동 3~5가지.
반드시 구체적인 종목명, 가격, 수량 포함.
🔴 긴급 / 🟡 오늘 중 / 🔵 이번 주 중으로 우선순위 표시.

<h2>📅 이번 주 핵심 이벤트</h2>
날짜 / 이벤트 / 예상 영향 / 관련 보유 종목 포함.
- 모든 이벤트 앞에 확실성 등급을 표시하라:
  ✅ 확정 (공식 발표된 일정)
  📋 예정 (통상적 발표 주기 기반)
  ⚠️ 추정 (AI 학습 데이터 기반, 변경 가능)
- 확실성 등급 없이 날짜를 단정적으로 쓰는 것을 금지한다.

<h2>📊 전체 종목 종합 스코어카드</h2>
보유 중인 모든 종목 + 오늘 추천 종목을 하나의 HTML 테이블로 정리.
컬럼 구성:
| 종목 | 현재가 | RSI | 정배열 | 거래량비율 | 영업이익률 | 매출성장률 | PER | 판단 | 단기목표가 | 종합점수(100점) |

점수 기준:
- 기술적 점수 (RSI, MA, 거래량): 30점
- 펀더멘털 (영업이익률, 매출성장, PER): 30점
- 모멘텀 (수익률 추세): 20점
- 리스크 대비 기대수익: 20점

점수별 셀 배경색:
- 80점 이상: background-color: #d4edda (초록)
- 60~79점: background-color: #fff3cd (노랑)
- 59점 이하: background-color: #f8d7da (빨강)
- 종합점수 옆에 각 항목별 세부점수를 괄호로 표시하라.
  예: 92점 → (기술28/펀더28/모멘텀18/리스크18)
- 점수가 60점 이하인 종목은 "낮은 이유 한 줄"을 테이블 아래 각주로 추가하라.
- 보유 종목과 신규 추천 종목을 테이블 내에서 배경색으로 구분하라.
  보유: 흰색 배경 / 신규 추천: 연한 파란색(#e8f4f8) 배경

[전체 공통 원칙]
판단 표현 원칙:
- 🔴즉시매도와 💎강력매수는 절대 애매하게 표현하지 마라
- "검토해볼 만하다" "고려해볼 수 있다" 같은 모호한 표현 금지
- 매도해야 하면 "지금 당장 매도하라"
- 매수해야 하면 "지금 바로 매수하라"
- 큰 수익 기회가 보이면 근거와 함께 명확하게 표현
- 장기 보유가 필요한 종목은 "최소 X개월 보유 전략"으로 명시
- 손실 확정이 필요한 종목은 "기회비용 X원, 지금 정리하고 Y종목으로 이동 권고"로 명시

목표주가 설정 원칙:
- 획일적 -10%손절/+20%익절 금지
- 단기 스윙: 기술적 저항선 기반
- 중장기 성장주: 6개월/1년/3년 목표가 각각
- 🔥 멀티배거 후보: 매수 후 보유 전략, 중간 익절 구간, 최종 목표가 상세히
- 손절 트리거는 반드시 "근거 붕괴 조건"으로 명시 (단순 % 하락 금지)

뉴스, 기업 이벤트 등 실시간 미확인 정보는 (※AI 학습 데이터 기반) 표시.
수집된 실제 데이터(주가, RSI, MA, 거래량 등)에는 표시 붙이지 않는다.
데이터 수집 실패 항목은 보고서 내 ⚠️ 표시 후 수동 확인 안내.

보고서 출력 원칙:
- 보고서는 최종 결론만 출력한다.
- "원래 NVDA를 넣으려 했지만..." "절대 규칙 준수 차원에서..." 같은
  내부 검토 과정, 수정 흔적, 자기 수정 코멘트를 보고서에 절대 쓰지 마라.
- 독자는 AI가 고민한 과정이 아니라 최종 판단만 원한다."""

    static_system = (
        static_system
        .replace("__PORTFOLIO__", _pf_section)
        .replace("__RESTRICTED__", _restricted)
    )

    user_content = (
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
    for block in final.content:
        if block.type == "text":
            return block.text
    return ""


# ── 이메일 발송 ───────────────────────────────────────────────────────────────
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
            {report_content}
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


# ── 메인 실행 ─────────────────────────────────────────────────────────────────
def run_daily_report():
    print(f"\n{'='*50}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 보고서 생성 시작", flush=True)
    print(f"{'='*50}", flush=True)

    print(f"\n[0/9] 포트폴리오 로드", flush=True)
    portfolio_data = load_portfolio()

    print(f"\n[1/9] 환율 조회", flush=True)
    exchange_rate = get_exchange_rate()

    print(f"\n[2/9] 거시경제 지표 수집 (VIX, DXY, 금리, 원유, 금)", flush=True)
    macro_data = get_macro_data()

    print(f"\n[3/9] Fear & Greed Index 수집", flush=True)
    fear_greed = get_fear_greed()

    print(f"\n[4/9] SEC 내부자 거래 수집", flush=True)
    insider_trades = get_insider_trades()

    print(f"\n[5/9] 정치인 거래 수집", flush=True)
    congress_trades = get_congress_trades()

    print(f"\n[6/9] Put/Call Ratio 수집", flush=True)
    vix_val = (macro_data.get("VIX") or {}).get("value")
    put_call_ratio = get_put_call_ratio(vix_value=vix_val)

    print(f"\n[7/9] 미국 주식 데이터 수집 ({len(US_TICKERS)}개 종목)", flush=True)
    us_data = get_stock_data(US_TICKERS)
    print(f"  → 수집 완료: {list(us_data.keys())}", flush=True)

    print(f"\n[7/9] 국내 주식 데이터 수집 ({len(KR_TICKERS)}개 종목)", flush=True)
    kr_data = get_stock_data(KR_TICKERS)
    print(f"  → 수집 완료: {list(kr_data.keys())}", flush=True)

    print(f"\n[8/9] AI 분석 보고서 생성 (Claude Opus 4.7)", flush=True)
    report = generate_report(us_data, kr_data, exchange_rate,
                             macro_data, fear_greed, insider_trades,
                             congress_trades, put_call_ratio, portfolio_data)
    print(f"  → 보고서 생성 완료 ({len(report)}자)", flush=True)

    print(f"\n[9/9] 이메일 발송", flush=True)
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
