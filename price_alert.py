import os
import json
import requests
from datetime import datetime
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HEADERS = {"User-Agent": "Mozilla/5.0"}

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})

def load_portfolio():
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def get_current_price(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        return float(stock.fast_info.last_price)
    except:
        return None

def get_naver_price(code: str):
    try:
        from bs4 import BeautifulSoup
        url = f"https://finance.naver.com/item/main.nhn?code={code}"
        r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.naver.com"}, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("strong", id="_nowVal")
        if tag:
            return float(tag.get_text(strip=True).replace(",", ""))
    except:
        pass
    return None

def check_alerts():
    pf = load_portfolio()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    alerts = []

    hour = datetime.now().hour
    is_kr_market = 0 <= hour <= 6      # UTC 00~06 = KST 09~15
    is_us_market = 13 <= hour <= 21    # UTC 13~21 = KST 22~06

    all_holdings = (
        pf.get("category1", []) +
        pf.get("category2", []) +
        pf.get("category3", [])
    )

    for it in all_holdings:
        ticker = it["ticker"]
        name = it["name"]
        avg = it["avg_price"]
        currency = it["currency"]
        target = it.get("target_price")
        stop_loss = it.get("stop_loss")

        if currency == "KRW":
            if not is_kr_market:
                continue
            price = get_naver_price(ticker.split(".")[0])
        else:
            if not is_us_market:
                continue
            price = get_current_price(ticker)

        if not price:
            continue

        pct = (price / float(avg) - 1) * 100

        if target and price >= float(target):
            alerts.append(
                f"🎯 <b>{name}({ticker}) 목표가 도달!</b>\n"
                f"현재가: {price:,.0f} / 목표가: {float(target):,.0f}\n"
                f"수익률: {pct:+.1f}%\n"
                f"→ 분할 매도 검토 권고"
            )

        if stop_loss and price <= float(stop_loss):
            alerts.append(
                f"🚨 <b>{name}({ticker}) 손절가 이탈!</b>\n"
                f"현재가: {price:,.0f} / 손절가: {float(stop_loss):,.0f}\n"
                f"손실률: {pct:+.1f}%\n"
                f"→ 즉시 매도 검토 권고"
            )

    for a in pf.get("pending_actions", []):
        if a.get("status") != "진행중":
            continue
        ticker = a.get("ticker")
        name = a.get("name", ticker)
        done = a.get("done_units", 0)
        total = a.get("total_units", 0)
        remaining = total - done
        entry_price = a.get("unit_amount_usd")

        if remaining <= 0 or not entry_price:
            continue

        currency = "KRW" if (ticker.endswith(".KS") or ticker.endswith(".KQ")) else "USD"
        if currency == "KRW":
            if not is_kr_market:
                continue
            price = get_naver_price(ticker.split(".")[0])
        else:
            if not is_us_market:
                continue
            price = get_current_price(ticker)

        if not price:
            continue

        if price <= float(entry_price) * 1.02:
            alerts.append(
                f"🔄 <b>{name}({ticker}) 분할 진입 타이밍!</b>\n"
                f"현재가: {price:,.2f} / 목표 진입가: {entry_price}\n"
                f"진행: {done}/{total}회 완료, {remaining}회 남음\n"
                f"→ 지금 {remaining}회차 진입 검토"
            )

    if is_us_market:
        try:
            vix = yf.Ticker("^VIX")
            vix_hist = vix.history(period="1d", interval="5m")
            if not vix_hist.empty:
                vix_val = float(vix_hist["Close"].iloc[-1])
                if vix_val >= 22:
                    alerts.append(
                        f"⚠️ <b>VIX 급등 경고!</b>\n"
                        f"현재 VIX: {vix_val:.1f}\n"
                        f"→ 진행 중인 분할매수 일시중단 검토\n"
                        f"→ 손절가 근접 종목 점검 필요"
                    )
        except:
            pass

    if alerts:
        header = f"📊 <b>포트폴리오 실시간 알림</b> ({now})\n{'='*30}\n\n"
        msg = header + "\n\n".join(alerts)
        send_telegram(msg)
        print(f"[{now}] 알림 {len(alerts)}건 발송", flush=True)
    else:
        print(f"[{now}] 이상 없음", flush=True)

if __name__ == "__main__":
    check_alerts()
