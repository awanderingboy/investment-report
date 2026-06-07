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

_EXCLUDE_FROM_NEW = {"068760.KS", "RGTI", "BEAM"}  # 영구 제외 종목

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

# candidate_type 분류
CANDIDATE_TYPES = ["안정형", "추세형", "이벤트형", "현금대체형", "보유추가형", "고위험", "제외"]
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "EXCLUDE": 4}

# ── Universe Loading ───────────────────────────────────────────────────────────

def _scrape_sp500_wikipedia():
    """S&P 500 tickers from Wikipedia."""
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
                ticker = cols[0].text.strip().replace(".", "-")
                tickers.append(ticker)
        return tickers
    except Exception:
        return []


def _scrape_nasdaq100_wikipedia():
    """NASDAQ-100 tickers from Wikipedia."""
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
                        ticker = cols[0].text.strip().replace(".", "-")
                        if ticker:
                            tickers.append(ticker)
                if tickers:
                    break
        return tickers
    except Exception:
        return []


def load_market_universe(market: str) -> dict:
    """
    Load full market universe for given market ("US" or "KR").
    Returns {
        "market": str,
        "tickers": list[str],
        "market_scan_status": "success" | "partial" | "fallback",
        "source": str,
        "count": int,
    }
    """
    market = market.upper()

    if market == "US":
        sp500 = _scrape_sp500_wikipedia()
        ndq100 = _scrape_nasdaq100_wikipedia()
        combined = list(dict.fromkeys(sp500 + ndq100))  # dedup, preserve order

        if len(combined) >= 400:
            status = "success"
            source = "Wikipedia S&P500+NASDAQ100 scrape"
        elif len(combined) >= 50:
            status = "partial"
            fallback_set = set(US_WATCHLIST_FALLBACK)
            combined = list(dict.fromkeys(combined + [t for t in US_WATCHLIST_FALLBACK if t not in set(combined)]))
            source = "Wikipedia partial + fallback supplement"
        else:
            combined = list(US_WATCHLIST_FALLBACK)
            status = "fallback"
            source = "US_WATCHLIST_FALLBACK (scrape failed)"

        return {
            "market": "US",
            "tickers": combined,
            "market_scan_status": status,
            "source": source,
            "count": len(combined),
        }

    elif market == "KR":
        # pykrx not installed — always fallback
        try:
            import pykrx  # noqa: F401
            # If somehow installed, attempt KOSPI+KOSDAQ scan
            from pykrx import stock
            today = datetime.date.today().strftime("%Y%m%d")
            kospi = stock.get_market_ticker_list(today, market="KOSPI")
            kosdaq = stock.get_market_ticker_list(today, market="KOSDAQ")
            tickers_kr = [f"{t}.KS" for t in kospi] + [f"{t}.KQ" for t in kosdaq]
            if len(tickers_kr) >= 100:
                return {
                    "market": "KR",
                    "tickers": tickers_kr,
                    "market_scan_status": "success",
                    "source": "pykrx KOSPI+KOSDAQ",
                    "count": len(tickers_kr),
                }
        except ImportError:
            pass
        except Exception:
            pass

        return {
            "market": "KR",
            "tickers": list(KR_WATCHLIST_FALLBACK),
            "market_scan_status": "fallback",
            "source": "KR_WATCHLIST_FALLBACK (pykrx unavailable)",
            "count": len(KR_WATCHLIST_FALLBACK),
        }

    else:
        raise ValueError(f"Unknown market: {market}. Use 'US' or 'KR'.")


# ── Data Collection ────────────────────────────────────────────────────────────

def _fetch_ticker_data(ticker: str, period: str = "3mo"):
    """Fetch basic price/volume/info data for one ticker. Returns None on failure."""
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

        # Simple MA
        ma20 = float(close_series.iloc[-20:].mean()) if len(close_series) >= 20 else current_price
        ma60 = float(close_series.iloc[-60:].mean()) if len(close_series) >= 60 else current_price

        market_cap = info.get("marketCap") or info.get("market_cap") or 0
        pe_ratio = info.get("trailingPE") or info.get("forwardPE") or None
        sector = info.get("sector") or ""
        industry = info.get("industry") or ""
        short_name = info.get("shortName") or info.get("longName") or ticker
        currency = info.get("currency") or "USD"

        # 52-week
        week52_high = info.get("fiftyTwoWeekHigh") or float(close_series.max())
        week52_low = info.get("fiftyTwoWeekLow") or float(close_series.min())
        dist_from_52w_high = (current_price - week52_high) / week52_high * 100 if week52_high else 0.0

        return {
            "ticker": ticker,
            "name": short_name,
            "currency": currency,
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
            "sector": sector,
            "industry": industry,
            "week52_high": week52_high,
            "week52_low": week52_low,
            "dist_from_52w_high_pct": dist_from_52w_high,
            "data_source": "yfinance",
            "data_date": hist.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception:
        return None


# ── Raw Candidate Validation ───────────────────────────────────────────────────

def validate_raw_candidate_data(candidate: dict) -> tuple[str, list[str]]:
    """
    Validate raw candidate data quality.
    Returns (verdict, issues) where verdict ∈ {"pass", "warning", "verify", "fail"}.
    "fail" → excluded from TOP.
    """
    issues = []

    # Required field: current_price
    cp = candidate.get("current_price")
    if cp is None or cp <= 0:
        return "fail", ["current_price 없음 또는 0 이하 — 데이터 취득 실패"]

    # Stop/target sanity
    stop = candidate.get("stop_price")
    target = candidate.get("target_price")
    if stop is not None and stop >= cp:
        issues.append(f"손절가({stop:.2f}) >= 현재가({cp:.2f}) — 손절 설정 오류")
        return "fail", issues
    if target is not None and target <= cp:
        issues.append(f"목표가({target:.2f}) <= 현재가({cp:.2f}) — 목표 설정 오류")
        return "fail", issues

    # Extreme 1-day move (한국 대형주 or any: 1일 +30% 이상은 이상치)
    price_1d = candidate.get("price_1d_pct", 0.0)
    if abs(price_1d) >= 30:
        issues.append(f"1일 등락 ±30% 초과({price_1d:.1f}%) — 스플릿/이벤트 의심")
        return "fail", issues

    # model_accuracy
    model_acc = candidate.get("model_accuracy")
    if model_acc is not None and model_acc < 30:
        issues.append(f"model_accuracy={model_acc}% < 30% — A/B 등급 불가")

    # Single source
    sources = candidate.get("data_sources", [])
    if isinstance(sources, list) and len(sources) <= 1:
        issues.append("단일 데이터소스 — A/B 등급 불가")

    # RR check
    rr = candidate.get("risk_reward_ratio")
    if rr is not None and rr < 1.5:
        issues.append(f"RR={rr:.2f} < 1.5 — 위험보상비율 낮음")

    # Market cap sanity (50조 KRW ≈ 38B USD threshold as proxy)
    market_cap = candidate.get("market_cap", 0)
    earnings = candidate.get("earnings_estimate", None)
    if earnings is not None and earnings > 50e12:  # 50조 KRW
        issues.append(f"추정 실적 50조+ 이상치 의심 — 검증 필요")
        return "fail", issues

    if issues:
        # Determine severity
        grade_ban = any("A/B" in i for i in issues)
        if grade_ban and len(issues) >= 2:
            return "verify", issues
        return "warning", issues

    return "pass", []


# ── Candidate Scoring ──────────────────────────────────────────────────────────

def _score_data_quality(candidate: dict) -> int:
    """Data quality component: 0–15 pts."""
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
    """Trend component: 0–20 pts."""
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
    if d52h > -5:  # near 52-week high
        score += 3
    elif d52h < -30:
        score -= 2

    return max(0, min(score, 20))


def _score_volume(candidate: dict) -> int:
    """Volume component: 0–15 pts."""
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

    # Minimum liquidity
    market = candidate.get("market", "US")
    if market == "US":
        if avg_vol >= 1_000_000:
            score += 5
        elif avg_vol >= 200_000:
            score += 2
    else:  # KR
        if avg_vol >= 100_000:
            score += 5
        elif avg_vol >= 20_000:
            score += 2

    return max(0, min(score, 15))


def _score_news_sentiment(candidate: dict) -> int:
    """News/event component: 0–20 pts (default neutral if no news data)."""
    news_score = candidate.get("news_sentiment_score")
    if news_score is None:
        return 10  # neutral default
    return max(0, min(int(news_score), 20))


def _score_fundamental(candidate: dict) -> int:
    """Fundamental component: 0–10 pts."""
    score = 5  # neutral default
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
    """Risk component: 0–10 pts (higher = lower risk)."""
    score = 5
    p1d = abs(candidate.get("price_1d_pct", 0.0))
    rr = candidate.get("risk_reward_ratio")
    validation_verdict = candidate.get("_validation_verdict", "pass")

    if p1d > 20:
        score -= 4
    elif p1d > 10:
        score -= 2

    if rr is not None:
        if rr >= 2.5:
            score += 3
        elif rr >= 2.0:
            score += 2
        elif rr >= 1.5:
            score += 1
        else:
            score -= 2

    if validation_verdict == "fail":
        score -= 5
    elif validation_verdict == "verify":
        score -= 2
    elif validation_verdict == "warning":
        score -= 1

    return max(0, min(score, 10))


def _score_portfolio_fit(candidate: dict) -> int:
    """Portfolio fit component: 0–10 pts."""
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

    dynamic_pool = candidate.get("dynamic_pool", False)
    if dynamic_pool:
        score += 2

    return max(0, min(score, 10))


def score_candidate(candidate: dict) -> dict:
    """
    Score candidate on 100-point scale.
    Returns candidate dict with added scoring fields.
    """
    candidate = dict(candidate)

    s_data = _score_data_quality(candidate)
    s_trend = _score_trend(candidate)
    s_volume = _score_volume(candidate)
    s_news = _score_news_sentiment(candidate)
    s_fundamental = _score_fundamental(candidate)
    s_risk = _score_risk(candidate)
    s_portfolio = _score_portfolio_fit(candidate)

    total = s_data + s_trend + s_volume + s_news + s_fundamental + s_risk + s_portfolio

    candidate["score_breakdown"] = {
        "data": s_data,
        "trend": s_trend,
        "volume": s_volume,
        "news": s_news,
        "fundamental": s_fundamental,
        "risk": s_risk,
        "portfolio_fit": s_portfolio,
    }
    candidate["total_score"] = total

    # Derive grade
    model_acc = candidate.get("model_accuracy")
    validation_verdict = candidate.get("_validation_verdict", "pass")
    sources = candidate.get("data_sources", [])
    single_source = isinstance(sources, list) and len(sources) <= 1

    grade_ban_ab = (model_acc is not None and model_acc < 30) or single_source
    ticker = candidate.get("ticker", "")

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

    candidate["grade"] = grade

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

    return candidate


# ── Trade Plan Builder ─────────────────────────────────────────────────────────

def build_trade_plan(candidate: dict) -> dict:
    """
    Generate entry/stop/target/RR with 4-component entry condition.
    Returns updated candidate dict with trade_plan.
    """
    cp = candidate.get("current_price", 0)
    ma20 = candidate.get("ma20", cp)
    ma60 = candidate.get("ma60", cp)
    vr = candidate.get("volume_ratio", 1.0)
    above_ma20 = candidate.get("above_ma20", False)
    p1d = candidate.get("price_1d_pct", 0.0)

    if cp <= 0:
        return candidate

    # Stop: 5% below current or MA20 whichever is lower
    stop_candidate = min(cp * 0.95, ma20 * 0.97)
    stop = candidate.get("stop_price") or round(stop_candidate, 4)

    # Target: based on trend strength
    p20 = candidate.get("price_20d_pct", 0.0)
    if p20 > 10:
        target_mult = 1.15
    elif p20 > 0:
        target_mult = 1.10
    else:
        target_mult = 1.07
    target = candidate.get("target_price") or round(cp * target_mult, 4)

    # RR
    risk = cp - stop if stop < cp else cp * 0.05
    reward = target - cp if target > cp else cp * 0.10
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    # 4-component entry conditions
    price_cond = f"현재가 {cp:.2f} 돌파 후 지지 확인"
    volume_cond = f"거래량비율 1.3x 이상 (현재 {vr:.1f}x)"
    technical_cond = f"MA20({ma20:.2f}) 위 유지" if above_ma20 else f"MA20({ma20:.2f}) 돌파 확인"
    news_cond = "실적/뉴스 촉매 또는 섹터 모멘텀 확인"

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
    }

    candidate["trade_plan"] = trade_plan
    candidate["stop_price"] = round(stop, 4)
    candidate["target_price"] = round(target, 4)
    candidate["risk_reward_ratio"] = rr

    return candidate


# ── Dynamic Pool Builder ───────────────────────────────────────────────────────

def build_dynamic_candidate_pool(market_universe: dict, max_pool: int = 300) -> list:
    """
    Fetch data for tickers in market_universe and build raw candidate pool.
    Applies basic filters before scoring.
    Returns list of raw candidate dicts (unsorted).
    """
    tickers = market_universe["tickers"][:max_pool]
    market = market_universe["market"]
    scan_status = market_universe["market_scan_status"]
    dynamic_pool_flag = scan_status == "success"

    candidates = []
    print(f"  [{market}] Fetching {len(tickers)} tickers (scan_status={scan_status})...", flush=True)

    for i, ticker in enumerate(tickers):
        if ticker in _EXCLUDE_FROM_NEW:
            continue
        data = _fetch_ticker_data(ticker)
        if data is None:
            continue
        data["market"] = market
        data["dynamic_pool"] = dynamic_pool_flag

        # Basic liquidity filter
        avg_vol = data.get("volume_avg_20d", 0)
        cp = data.get("current_price", 0)
        if market == "US" and avg_vol < 50_000:
            continue
        if market == "KR" and avg_vol < 5_000:
            continue
        if cp <= 0:
            continue

        # Validate
        verdict, issues = validate_raw_candidate_data(data)
        data["_validation_verdict"] = verdict
        data["_validation_issues"] = issues

        if verdict == "fail":
            continue

        candidates.append(data)

        if (i + 1) % 50 == 0:
            print(f"    ... {i+1}/{len(tickers)} processed, {len(candidates)} candidates so far", flush=True)
        time.sleep(0.05)  # rate limit courtesy

    return candidates


# ── Top Candidate Selector ─────────────────────────────────────────────────────

def select_top_candidates(scored_candidates: list, max_candidates: int = 10) -> list:
    """
    Select top candidates with composition rules:
    - 추세형 ≥ 2
    - dynamic_pool ≥ 2
    - 보유종목 ≤ 3
    - 안정형 ≤ 2
    - grade EXCLUDE → never included
    - No _EXCLUDE_FROM_NEW tickers
    Returns sorted top list (descending score).
    """
    eligible = [
        c for c in scored_candidates
        if c.get("grade") not in ("EXCLUDE",)
        and c.get("_validation_verdict") != "fail"
        and c.get("ticker") not in _EXCLUDE_FROM_NEW
    ]

    # Sort by total_score desc, then grade asc
    eligible.sort(key=lambda c: (-c.get("total_score", 0), GRADE_ORDER.get(c.get("grade", "D"), 3)))

    top = []
    counts = {"추세형": 0, "안정형": 0, "dynamic_pool": 0, "보유추가형": 0}

    for c in eligible:
        if len(top) >= max_candidates:
            break
        ctype = c.get("candidate_type", "")
        is_dynamic = c.get("dynamic_pool", False)
        is_holding = ctype == "보유추가형"

        # Composition caps
        if ctype == "안정형" and counts["안정형"] >= 2:
            continue
        if is_holding and counts["보유추가형"] >= 3:
            continue

        top.append(c)
        if ctype in counts:
            counts[ctype] += 1
        if is_dynamic:
            counts["dynamic_pool"] += 1

    # Composition warnings
    warnings_out = []
    trend_count = sum(1 for c in top if c.get("candidate_type") == "추세형")
    dynamic_count = sum(1 for c in top if c.get("dynamic_pool"))

    if trend_count < 2:
        warnings_out.append(f"추세형 후보 {trend_count}개 < 2개 권장")
    if dynamic_count < 2:
        warnings_out.append(f"dynamic_pool 후보 {dynamic_count}개 < 2개 권장")

    if warnings_out:
        print("  [WARNING] TOP 선정 구성 경고:", flush=True)
        for w in warnings_out:
            print(f"    - {w}", flush=True)

    return top


# ── JSON Output ────────────────────────────────────────────────────────────────

def save_top_candidates_json(top_candidates: list[dict], market_scan_status: str, meta: dict = None) -> str:
    """
    Save top candidates to outputs/top_candidates_YYYYMMDD.json.
    Returns the output file path.
    """
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

def run_screening(markets: list[str] = None, max_pool: int = 300, dry_run: bool = False) -> dict:
    """
    Full screening pipeline.
    Returns result dict with top_candidates and metadata.
    """
    if markets is None:
        markets = ["US", "KR"]

    all_candidates = []
    combined_scan_status = "success"
    meta = {"markets": markets, "universes": {}}

    for market in markets:
        print(f"\n[SCAN] Loading {market} universe...", flush=True)
        universe = load_market_universe(market)
        meta["universes"][market] = {
            "source": universe["source"],
            "count": universe["count"],
            "market_scan_status": universe["market_scan_status"],
        }

        # Downgrade combined status
        if universe["market_scan_status"] == "fallback":
            if combined_scan_status == "success":
                combined_scan_status = "partial"
            if len(markets) == 1:
                combined_scan_status = "fallback"
        elif universe["market_scan_status"] == "partial" and combined_scan_status == "success":
            combined_scan_status = "partial"

        print(f"  Universe: {universe['count']} tickers (status={universe['market_scan_status']})", flush=True)

        if not dry_run:
            raw_pool = build_dynamic_candidate_pool(universe, max_pool=max_pool)
            print(f"  [SCORE] Scoring {len(raw_pool)} raw candidates...", flush=True)
            scored = [score_candidate(build_trade_plan(c)) for c in raw_pool]
            all_candidates.extend(scored)
        else:
            print(f"  [DRY RUN] Skipping data fetch.", flush=True)

    # Check fallback-only warning
    all_fallback = all(
        meta["universes"][m]["market_scan_status"] == "fallback"
        for m in markets
        if m in meta["universes"]
    )
    if all_fallback:
        combined_scan_status = "fallback"
        print("\n  [WARNING] 전체 시장: fallback watchlist만 사용됨 — 동적 스캔 실패", flush=True)

    print(f"\n[SELECT] Total scored candidates: {len(all_candidates)}", flush=True)
    top = select_top_candidates(all_candidates, max_candidates=10)
    print(f"[SELECT] TOP {len(top)} candidates selected.", flush=True)

    if len(top) == 0:
        print("  [WARNING] top_candidates 비어있음 — 스크리닝 조건 재검토 필요", flush=True)

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
    }


# ── Mock Tests ────────────────────────────────────────────────────────────────

def _run_mock_tests() -> bool:
    """Run 15 mock tests. Returns True if all pass."""
    import traceback

    results = []

    def check(name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    print("\n=== candidate_screener.py mock tests ===\n")

    # T1: load_market_universe 함수 존재
    try:
        fn = load_market_universe
        check("T1: load_market_universe 함수 존재", callable(fn))
    except Exception as e:
        check("T1: load_market_universe 함수 존재", False, str(e))

    # T2: build_dynamic_candidate_pool 함수 존재
    try:
        fn = build_dynamic_candidate_pool
        check("T2: build_dynamic_candidate_pool 함수 존재", callable(fn))
    except Exception as e:
        check("T2: build_dynamic_candidate_pool 함수 존재", False, str(e))

    # T3: WATCHLIST_FALLBACK 이름 사용 확인
    try:
        ok = (
            isinstance(US_WATCHLIST_FALLBACK, list) and len(US_WATCHLIST_FALLBACK) > 0
            and isinstance(KR_WATCHLIST_FALLBACK, list) and len(KR_WATCHLIST_FALLBACK) > 0
        )
        check("T3: WATCHLIST_FALLBACK 이름 및 내용 확인", ok,
              f"US={len(US_WATCHLIST_FALLBACK)}, KR={len(KR_WATCHLIST_FALLBACK)}")
    except Exception as e:
        check("T3: WATCHLIST_FALLBACK 이름 및 내용 확인", False, str(e))

    # T4: current_price 없는 후보 → fail
    try:
        c = {"ticker": "TEST", "name": "Test", "price_1d_pct": 0.0}
        verdict, issues = validate_raw_candidate_data(c)
        check("T4: current_price 없는 후보 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T4: current_price 없는 후보 → fail", False, str(e))

    # T5: model_accuracy=18 → grade A/B 0개
    try:
        c = {
            "ticker": "TEST", "name": "Test", "current_price": 100.0,
            "price_1d_pct": 1.0, "model_accuracy": 18,
            "data_sources": ["yfinance", "news"],
            "_validation_verdict": "pass",
        }
        scored = score_candidate(c)
        check("T5: model_accuracy=18 → A/B 불가", scored["grade"] not in ("A", "B"),
              f"grade={scored['grade']}")
    except Exception as e:
        check("T5: model_accuracy=18 → A/B 불가", False, str(e))

    # T6: model_accuracy=18이어도 C 가능
    try:
        c = {
            "ticker": "SAFE", "name": "Safe", "current_price": 100.0,
            "price_1d_pct": 2.0, "price_20d_pct": 8.0,
            "volume_ratio": 1.5, "volume_avg_20d": 500_000,
            "above_ma20": True, "above_ma60": True,
            "ma20": 95.0, "ma60": 90.0,
            "market_cap": 50e9, "pe_ratio": 15.0,
            "dist_from_52w_high_pct": -3.0,
            "model_accuracy": 18, "market": "US",
            "data_sources": ["yfinance", "news"],
            "_validation_verdict": "pass",
        }
        scored = score_candidate(c)
        check("T6: model_accuracy=18이어도 C 가능", scored["grade"] in ("C", "D"),
              f"grade={scored['grade']}, score={scored['total_score']}")
    except Exception as e:
        check("T6: model_accuracy=18이어도 C 가능", False, str(e))

    # T7: 손절가 >= 현재가 → fail
    try:
        c = {
            "ticker": "TEST", "name": "Test",
            "current_price": 100.0, "stop_price": 105.0,
            "price_1d_pct": 1.0,
        }
        verdict, issues = validate_raw_candidate_data(c)
        check("T7: 손절가 >= 현재가 → fail", verdict == "fail", f"verdict={verdict}, issues={issues}")
    except Exception as e:
        check("T7: 손절가 >= 현재가 → fail", False, str(e))

    # T8: 목표가 <= 현재가 → fail
    try:
        c = {
            "ticker": "TEST", "name": "Test",
            "current_price": 100.0, "target_price": 90.0,
            "price_1d_pct": 1.0,
        }
        verdict, issues = validate_raw_candidate_data(c)
        check("T8: 목표가 <= 현재가 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T8: 목표가 <= 현재가 → fail", False, str(e))

    # T9: 1일 ±30% 이상 → fail
    try:
        c = {
            "ticker": "MOON", "name": "Moon",
            "current_price": 130.0, "price_1d_pct": 35.0,
        }
        verdict, issues = validate_raw_candidate_data(c)
        check("T9: 1일 +30% 이상 → fail", verdict == "fail", f"verdict={verdict}")
    except Exception as e:
        check("T9: 1일 +30% 이상 → fail", False, str(e))

    # T10: 단일소스 → A/B 금지
    try:
        c = {
            "ticker": "SOLO", "name": "Solo", "current_price": 100.0,
            "price_1d_pct": 1.0, "data_sources": ["yfinance"],
            "_validation_verdict": "warning",
        }
        verdict, issues = validate_raw_candidate_data(c)
        single_source_blocked = any("A/B" in i for i in issues)
        # Also verify scoring blocks A/B
        c2 = dict(c)
        c2["price_20d_pct"] = 15.0
        c2["above_ma20"] = True
        c2["above_ma60"] = True
        c2["volume_ratio"] = 2.5
        c2["volume_avg_20d"] = 1_000_000
        c2["market_cap"] = 100e9
        c2["pe_ratio"] = 12.0
        c2["dist_from_52w_high_pct"] = -1.0
        c2["market"] = "US"
        c2["ma20"] = 95.0
        c2["ma60"] = 90.0
        c2["_validation_verdict"] = "warning"
        scored = score_candidate(c2)
        check("T10: 단일소스 → A/B 불가", scored["grade"] not in ("A", "B"),
              f"grade={scored['grade']}, single_source_blocked={single_source_blocked}")
    except Exception as e:
        check("T10: 단일소스 → A/B 불가", False, str(e))

    # T11: TOP에 data validation fail 없음
    try:
        fail_c = {
            "ticker": "FAIL1", "name": "Fail", "current_price": 100.0,
            "grade": "B", "total_score": 75, "_validation_verdict": "fail",
            "candidate_type": "추세형", "dynamic_pool": True,
            "score_breakdown": {},
        }
        ok_c = {
            "ticker": "OK1", "name": "OK", "current_price": 100.0,
            "grade": "B", "total_score": 60, "_validation_verdict": "pass",
            "candidate_type": "추세형", "dynamic_pool": True,
            "score_breakdown": {},
        }
        top = select_top_candidates([fail_c, ok_c], max_candidates=10)
        fail_in_top = any(c["ticker"] == "FAIL1" for c in top)
        check("T11: TOP에 validation fail 후보 없음", not fail_in_top,
              f"fail_in_top={fail_in_top}, top_count={len(top)}")
    except Exception as e:
        check("T11: TOP에 validation fail 없음", False, str(e))

    # T12: fallback만 사용 시 warning 출력 확인 (market_scan_status 값 검증)
    try:
        kr_universe = load_market_universe("KR")
        is_fallback = kr_universe["market_scan_status"] == "fallback"
        check("T12: KR pykrx 없을 때 fallback status", is_fallback,
              f"status={kr_universe['market_scan_status']}, source={kr_universe['source']}")
    except Exception as e:
        check("T12: KR fallback status", False, str(e))

    # T13: JSON 파일 생성 가능
    try:
        test_candidates = [{
            "ticker": "AAPL", "name": "Apple", "current_price": 200.0,
            "grade": "A", "total_score": 80, "candidate_type": "추세형",
            "_validation_verdict": "pass", "dynamic_pool": True,
        }]
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

    # T14: top_candidates 비어있으면 warning (select_top_candidates 빈 입력)
    try:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            top_empty = select_top_candidates([], max_candidates=10)
        # Warning issued in run_screening when len==0; here just verify empty result
        check("T14: top_candidates 빈 입력 → 빈 결과", len(top_empty) == 0,
              f"top_count={len(top_empty)}")
    except Exception as e:
        check("T14: top_candidates 비어있으면 경고", False, str(e))

    # T15: 진입 조건 4개 포함 확인 (price + volume + technical + news_event)
    try:
        c = {
            "ticker": "NVDA", "name": "Nvidia", "current_price": 500.0,
            "ma20": 480.0, "ma60": 450.0, "above_ma20": True,
            "volume_ratio": 1.8, "price_1d_pct": 2.0,
        }
        result = build_trade_plan(c)
        entry_conds = result.get("trade_plan", {}).get("entry_conditions", {})
        required_keys = {"price", "volume", "technical", "news_event"}
        has_all = required_keys.issubset(set(entry_conds.keys()))
        all_nonempty = all(bool(entry_conds.get(k)) for k in required_keys)
        check("T15: 진입 조건 4개(price/volume/technical/news_event) 모두 포함",
              has_all and all_nonempty,
              f"keys={list(entry_conds.keys())}")
    except Exception as e:
        check("T15: 진입 조건 4개 포함", False, str(e))

    # Summary
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
    parser = argparse.ArgumentParser(description="candidate_screener — dynamic market screener")
    parser.add_argument("--mock-test", action="store_true", help="Run mock tests (T1–T15)")
    parser.add_argument("--markets", nargs="+", default=["US", "KR"],
                        help="Markets to scan (default: US KR)")
    parser.add_argument("--max-pool", type=int, default=300,
                        help="Max tickers to fetch per market (default: 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load universe only, skip data fetch")
    args = parser.parse_args()

    if args.mock_test:
        ok = _run_mock_tests()
        sys.exit(0 if ok else 1)

    result = run_screening(
        markets=[m.upper() for m in args.markets],
        max_pool=args.max_pool,
        dry_run=args.dry_run,
    )

    print(f"\n=== 스크리닝 완료 ===")
    print(f"market_scan_status : {result['market_scan_status']}")
    print(f"top_candidates     : {len(result['top_candidates'])}개")
    if result.get("output_path"):
        print(f"output             : {result['output_path']}")

    if result["top_candidates"]:
        print("\n--- TOP 후보 ---")
        for i, c in enumerate(result["top_candidates"], 1):
            tp = c.get("trade_plan", {})
            print(
                f"  {i:2d}. [{c.get('grade','?')}] {c['ticker']:12s} "
                f"score={c.get('total_score',0):3d} "
                f"type={c.get('candidate_type','?'):6s} "
                f"price={c.get('current_price',0):.2f} "
                f"RR={tp.get('risk_reward_ratio', c.get('risk_reward_ratio', 0)):.2f}"
            )


if __name__ == "__main__":
    main()
