"""
candidate_screener.py — Dynamic market-based new candidate screener (Stage 1)
Independent module: run before report generation to surface new tradeable candidates.
"""

import os
import json
import argparse
import datetime
import sys
import time
import warnings
warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── Constants ──────────────────────────────────────────────────────────────────
OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

_EXCLUDE_FROM_NEW = {"068760.KS", "RGTI", "BEAM"}

US_WATCHLIST_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "LLY",
    "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "ABBV", "MRK", "CVX",
    "PEP", "KO", "COST", "ADBE", "CRM", "AMD", "NFLX", "TMO", "ACN", "QCOM",
    "WMT", "BAC", "ORCL", "MCD", "GE", "DHR", "IBM", "TXN", "INTU", "PM",
    "NOW", "AMGN", "LOW", "SPGI", "GS", "CAT", "ISRG", "BKNG", "AXP", "ELV",
    "PANW", "AMAT", "VRTX", "ADI", "LRCX", "CDNS", "SNPS", "REGN", "BSX", "SYK",
    "MMC", "MU", "KLAC", "PLD", "COP", "EOG", "BLK", "SCHW", "CB", "TJX",
    "SO", "DUK", "NEE", "APH", "MSI", "PYPL", "MRNA", "DXCM", "IDXX", "EW",
    "HUM", "CI", "MCK", "ABC", "CAH", "GEHC", "IQV", "A", "MTD", "WAT",
    "PLTR", "CRWD", "SNOW", "UBER", "LYFT", "ABNB", "DASH", "COIN", "HOOD", "RBLX",
]

KR_WATCHLIST_FALLBACK = [
    "005930.KS", "000660.KS", "005380.KS", "035420.KS", "000270.KS",
    "051910.KS", "035720.KS", "006400.KS", "028260.KS", "055550.KS",
    "105560.KS", "034220.KS", "068270.KS", "207940.KS", "090430.KS",
    "096770.KS", "010130.KS", "003670.KS", "323410.KS", "012330.KS",
    "011170.KS", "009150.KS", "000810.KS", "033780.KS", "003550.KS",
    "017670.KS", "030200.KS", "015760.KS", "032830.KS", "086790.KS",
    "066570.KS", "042660.KS", "010950.KS", "097950.KS", "024110.KS",
    "000100.KS", "001040.KS", "011780.KS", "018260.KS", "047050.KS",
    "298040.KS", "036570.KS", "251270.KS", "352820.KS", "035900.KS",
    "047810.KS", "009830.KS", "011790.KS", "021240.KS", "010120.KS",
]

CANDIDATE_TYPES = ["안정형", "추세형", "이벤트형", "현금대체형", "보유추가형", "고위험", "제외"]
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "EXCLUDE": 4}

_FIN_SECTOR_KEYS = ("financial", "insurance", "finance", "금융", "보험")


# ── Cache ──────────────────────────────────────────────────────────────────────

def _cache_path() -> str:
    today = datetime.date.today().strftime("%Y%m%d")
    return os.path.join(OUTPUTS_DIR, f"ticker_cache_{today}.json")


def _load_cache() -> dict:
    path = _cache_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    if not cache:
        return
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


# ── Universe Loading ───────────────────────────────────────────────────────────

def _scrape_sp500_wikipedia():
    if not REQUESTS_OK:
        return []
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", {"id": "constituents"})
        if not table:
            return []
        tickers = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if cols:
                tickers.append(cols[0].text.strip().replace(".", "-"))
        return tickers
    except Exception:
        return []


def _scrape_nasdaq100_wikipedia():
    if not REQUESTS_OK:
        return []
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tickers = []
        for table in soup.find_all("table", {"class": "wikitable"}):
            headers = [th.text.strip().lower() for th in table.find_all("th")]
            if any("ticker" in h or "symbol" in h for h in headers):
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if cols:
                        t = cols[0].text.strip().replace(".", "-")
                        if t:
                            tickers.append(t)
                if tickers:
                    break
        return tickers
    except Exception:
        return []


def load_market_universe(market: str) -> dict:
    market = market.upper()
    if market == "US":
        sp500 = _scrape_sp500_wikipedia()
        ndq100 = _scrape_nasdaq100_wikipedia()
        combined = list(dict.fromkeys(sp500 + ndq100))
        if len(combined) >= 400:
            status, source = "success", "Wikipedia S&P500+NASDAQ100 scrape"
        elif len(combined) >= 50:
            status = "partial"
            combined = list(dict.fromkeys(combined + [t for t in US_WATCHLIST_FALLBACK if t not in set(combined)]))
            source = "Wikipedia partial + fallback supplement"
        else:
            combined = list(US_WATCHLIST_FALLBACK)
            status, source = "fallback", "US_WATCHLIST_FALLBACK (scrape failed)"
        return {"market": "US", "tickers": combined, "market_scan_status": status, "source": source, "count": len(combined)}

    elif market == "KR":
        try:
            import pykrx  # noqa: F401
            from pykrx import stock
            today = datetime.date.today().strftime("%Y%m%d")
            kospi = stock.get_market_ticker_list(today, market="KOSPI")
            kosdaq = stock.get_market_ticker_list(today, market="KOSDAQ")
            tickers_kr = [f"{t}.KS" for t in kospi] + [f"{t}.KQ" for t in kosdaq]
            if len(tickers_kr) >= 100:
                return {"market": "KR", "tickers": tickers_kr, "market_scan_status": "success",
                        "source": "pykrx KOSPI+KOSDAQ", "count": len(tickers_kr)}
        except (ImportError, Exception):
            pass
        return {"market": "KR", "tickers": list(KR_WATCHLIST_FALLBACK), "market_scan_status": "fallback",
                "source": "KR_WATCHLIST_FALLBACK (pykrx unavailable)", "count": len(KR_WATCHLIST_FALLBACK)}
    else:
        raise ValueError(f"Unknown market: {market}. Use 'US' or 'KR'.")


# ── Data Collection ────────────────────────────────────────────────────────────

def _fetch_ticker_data(ticker: str, period: str = "3mo", cache: dict = None):
    cache_key = f"{ticker}:{period}"
    if cache is not None and cache_key in cache:
        return dict(cache[cache_key])
    if not YFINANCE_OK:
        return None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, timeout=10)
        if hist.empty or len(hist) < 5:
            return None
        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass
        close_series = hist["Close"]
        volume_series = hist["Volume"]
        current_price = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else current_price
        price_1d_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0.0
        price_20d_ago = float(close_series.iloc[-20]) if len(close_series) >= 20 else float(close_series.iloc[0])
        price_20d_pct = (current_price - price_20d_ago) / price_20d_ago * 100 if price_20d_ago else 0.0
        avg_vol_20d = float(volume_series.iloc[-20:].mean()) if len(volume_series) >= 20 else float(volume_series.mean())
        last_vol = float(volume_series.iloc[-1])
        vol_ratio = last_vol / avg_vol_20d if avg_vol_20d > 0 else 1.0
        ma20 = float(close_series.iloc[-20:].mean()) if len(close_series) >= 20 else current_price
        ma60 = float(close_series.iloc[-60:].mean()) if len(close_series) >= 60 else current_price
        market_cap = info.get("marketCap") or info.get("market_cap") or 0
        pe_ratio = info.get("trailingPE") or info.get("forwardPE") or None
        week52_high = info.get("fiftyTwoWeekHigh") or float(close_series.max())
        week52_low = info.get("fiftyTwoWeekLow") or float(close_series.min())
        dist_from_52w_high = (current_price - week52_high) / week52_high * 100 if week52_high else 0.0
        result = {
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "currency": info.get("currency") or "USD",
            "current_price": current_price,
            "prev_close": prev_close,
            "price_1d_pct": price_1d_pct,
            "price_20d_pct": price_20d_pct,
            "volume_last": last_vol,
            "volume_avg_20d": avg_vol_20d,
            "volume_ratio": vol_ratio,
            "ma20": ma20,
            "ma60": ma60,
            "above_ma20": current_price > ma20,
            "above_ma60": current_price > ma60,
            "market_cap": market_cap,
            "pe_ratio": pe_ratio,
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "week52_high": week52_high,
            "week52_low": week52_low,
            "dist_from_52w_high_pct": dist_from_52w_high,
            "data_source": "yfinance",
            "data_date": hist.index[-1].strftime("%Y-%m-%d"),
        }
        if cache is not None:
            cache[cache_key] = result
        return result
    except Exception:
        return None


# ── Raw Candidate Validation ───────────────────────────────────────────────────

def validate_raw_candidate_data(candidate: dict) -> tuple[str, list[str]]:
    issues = []
    cp = candidate.get("current_price")
    if cp is None or cp <= 0:
        return "fail", ["current_price 없음 또는 0 이하 — 데이터 취득 실패"]

    stop = candidate.get("stop_price")
    target = candidate.get("target_price")
    if stop is not None and stop >= cp:
        issues.append(f"손절가({stop:.2f}) >= 현재가({cp:.2f}) — 손절 설정 오류")
        return "fail", issues
    if target is not None and target <= cp:
        issues.append(f"목표가({target:.2f}) <= 현재가({cp:.2f}) — 목표 설정 오류")
        return "fail", issues

    price_1d = candidate.get("price_1d_pct", 0.0)
    if abs(price_1d) >= 30:
        issues.append(f"1일 등락 ±30% 초과({price_1d:.1f}%) — 스플릿/이벤트 의심")
        return "fail", issues

    rr = candidate.get("risk_reward_ratio")
    if rr is not None:
        if rr < 1.0:
            issues.append(f"RR={rr:.2f} < 1.0 — TOP 제외")
            return "fail", issues
        elif rr < 1.5:
            issues.append(f"RR={rr:.2f} < 1.5 — 위험보상비율 낮음")

    model_acc = candidate.get("model_accuracy")
    if model_acc is not None and model_acc < 30:
        issues.append(f"model_accuracy={model_acc}% < 30% — A/B 등급 불가")

    sources = candidate.get("data_sources", [])
    if isinstance(sources, list) and len(sources) <= 1:
        issues.append("단일 데이터소스 — A/B 등급 불가")

    earnings = candidate.get("earnings_estimate", None)
    if earnings is not None and earnings > 50e12:
        issues.append("추정 실적 50조+ 이상치 의심 — 검증 필요")
        return "fail", issues

    if issues:
        grade_ban = any("A/B" in i for i in issues)
        if grade_ban and len(issues) >= 2:
            return "verify", issues
        return "warning", issues
    return "pass", []


# ── Candidate Scoring ──────────────────────────────────────────────────────────

def _score_data_quality(candidate: dict) -> int:
    score = 0
    if candidate.get("current_price", 0) > 0:
        score += 5
    if candidate.get("volume_avg_20d", 0) > 0:
        score += 3
    if candidate.get("market_cap", 0) > 0:
        score += 3
    if candidate.get("pe_ratio") is not None:
        score += 2
    validation_verdict = candidate.get("_validation_verdict", "pass")
    if validation_verdict == "pass":
        score += 2
    elif validation_verdict == "warning":
        score += 1
    return min(score, 15)


def _score_trend(candidate: dict) -> int:
    score = 0
    p20 = candidate.get("price_20d_pct", 0.0)
    above_ma20 = candidate.get("above_ma20", False)
    above_ma60 = candidate.get("above_ma60", False)
    d52h = candidate.get("dist_from_52w_high_pct", 0.0)
    if p20 > 10:
        score += 8
    elif p20 > 5:
        score += 5
    elif p20 > 0:
        score += 2
    elif p20 < -10:
        score -= 3
    if above_ma20:
        score += 5
    if above_ma60:
        score += 4
    if d52h > -5:
        score += 3
    elif d52h < -30:
        score -= 2
    return max(0, min(score, 20))


def _score_volume(candidate: dict) -> int:
    score = 0
    vr = candidate.get("volume_ratio", 1.0)
    avg_vol = candidate.get("volume_avg_20d", 0)
    if vr >= 2.0:
        score += 8
    elif vr >= 1.5:
        score += 5
    elif vr >= 1.2:
        score += 3
    elif vr < 0.5:
        score -= 3
    market = candidate.get("market", "US")
    if market == "US":
        if avg_vol >= 1_000_000:
            score += 5
        elif avg_vol >= 200_000:
            score += 2
    else:
        if avg_vol >= 100_000:
            score += 5
        elif avg_vol >= 20_000:
            score += 2
    return max(0, min(score, 15))


def _score_news_sentiment(candidate: dict) -> int:
    news_score = candidate.get("news_sentiment_score")
    if news_score is None:
        return 5  # 뉴스 미확인 기본값 — 차별화 왜곡 방지
    return max(0, min(int(news_score), 20))


def _score_fundamental(candidate: dict) -> int:
    score = 5
    pe = candidate.get("pe_ratio")
    market_cap = candidate.get("market_cap", 0)
    market = candidate.get("market", "US")
    if pe is not None:
        if 5 < pe < 20:
            score += 3
        elif 20 <= pe < 40:
            score += 1
        elif pe > 80 or pe < 0:
            score -= 2
    if market == "US" and market_cap >= 10e9:
        score += 2
    elif market == "KR" and market_cap >= 1e12:
        score += 2
    return max(0, min(score, 10))


def _score_risk(candidate: dict) -> int:
    score = 5
    p1d = candidate.get("price_1d_pct", 0.0)
    rr = candidate.get("risk_reward_ratio")
    validation_verdict = candidate.get("_validation_verdict", "pass")

    # Sharp drop penalty (directional, not absolute)
    if p1d <= -10:
        score -= 8
    elif p1d <= -8:
        score -= 6
    elif p1d <= -5:
        score -= 4
    elif abs(p1d) > 20:
        score -= 4
    elif abs(p1d) > 10:
        score -= 2

    if rr is not None:
        if rr >= 2.5:
            score += 3
        elif rr >= 2.0:
            score += 2
        elif rr >= 1.5:
            score += 1
        elif rr >= 1.0:
            score -= 3  # 1.0~1.5: strong penalty
        else:
            score -= 5  # < 1.0: extreme penalty

    if validation_verdict == "fail":
        score -= 5
    elif validation_verdict == "verify":
        score -= 2
    elif validation_verdict == "warning":
        score -= 1

    return max(0, min(score, 10))


def _score_portfolio_fit(candidate: dict) -> int:
    score = 5
    ticker = candidate.get("ticker", "")
    if ticker in _EXCLUDE_FROM_NEW:
        return 0
    candidate_type = candidate.get("candidate_type", "")
    if candidate_type in ("추세형", "안정형"):
        score += 3
    elif candidate_type == "이벤트형":
        score += 1
    elif candidate_type in ("고위험", "제외"):
        score -= 3
    if candidate.get("dynamic_pool", False):
        score += 2
    return max(0, min(score, 10))


def score_candidate(candidate: dict) -> dict:
    candidate = dict(candidate)

    # Mark missing news
    candidate["_news_missing"] = candidate.get("news_sentiment_score") is None

    s_data = _score_data_quality(candidate)
    s_trend = _score_trend(candidate)
    s_volume = _score_volume(candidate)
    s_news = _score_news_sentiment(candidate)
    s_fundamental = _score_fundamental(candidate)
    s_risk = _score_risk(candidate)
    s_portfolio = _score_portfolio_fit(candidate)

    total = s_data + s_trend + s_volume + s_news + s_fundamental + s_risk + s_portfolio

    candidate["score_breakdown"] = {
        "data": s_data, "trend": s_trend, "volume": s_volume, "news": s_news,
        "fundamental": s_fundamental, "risk": s_risk, "portfolio_fit": s_portfolio,
    }
    candidate["total_score"] = total

    # Grade derivation
    model_acc = candidate.get("model_accuracy")
    validation_verdict = candidate.get("_validation_verdict", "pass")
    sources = candidate.get("data_sources", [])
    single_source = isinstance(sources, list) and len(sources) <= 1
    grade_ban_ab = (model_acc is not None and model_acc < 30) or single_source
    ticker = candidate.get("ticker", "")
    rr = candidate.get("risk_reward_ratio")

    # Hard RR < 1.0 exclusion
    if rr is not None and rr < 1.0:
        candidate["grade"] = "EXCLUDE"
        candidate["_rr_excluded"] = True
        candidate["candidate_type"] = candidate.get("candidate_type") or "제외"
        return candidate

    if ticker in _EXCLUDE_FROM_NEW or validation_verdict == "fail":
        grade = "EXCLUDE"
    elif total >= 70 and not grade_ban_ab:
        grade = "A"
    elif total >= 55 and not grade_ban_ab:
        grade = "B"
    elif total >= 40:
        grade = "C"
    elif total >= 25:
        grade = "D"
    else:
        grade = "EXCLUDE"

    # RR 1.0~1.5: cap at C
    if rr is not None and rr < 1.5 and grade in ("A", "B"):
        grade = "C"

    # entry_conditions_met grade cap (< 3 conditions → max C)
    cond_met = candidate.get("entry_conditions_met", 4)
    if cond_met < 3 and grade in ("A", "B"):
        grade = "C"

    # Derive candidate_type if not set
    if not candidate.get("candidate_type"):
        p20 = candidate.get("price_20d_pct", 0.0)
        above_ma20 = candidate.get("above_ma20", False)
        vr = candidate.get("volume_ratio", 1.0)
        if grade == "EXCLUDE":
            candidate["candidate_type"] = "제외"
        elif p20 > 10 and above_ma20 and vr > 1.3:
            candidate["candidate_type"] = "추세형"
        elif above_ma20 and model_acc is not None and model_acc >= 50:
            candidate["candidate_type"] = "안정형"
        elif vr > 2.0:
            candidate["candidate_type"] = "이벤트형"
        elif total < 35:
            candidate["candidate_type"] = "고위험"
        else:
            candidate["candidate_type"] = "추세형"

    # Sharp drop override (applied after type derivation)
    p1d_actual = candidate.get("price_1d_pct", 0.0)
    if p1d_actual <= -10:
        if grade in ("A", "B", "C"):
            grade = "D"
        candidate["candidate_type"] = "고위험"
        candidate["_sharp_drop_flag"] = f"1일 {p1d_actual:.1f}% 급락 — 반등 확인 전 진입 금지"
    elif p1d_actual <= -8:
        if grade in ("A", "B", "C"):
            grade = "D"
        candidate["candidate_type"] = "이벤트형"
        candidate["_sharp_drop_flag"] = f"1일 {p1d_actual:.1f}% 급락 — C 하단/확인 필요"
    elif p1d_actual <= -5:
        if candidate.get("candidate_type") == "추세형":
            candidate["candidate_type"] = "이벤트형"
        candidate["_sharp_drop_flag"] = f"당일 {p1d_actual:.1f}% 급락 — 반등 확인 전 진입 금지"

    candidate["grade"] = grade
    return candidate


# ── Trade Plan Builder ─────────────────────────────────────────────────────────

def build_trade_plan(candidate: dict) -> dict:
    cp = candidate.get("current_price", 0)
    ma20 = candidate.get("ma20", cp)
    vr = candidate.get("volume_ratio", 1.0)
    above_ma20 = candidate.get("above_ma20", False)

    if cp <= 0:
        return candidate

    stop_candidate = min(cp * 0.95, ma20 * 0.97)
    stop = candidate.get("stop_price") or round(stop_candidate, 4)

    p20 = candidate.get("price_20d_pct", 0.0)
    if p20 > 10:
        target_mult = 1.15
    elif p20 > 0:
        target_mult = 1.10
    else:
        target_mult = 1.07
    target = candidate.get("target_price") or round(cp * target_mult, 4)

    risk = cp - stop if stop < cp else cp * 0.05
    reward = target - cp if target > cp else cp * 0.10
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    price_cond = f"현재가 {cp:.2f} 돌파 후 지지 확인"

    # Volume condition: text matches actual data
    if vr >= 1.3:
        volume_cond = f"거래량 1.3x 이상 충족 (현재 {vr:.1f}x)"
        volume_met = True
    elif vr >= 1.0:
        volume_cond = f"거래량 보통, 추가 확인 필요 (현재 {vr:.1f}x)"
        volume_met = False
    else:
        volume_cond = f"거래량 미확인/부족 — 진입 보류 (현재 {vr:.1f}x)"
        volume_met = False

    technical_cond = f"MA20({ma20:.2f}) 위 유지" if above_ma20 else f"MA20({ma20:.2f}) 돌파 확인"
    technical_met = above_ma20

    news_cond = "실적/뉴스 촉매 또는 섹터 모멘텀 확인"
    news_met = candidate.get("news_sentiment_score") is not None

    # Count met entry conditions (price always met)
    entry_conditions_met = 1 + int(volume_met) + int(technical_met) + int(news_met)

    trade_plan = {
        "entry_price": round(cp, 4),
        "stop_price": round(stop, 4),
        "target_price": round(target, 4),
        "risk_reward_ratio": rr,
        "entry_conditions": {
            "price": price_cond,
            "volume": volume_cond,
            "technical": technical_cond,
            "news_event": news_cond,
        },
        "entry_conditions_met": entry_conditions_met,
    }

    candidate["trade_plan"] = trade_plan
    candidate["stop_price"] = round(stop, 4)
    candidate["target_price"] = round(target, 4)
    candidate["risk_reward_ratio"] = rr
    candidate["entry_conditions_met"] = entry_conditions_met

    return candidate


# ── Dynamic Pool Builder ───────────────────────────────────────────────────────

def build_dynamic_candidate_pool(
    market_universe: dict,
    max_pool: int = 300,
    cache: dict = None,
    deadline: float = None,
) -> tuple:
    tickers = market_universe["tickers"][:max_pool]
    market = market_universe["market"]
    scan_status = market_universe["market_scan_status"]
    dynamic_pool_flag = scan_status == "success"

    candidates = []
    timeout_count = 0
    cache_hits = 0
    print(f"  [{market}] Fetching {len(tickers)} tickers (scan_status={scan_status})...", flush=True)

    for i, ticker in enumerate(tickers):
        if deadline is not None and time.time() > deadline:
            remaining = len(tickers) - i
            timeout_count += remaining
            print(f"    [TIMEOUT] 시간 초과 — 남은 {remaining}개 ticker 생략", flush=True)
            break

        if ticker in _EXCLUDE_FROM_NEW:
            continue

        cache_key = f"{ticker}:3mo"
        was_cached = cache is not None and cache_key in cache

        data = _fetch_ticker_data(ticker, cache=cache)
        if data is None:
            continue

        if was_cached:
            cache_hits += 1

        data["market"] = market
        data["dynamic_pool"] = dynamic_pool_flag

        avg_vol = data.get("volume_avg_20d", 0)
        cp = data.get("current_price", 0)
        if market == "US" and avg_vol < 50_000:
            continue
        if market == "KR" and avg_vol < 5_000:
            continue
        if cp <= 0:
            continue

        verdict, issues = validate_raw_candidate_data(data)
        data["_validation_verdict"] = verdict
        data["_validation_issues"] = issues

        if verdict == "fail":
            continue

        candidates.append(data)

        if (i + 1) % 50 == 0:
            elapsed_msg = ""
            if deadline is not None:
                remaining_s = max(0, deadline - time.time())
                elapsed_msg = f", {remaining_s:.0f}s remaining"
            print(f"    ... {i+1}/{len(tickers)} processed, {len(candidates)} candidates so far{elapsed_msg}", flush=True)

        if not was_cached:
            time.sleep(0.05)

    return candidates, timeout_count, cache_hits


# ── Top Candidate Selector ─────────────────────────────────────────────────────

def _is_fin_sector(sector: str) -> bool:
    sk = (sector or "").lower().strip()
    return any(fk in sk for fk in _FIN_SECTOR_KEYS)


def _top_sort_key(c: dict) -> tuple:
    """Quality-first sort: lower tuple = higher priority."""
    rr = c.get("risk_reward_ratio") or 0
    vr = c.get("volume_ratio") or 0
    p1d = c.get("price_1d_pct", 0.0)
    ctype = c.get("candidate_type", "")
    verdict = c.get("_validation_verdict", "pass")
    k1 = 0 if verdict == "pass" else (1 if verdict == "warning" else 2)
    k2 = 0 if rr >= 1.5 else 1
    k3 = 0 if vr >= 1.3 else 1
    k4 = 0 if p1d > -5 else 1
    k5 = 0 if ctype in ("추세형", "이벤트형") else 1
    k6 = GRADE_ORDER.get(c.get("grade", "D"), 3)
    k7 = -c.get("total_score", 0)
    return (k1, k2, k3, k4, k5, k6, k7)


def select_top_candidates(
    scored_candidates: list,
    max_candidates: int = 10,
    quality_stats: dict = None,
) -> list:
    """
    Select top candidates with quality-first ordering and composition rules.
    Two-pass: pass1 fills positions 1-5 (top5 sector cap ≤2), pass2 fills 6-max (global cap ≤3).
    """
    eligible = [
        c for c in scored_candidates
        if c.get("grade") not in ("EXCLUDE",)
        and c.get("_validation_verdict") != "fail"
        and c.get("ticker") not in _EXCLUDE_FROM_NEW
        and (c.get("risk_reward_ratio") is None or c.get("risk_reward_ratio") >= 1.0)
    ]
    eligible.sort(key=_top_sort_key)

    top = []
    added_tickers = set()
    sector_counts = {}    # global sector counts
    sector_top5 = {}      # sector counts within top-5 positions
    fin_count = 0
    type_counts = {"추세형": 0, "안정형": 0, "보유추가형": 0}
    dynamic_count = 0
    sector_skipped = set()  # tickers skipped due to sector (may be recovered in pass2)

    def _accept(c):
        nonlocal fin_count, dynamic_count
        top.append(c)
        added_tickers.add(c["ticker"])
        sector_skipped.discard(c["ticker"])
        sk = (c.get("sector") or "").lower().strip()
        if sk:
            sector_counts[sk] = sector_counts.get(sk, 0) + 1
            if len(top) <= 5:
                sector_top5[sk] = sector_top5.get(sk, 0) + 1
        if _is_fin_sector(c.get("sector")):
            fin_count += 1
        ctype = c.get("candidate_type", "")
        if ctype in type_counts:
            type_counts[ctype] += 1
        if c.get("dynamic_pool"):
            dynamic_count += 1

    def _sector_ok(c, in_top5: bool) -> bool:
        sk = (c.get("sector") or "").lower().strip()
        if sk:
            if in_top5 and sector_top5.get(sk, 0) >= 2:
                return False
            if sector_counts.get(sk, 0) >= 3:
                return False
        if _is_fin_sector(c.get("sector")) and fin_count >= 3:
            return False
        return True

    def _type_ok(c) -> bool:
        ctype = c.get("candidate_type", "")
        if ctype == "안정형" and type_counts["안정형"] >= 2:
            return False
        if ctype == "보유추가형" and type_counts["보유추가형"] >= 3:
            return False
        return True

    # Pass 1: fill positions 1–5 (strict sector cap)
    for c in eligible:
        if len(top) >= min(5, max_candidates):
            break
        if c["ticker"] in added_tickers:
            continue
        if not _type_ok(c):
            continue
        if not _sector_ok(c, in_top5=True):
            sector_skipped.add(c["ticker"])
            continue
        _accept(c)

    # Pass 2: fill positions 6–max_candidates (global sector cap only)
    for c in eligible:
        if len(top) >= max_candidates:
            break
        if c["ticker"] in added_tickers:
            continue
        if not _type_ok(c):
            continue
        if not _sector_ok(c, in_top5=False):
            sector_skipped.add(c["ticker"])
            continue
        _accept(c)

    sector_cap_applied = len(sector_skipped)

    # Composition warnings
    warnings_out = []
    trend_count = sum(1 for c in top if c.get("candidate_type") == "추세형")
    if trend_count < 2:
        warnings_out.append(f"추세형 후보 {trend_count}개 < 2개 권장")
    if dynamic_count < 2:
        warnings_out.append(f"dynamic_pool 후보 {dynamic_count}개 < 2개 권장")

    # Defensive/sector-only warning
    fin_in_top = sum(1 for c in top if _is_fin_sector(c.get("sector")))
    if len(top) > 0 and fin_in_top == len(top):
        warnings_out.append("안정/방어 섹터만으로 TOP 구성 — 다양성 재검토 필요")

    if warnings_out:
        print("  [WARNING] TOP 선정 구성 경고:", flush=True)
        for w in warnings_out:
            print(f"    - {w}", flush=True)

    if quality_stats is not None:
        quality_stats["sector_cap_applied"] = sector_cap_applied

    return top


# ── JSON Output ────────────────────────────────────────────────────────────────

def save_top_candidates_json(top_candidates: list[dict], market_scan_status: str, meta: dict = None) -> str:
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    out_path = os.path.join(OUTPUTS_DIR, f"top_candidates_{today}.json")
    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "date": today,
        "market_scan_status": market_scan_status,
        "candidate_count": len(top_candidates),
        "meta": meta or {},
        "top_candidates": top_candidates,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


# ── Main Screening Flow ────────────────────────────────────────────────────────

def run_screening(
    markets: list[str] = None,
    max_pool: int = 300,
    dry_run: bool = False,
    max_us_tickers: int = None,
    max_kr_tickers: int = None,
    timeout_seconds: int = 480,
    use_cache: bool = True,
) -> dict:
    if markets is None:
        markets = ["US", "KR"]

    start_time = time.time()
    deadline = (start_time + timeout_seconds) if timeout_seconds > 0 else None
    _us_max = max_us_tickers if max_us_tickers is not None else max_pool
    _kr_max = max_kr_tickers if max_kr_tickers is not None else max_pool

    cache = _load_cache() if use_cache else {}
    initial_cache_size = len(cache)

    all_candidates = []
    combined_scan_status = "success"
    total_timeout_count = 0
    total_cache_hits = 0
    meta = {"markets": markets, "universes": {}}

    for market in markets:
        print(f"\n[SCAN] Loading {market} universe...", flush=True)
        universe = load_market_universe(market)
        meta["universes"][market] = {
            "source": universe["source"],
            "count": universe["count"],
            "market_scan_status": universe["market_scan_status"],
        }

        if universe["market_scan_status"] == "fallback":
            if combined_scan_status == "success":
                combined_scan_status = "partial"
            if len(markets) == 1:
                combined_scan_status = "fallback"
        elif universe["market_scan_status"] == "partial" and combined_scan_status == "success":
            combined_scan_status = "partial"

        per_market_max = _us_max if market == "US" else _kr_max
        print(f"  Universe: {universe['count']} tickers (status={universe['market_scan_status']}, limit={per_market_max})", flush=True)

        if not dry_run:
            raw_pool, tc, ch = build_dynamic_candidate_pool(
                universe, max_pool=per_market_max, cache=cache, deadline=deadline,
            )
            total_timeout_count += tc
            total_cache_hits += ch
            print(f"  [SCORE] Scoring {len(raw_pool)} raw candidates...", flush=True)
            scored = [score_candidate(build_trade_plan(c)) for c in raw_pool]
            all_candidates.extend(scored)
        else:
            print(f"  [DRY RUN] Skipping data fetch.", flush=True)

    if use_cache and not dry_run:
        _save_cache(cache)

    all_fallback = all(
        meta["universes"][m]["market_scan_status"] == "fallback"
        for m in markets if m in meta["universes"]
    )
    if all_fallback:
        combined_scan_status = "fallback"
        print("\n  [WARNING] 전체 시장: fallback watchlist만 사용됨 — 동적 스캔 실패", flush=True)

    print(f"\n[SELECT] Total scored candidates: {len(all_candidates)}", flush=True)

    quality_stats = {}
    top = select_top_candidates(all_candidates, max_candidates=10, quality_stats=quality_stats)
    print(f"[SELECT] TOP {len(top)} candidates selected.", flush=True)

    if len(top) == 0:
        print("  [WARNING] top_candidates 비어있음 — 스크리닝 조건 재검토 필요", flush=True)

    elapsed = round(time.time() - start_time, 1)

    quality_check = {
        "rr_below_1_excluded": sum(1 for c in all_candidates if c.get("_rr_excluded")),
        "volume_condition_mismatch": sum(1 for c in top if (c.get("volume_ratio") or 0) < 1.3),
        "sharp_drop_excluded": sum(1 for c in all_candidates
                                   if c.get("_sharp_drop_flag") and c.get("grade") in ("D", "EXCLUDE")),
        "sector_cap_applied": quality_stats.get("sector_cap_applied", 0),
        "news_missing_count": sum(1 for c in top if c.get("_news_missing")),
        "top_dynamic_count": sum(1 for c in top if c.get("dynamic_pool")),
        "top_fallback_count": sum(1 for c in top if not c.get("dynamic_pool")),
    }

    meta["elapsed_seconds"] = elapsed
    meta["timeout_count"] = total_timeout_count
    meta["cache_used"] = total_cache_hits
    meta["cache_new_entries"] = len(cache) - initial_cache_size
    meta["quality_check"] = quality_check

    if not dry_run:
        out_path = save_top_candidates_json(top, combined_scan_status, meta)
        print(f"\n[SAVE] {out_path}", flush=True)
    else:
        out_path = None

    return {
        "top_candidates": top,
        "market_scan_status": combined_scan_status,
        "meta": meta,
        "output_path": out_path,
        "elapsed_seconds": elapsed,
        "timeout_count": total_timeout_count,
        "cache_used": total_cache_hits,
        "quality_check": quality_check,
    }


# ── Mock Tests ────────────────────────────────────────────────────────────────

def _run_mock_tests() -> bool:
    results = []

    def check(name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    print("\n=== candidate_screener.py mock tests ===\n")

    # T1
    try:
        check("T1: load_market_universe 함수 존재", callable(load_market_universe))
    except Exception as e:
        check("T1: load_market_universe 함수 존재", False, str(e))

    # T2
    try:
        check("T2: build_dynamic_candidate_pool 함수 존재", callable(build_dynamic_candidate_pool))
    except Exception as e:
        check("T2: build_dynamic_candidate_pool 함수 존재", False, str(e))

    # T3
    try:
        ok = (isinstance(US_WATCHLIST_FALLBACK, list) and len(US_WATCHLIST_FALLBACK) > 0
              and isinstance(KR_WATCHLIST_FALLBACK, list) and len(KR_WATCHLIST_FALLBACK) > 0)
        check("T3: WATCHLIST_FALLBACK 이름 및 내용 확인", ok,
              f"US={len(US_WATCHLIST_FALLBACK)}, KR={len(KR_WATCHLIST_FALLBACK)}")
    except Exception as e:
        check("T3: WATCHLIST_FALLBACK 이름 및 내용 확인", False, str(e))

    # T4
    try:
        c = {"ticker": "TEST", "name": "Test", "price_1d_pct": 0.0}
        verdict, issues = validate_raw_candidate_data(c)
        check("T4: current_price 없는 후보 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T4: current_price 없는 후보 → fail", False, str(e))

    # T5
    try:
        c = {"ticker": "TEST", "name": "Test", "current_price": 100.0,
             "price_1d_pct": 1.0, "model_accuracy": 18,
             "data_sources": ["yfinance", "news"], "_validation_verdict": "pass"}
        scored = score_candidate(c)
        check("T5: model_accuracy=18 → A/B 불가", scored["grade"] not in ("A", "B"),
              f"grade={scored['grade']}")
    except Exception as e:
        check("T5: model_accuracy=18 → A/B 불가", False, str(e))

    # T6
    try:
        c = {"ticker": "SAFE", "name": "Safe", "current_price": 100.0,
             "price_1d_pct": 2.0, "price_20d_pct": 8.0,
             "volume_ratio": 1.5, "volume_avg_20d": 500_000,
             "above_ma20": True, "above_ma60": True, "ma20": 95.0, "ma60": 90.0,
             "market_cap": 50e9, "pe_ratio": 15.0, "dist_from_52w_high_pct": -3.0,
             "model_accuracy": 18, "market": "US",
             "data_sources": ["yfinance", "news"], "_validation_verdict": "pass"}
        scored = score_candidate(c)
        check("T6: model_accuracy=18이어도 C 가능", scored["grade"] in ("C", "D"),
              f"grade={scored['grade']}, score={scored['total_score']}")
    except Exception as e:
        check("T6: model_accuracy=18이어도 C 가능", False, str(e))

    # T7
    try:
        c = {"ticker": "TEST", "name": "Test", "current_price": 100.0,
             "stop_price": 105.0, "price_1d_pct": 1.0}
        verdict, issues = validate_raw_candidate_data(c)
        check("T7: 손절가 >= 현재가 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T7: 손절가 >= 현재가 → fail", False, str(e))

    # T8
    try:
        c = {"ticker": "TEST", "name": "Test", "current_price": 100.0,
             "target_price": 90.0, "price_1d_pct": 1.0}
        verdict, issues = validate_raw_candidate_data(c)
        check("T8: 목표가 <= 현재가 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T8: 목표가 <= 현재가 → fail", False, str(e))

    # T9
    try:
        c = {"ticker": "MOON", "name": "Moon", "current_price": 130.0, "price_1d_pct": 35.0}
        verdict, issues = validate_raw_candidate_data(c)
        check("T9: 1일 +30% 이상 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T9: 1일 +30% 이상 → fail", False, str(e))

    # T10
    try:
        c = {"ticker": "SOLO", "name": "Solo", "current_price": 100.0,
             "price_1d_pct": 1.0, "data_sources": ["yfinance"], "_validation_verdict": "warning"}
        verdict, issues = validate_raw_candidate_data(c)
        single_source_blocked = any("A/B" in i for i in issues)
        c2 = dict(c)
        c2.update({"price_20d_pct": 15.0, "above_ma20": True, "above_ma60": True,
                   "volume_ratio": 2.5, "volume_avg_20d": 1_000_000, "market_cap": 100e9,
                   "pe_ratio": 12.0, "dist_from_52w_high_pct": -1.0, "market": "US",
                   "ma20": 95.0, "ma60": 90.0, "_validation_verdict": "warning"})
        scored = score_candidate(c2)
        check("T10: 단일소스 → A/B 불가", scored["grade"] not in ("A", "B"),
              f"grade={scored['grade']}, single_source_blocked={single_source_blocked}")
    except Exception as e:
        check("T10: 단일소스 → A/B 불가", False, str(e))

    # T11
    try:
        fail_c = {"ticker": "FAIL1", "name": "Fail", "current_price": 100.0,
                  "grade": "B", "total_score": 75, "_validation_verdict": "fail",
                  "candidate_type": "추세형", "dynamic_pool": True, "score_breakdown": {}}
        ok_c = {"ticker": "OK1", "name": "OK", "current_price": 100.0,
                "grade": "B", "total_score": 60, "_validation_verdict": "pass",
                "candidate_type": "추세형", "dynamic_pool": True, "score_breakdown": {}}
        top = select_top_candidates([fail_c, ok_c], max_candidates=10)
        fail_in_top = any(c["ticker"] == "FAIL1" for c in top)
        check("T11: TOP에 validation fail 후보 없음", not fail_in_top,
              f"fail_in_top={fail_in_top}, top_count={len(top)}")
    except Exception as e:
        check("T11: TOP에 validation fail 없음", False, str(e))

    # T12
    try:
        kr_universe = load_market_universe("KR")
        is_fallback = kr_universe["market_scan_status"] == "fallback"
        check("T12: KR pykrx 없을 때 fallback status", is_fallback,
              f"status={kr_universe['market_scan_status']}")
    except Exception as e:
        check("T12: KR fallback status", False, str(e))

    # T13
    try:
        test_candidates = [{"ticker": "AAPL", "name": "Apple", "current_price": 200.0,
                            "grade": "A", "total_score": 80, "candidate_type": "추세형",
                            "_validation_verdict": "pass", "dynamic_pool": True}]
        out_path = save_top_candidates_json(test_candidates, "success", {"test": True})
        file_exists = os.path.exists(out_path)
        if file_exists:
            with open(out_path) as f:
                data = json.load(f)
            valid_json = "top_candidates" in data and len(data["top_candidates"]) == 1
        else:
            valid_json = False
        check("T13: JSON 파일 생성 가능", file_exists and valid_json, f"path={out_path}")
    except Exception as e:
        check("T13: JSON 파일 생성 가능", False, str(e))

    # T14
    try:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            top_empty = select_top_candidates([], max_candidates=10)
        check("T14: top_candidates 빈 입력 → 빈 결과", len(top_empty) == 0,
              f"top_count={len(top_empty)}")
    except Exception as e:
        check("T14: top_candidates 비어있으면 경고", False, str(e))

    # T15
    try:
        c = {"ticker": "NVDA", "name": "Nvidia", "current_price": 500.0,
             "ma20": 480.0, "ma60": 450.0, "above_ma20": True,
             "volume_ratio": 1.8, "price_1d_pct": 2.0}
        result = build_trade_plan(c)
        entry_conds = result.get("trade_plan", {}).get("entry_conditions", {})
        required_keys = {"price", "volume", "technical", "news_event"}
        has_all = required_keys.issubset(set(entry_conds.keys()))
        all_nonempty = all(bool(entry_conds.get(k)) for k in required_keys)
        check("T15: 진입 조건 4개(price/volume/technical/news_event) 모두 포함",
              has_all and all_nonempty, f"keys={list(entry_conds.keys())}")
    except Exception as e:
        check("T15: 진입 조건 4개 포함", False, str(e))

    # ── New quality filter tests ───────────────────────────────────────────────

    # T16: RR 0.76 → TOP 제외
    try:
        low_rr = {"ticker": "LOWRR", "name": "LowRR", "current_price": 100.0,
                  "grade": "C", "total_score": 65, "_validation_verdict": "pass",
                  "candidate_type": "추세형", "dynamic_pool": True,
                  "risk_reward_ratio": 0.76, "score_breakdown": {}}
        good = {"ticker": "GOOD", "name": "Good", "current_price": 100.0,
                "grade": "C", "total_score": 60, "_validation_verdict": "pass",
                "candidate_type": "추세형", "dynamic_pool": True,
                "risk_reward_ratio": 2.0, "score_breakdown": {}}
        top = select_top_candidates([low_rr, good], max_candidates=10)
        lowrr_in_top = any(c["ticker"] == "LOWRR" for c in top)
        check("T16: RR 0.76 → TOP 제외", not lowrr_in_top,
              f"lowrr_in_top={lowrr_in_top}, top_count={len(top)}")
    except Exception as e:
        check("T16: RR 0.76 → TOP 제외", False, str(e))

    # T17: RR 1.2 → C 이하만 가능
    try:
        c = {"ticker": "RR12", "name": "RR12", "current_price": 100.0,
             "price_1d_pct": 1.0, "price_20d_pct": 20.0,
             "volume_ratio": 2.5, "volume_avg_20d": 5_000_000,
             "above_ma20": True, "above_ma60": True, "ma20": 95.0, "ma60": 90.0,
             "market_cap": 500e9, "pe_ratio": 12.0, "dist_from_52w_high_pct": -1.0,
             "market": "US", "data_sources": ["yfinance", "news", "fundamental"],
             "_validation_verdict": "pass", "risk_reward_ratio": 1.2,
             "news_sentiment_score": 15,
             "stop_price": 93.0, "target_price": 115.2}
        scored = score_candidate(c)
        check("T17: RR 1.2 → C 이하만 가능", scored["grade"] in ("C", "D", "EXCLUDE"),
              f"grade={scored['grade']}, score={scored['total_score']}")
    except Exception as e:
        check("T17: RR 1.2 → C 이하만 가능", False, str(e))

    # T18: RR 1.6 → 정상 후보 가능 (A 가능)
    try:
        c = {"ticker": "RR16", "name": "RR16", "current_price": 100.0,
             "price_1d_pct": 1.0, "price_20d_pct": 20.0,
             "volume_ratio": 2.5, "volume_avg_20d": 5_000_000,
             "above_ma20": True, "above_ma60": True, "ma20": 95.0, "ma60": 90.0,
             "market_cap": 500e9, "pe_ratio": 12.0, "dist_from_52w_high_pct": -1.0,
             "market": "US", "data_sources": ["yfinance", "news", "fundamental"],
             "_validation_verdict": "pass", "risk_reward_ratio": 1.6,
             "news_sentiment_score": 15,
             "stop_price": 93.0, "target_price": 111.2}
        scored = score_candidate(c)
        check("T18: RR 1.6 → 정상 후보 가능 (A/B/C)", scored["grade"] in ("A", "B", "C"),
              f"grade={scored['grade']}, score={scored['total_score']}")
    except Exception as e:
        check("T18: RR 1.6 → 정상 후보 가능", False, str(e))

    # T19: volume_ratio=0.83 → "1.3x 이상" 문구 사용 안함
    try:
        c = {"ticker": "LOWV", "current_price": 100.0, "ma20": 95.0,
             "above_ma20": True, "volume_ratio": 0.83, "price_20d_pct": 5.0}
        result = build_trade_plan(c)
        vcond = result["trade_plan"]["entry_conditions"]["volume"]
        no_mismatch = "1.3x 이상" not in vcond and "1.3배 이상" not in vcond
        check("T19: volume_ratio=0.83 → '1.3x 이상' 문구 없음", no_mismatch, f"volume_cond={vcond}")
    except Exception as e:
        check("T19: volume_ratio=0.83 → 문구 불일치 없음", False, str(e))

    # T20: volume_ratio=1.37 → "1.3x 이상" 포함
    try:
        c = {"ticker": "HIGHV", "current_price": 100.0, "ma20": 95.0,
             "above_ma20": True, "volume_ratio": 1.37, "price_20d_pct": 5.0}
        result = build_trade_plan(c)
        vcond = result["trade_plan"]["entry_conditions"]["volume"]
        has_match = "1.3x 이상" in vcond
        check("T20: volume_ratio=1.37 → '1.3x 이상' 포함", has_match, f"volume_cond={vcond}")
    except Exception as e:
        check("T20: volume_ratio=1.37 → '1.3x 이상' 포함", False, str(e))

    # T21: volume_ratio=0.60 → 부족 표시
    try:
        c = {"ticker": "NOVOLV", "current_price": 100.0, "ma20": 95.0,
             "above_ma20": False, "volume_ratio": 0.60, "price_20d_pct": 2.0}
        result = build_trade_plan(c)
        vcond = result["trade_plan"]["entry_conditions"]["volume"]
        has_shortage = "부족" in vcond or "보류" in vcond
        check("T21: volume_ratio=0.60 → 거래량 부족/보류 표시", has_shortage, f"volume_cond={vcond}")
    except Exception as e:
        check("T21: volume_ratio=0.60 → 부족 표시", False, str(e))

    # T22: change_1d=-9.9 → 추세형 아님, grade D
    try:
        c = {"ticker": "DROP", "name": "Drop", "current_price": 200.0,
             "price_1d_pct": -9.9, "price_20d_pct": 25.2,
             "volume_ratio": 2.0, "volume_avg_20d": 5_000_000,
             "above_ma20": True, "above_ma60": True, "ma20": 190.0, "ma60": 160.0,
             "market_cap": 100e9, "pe_ratio": 15.0, "dist_from_52w_high_pct": -5.0,
             "market": "US", "_validation_verdict": "pass",
             "risk_reward_ratio": 1.91, "stop_price": 184.3, "target_price": 230.0}
        scored = score_candidate(c)
        not_trend_top = (scored.get("grade") in ("D", "EXCLUDE")
                         and scored.get("candidate_type") != "추세형")
        check("T22: p1d=-9.9 → 추세형 아님 + grade D/EXCLUDE", not_trend_top,
              f"grade={scored['grade']}, type={scored.get('candidate_type')}, flag={scored.get('_sharp_drop_flag','')}")
    except Exception as e:
        check("T22: p1d=-9.9 → 추세형 TOP 아님", False, str(e))

    # T23: 동일 sector 4개 → TOP 10에서 최대 3개
    try:
        def _make_c(ticker, sector, score_val, rr=2.0):
            return {"ticker": ticker, "sector": sector, "grade": "C", "total_score": score_val,
                    "_validation_verdict": "pass", "candidate_type": "추세형", "dynamic_pool": True,
                    "risk_reward_ratio": rr, "volume_ratio": 1.5, "price_1d_pct": 1.0, "score_breakdown": {}}

        fin_cands = [_make_c(f"FIN{i}", "Financial Services", 80 - i) for i in range(4)]
        oth_cands = [_make_c(f"OTH{i}", "Technology", 60 - i) for i in range(6)]
        top23 = select_top_candidates(fin_cands + oth_cands, max_candidates=10)
        fin_in = sum(1 for c in top23 if c.get("sector") == "Financial Services")
        check("T23: 동일 sector 4개 → TOP 10에서 ≤3개", fin_in <= 3,
              f"fin_in_top={fin_in}")
    except Exception as e:
        check("T23: 동일 sector 4개 → TOP 3개 제한", False, str(e))

    # T24: TOP 5에 동일 sector 3개 이상 불가 (3개 섹터 구성으로 검증)
    try:
        # Finance 5개(고점), Technology 3개(중점), Healthcare 2개(저점)
        # Finance top5 cap=2이므로 TOP5에 Finance는 최대 2개여야 함
        fin5 = [_make_c(f"F{i}", "Financial Services", 90 - i) for i in range(5)]
        tec3 = [_make_c(f"T{i}", "Technology", 75 - i) for i in range(3)]
        hlt2 = [_make_c(f"H{i}", "Healthcare", 60 - i) for i in range(2)]
        top24 = select_top_candidates(fin5 + tec3 + hlt2, max_candidates=10)
        top5_fin = sum(1 for c in top24[:5] if c.get("sector") == "Financial Services")
        check("T24: TOP 5에 동일 sector ≤2개", top5_fin <= 2,
              f"top5_fin={top5_fin}, top5={[c['ticker'] for c in top24[:5]]}")
    except Exception as e:
        check("T24: TOP 5에 동일 sector ≤2개", False, str(e))

    # T25: 뉴스 데이터 없는 후보 → news_score != 10 (5여야 함)
    try:
        c = {"ticker": "NONEWS", "current_price": 100.0, "price_1d_pct": 1.0,
             "_validation_verdict": "pass"}
        scored = score_candidate(c)
        news_score = scored.get("score_breakdown", {}).get("news", 10)
        check("T25: 뉴스 없는 후보 → news_score=5 (not 10)", news_score == 5,
              f"news_score={news_score}")
    except Exception as e:
        check("T25: 뉴스 없는 후보 → news_score 5", False, str(e))

    total = len(results)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed_tests = [(n, d) for n, s, d in results if s == "FAIL"]

    print(f"\n=== 결과: {passed}/{total} PASS ===")
    if failed_tests:
        print("실패한 테스트:")
        for n, d in failed_tests:
            print(f"  FAIL: {n} — {d}")

    return passed == total


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    parser = argparse.ArgumentParser(description="candidate_screener — dynamic market screener")
    parser.add_argument("--mock-test", action="store_true", help="Run mock tests (T1–T25)")
    parser.add_argument("--markets", nargs="+", default=["US", "KR"])
    parser.add_argument("--max-pool", type=int, default=300)
    parser.add_argument("--max-us-tickers", type=int, default=None)
    parser.add_argument("--max-kr-tickers", type=int, default=None)
    parser.add_argument("--fast", action="store_true", help="Quick run: US=50, KR=20")
    parser.add_argument("--timeout-seconds", type=int, default=480)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.mock_test:
        ok = _run_mock_tests()
        print(f"\n[DONE] candidate_screener completed in {time.time() - t0:.1f}s")
        sys.exit(0 if ok else 1)

    if args.fast:
        max_us, max_kr = 50, 20
    else:
        max_us = args.max_us_tickers if args.max_us_tickers is not None else args.max_pool
        max_kr = args.max_kr_tickers if args.max_kr_tickers is not None else min(args.max_pool, 50)

    result = run_screening(
        markets=[m.upper() for m in args.markets],
        max_pool=args.max_pool,
        dry_run=args.dry_run,
        max_us_tickers=max_us,
        max_kr_tickers=max_kr,
        timeout_seconds=args.timeout_seconds,
        use_cache=not args.no_cache,
    )

    elapsed = time.time() - t0
    qc = result.get("quality_check", {})

    print(f"\n=== 스크리닝 완료 ===")
    print(f"market_scan_status : {result['market_scan_status']}")
    print(f"top_candidates     : {len(result['top_candidates'])}개")
    print(f"elapsed_seconds    : {result.get('elapsed_seconds', round(elapsed, 1))}s")
    print(f"timeout_count      : {result.get('timeout_count', 0)}")
    print(f"cache_used         : {result.get('cache_used', 0)}")
    if result.get("output_path"):
        print(f"output             : {result['output_path']}")

    print(f"\nQuality check:")
    print(f"  rr_below_1_excluded     : {qc.get('rr_below_1_excluded', 0)}")
    print(f"  volume_condition_mismatch: {qc.get('volume_condition_mismatch', 0)}")
    print(f"  sharp_drop_excluded     : {qc.get('sharp_drop_excluded', 0)}")
    print(f"  sector_cap_applied      : {qc.get('sector_cap_applied', 0)}")
    print(f"  news_missing_count      : {qc.get('news_missing_count', 0)}")
    print(f"  top_dynamic_count       : {qc.get('top_dynamic_count', 0)}")
    print(f"  top_fallback_count      : {qc.get('top_fallback_count', 0)}")

    if result["top_candidates"]:
        print("\n--- TOP 후보 ---")
        for i, c in enumerate(result["top_candidates"], 1):
            tp = c.get("trade_plan", {})
            rr = tp.get("risk_reward_ratio", c.get("risk_reward_ratio", 0)) or 0
            drop_flag = " [급락]" if c.get("_sharp_drop_flag") else ""
            print(
                f"  {i:2d}. [{c.get('grade','?')}] {c['ticker']:12s} "
                f"score={c.get('total_score',0):3d} "
                f"type={c.get('candidate_type','?'):6s} "
                f"RR={rr:.2f} "
                f"vol={c.get('volume_ratio',0):.2f}x"
                f"{drop_flag}"
            )

    print(f"\n[DONE] candidate_screener completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
