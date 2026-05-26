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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"텔레그램 발송 실패: {e}")

def load_portfolio():
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_portfolio(pf: dict):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)
    import subprocess
    repo = os.path.dirname(os.path.abspath(PORTFOLIO_FILE))
    subprocess.run(["git", "-C", repo, "add", "portfolio.json"], capture_output=True)
    subprocess.run(["git", "-C", repo, "commit", "-m", f"auto: 목표가/손절가 실시간 조정 {datetime.now().strftime('%Y-%m-%d %H:%M')}"], capture_output=True)
    subprocess.run(["git", "-C", repo, "push"], capture_output=True)

def get_us_price(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        return float(stock.fast_info.last_price)
    except:
        return None

def get_kr_price(code: str):
    try:
        from bs4 import BeautifulSoup
        url = f"https://finance.naver.com/item/main.nhn?code={code}"
        r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.naver.com"}, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("strong", id="_nowVal")
        if tag:
            return float(tag.get_text(strip=True).replace(",", ""))
    except:
        return None

def get_rsi(ticker: str, period: int = 14):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="30d", interval="1d")
        if len(hist) < period + 1:
            return None
        delta = hist["Close"].diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])
    except:
        return None

def get_vix():
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except:
        return None

def check_alerts():
    pf = load_portfolio()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    hour = datetime.now().hour
    is_kr_market = 0 <= hour <= 6       # UTC 00~06 = KST 09~15
    is_us_market = 13 <= hour <= 21     # UTC 13~21 = KST 22~06

    alerts = []
    target_changes = []
    pf_changed = False

    # VIX 체크 (미국장)
    vix_val = None
    if is_us_market:
        vix_val = get_vix()
        if vix_val and vix_val >= 22:
            alerts.append(
                f"⚠️ <b>VIX 급등 경고!</b>\n"
                f"현재 VIX: {vix_val:.1f} (22 이상)\n"
                f"→ 진행 중인 분할매수 일시중단 검토\n"
                f"→ 손절가 근접 종목 즉시 점검"
            )

    all_holdings = []
    for cat in ["category1", "category2", "category3"]:
        for it in pf.get(cat, []):
            all_holdings.append((cat, it))

    for cat, it in all_holdings:
        ticker = it["ticker"]
        name = it["name"]
        avg = float(it["avg_price"])
        currency = it["currency"]
        target = it.get("target_price")
        stop_loss = it.get("stop_loss")
        is_dca = (cat == "category1")  # 적립 종목

        # 현재가 수집
        if currency == "KRW":
            if not is_kr_market:
                continue
            price = get_kr_price(ticker.split(".")[0])
        else:
            if not is_us_market:
                continue
            price = get_us_price(ticker)

        if not price:
            continue

        pct = (price / avg - 1) * 100

        # 목표가 도달 (적립 종목 포함)
        if target and price >= float(target):
            alerts.append(
                f"🎯 <b>{name}({ticker}) 목표가 도달!</b>\n"
                f"현재가: {price:,.2f} / 목표가: {float(target):,.2f}\n"
                f"수익률: {pct:+.1f}%\n"
                f"{'→ 적립 지속 (매도 금지)' if is_dca else '→ 분할 매도 검토 권고'}"
            )

        # 손절가 이탈 (적립 종목 제외)
        if not is_dca and stop_loss and price <= float(stop_loss):
            alerts.append(
                f"🚨 <b>{name}({ticker}) 손절가 이탈!</b>\n"
                f"현재가: {price:,.2f} / 손절가: {float(stop_loss):,.2f}\n"
                f"손실률: {pct:+.1f}%\n"
                f"→ 즉시 매도 검토 권고"
            )

        # 급등락 감지 (±5% 이상) → 목표가/손절가 자동 조정
        if abs(pct) >= 5 and currency != "KRW":  # 한국주는 RSI 계산 불안정
            rsi = get_rsi(ticker)
            if rsi:
                new_target = target
                new_stop = stop_loss
                change_reason = []

                # RSI 과열 → 목표가 하향
                if rsi >= 75 and target:
                    new_target = round(float(target) * 0.95, 2)
                    change_reason.append(f"RSI {rsi:.1f} 과열")

                # RSI 과매도 → 손절가 하향 (숨쉴 공간)
                if rsi <= 30 and stop_loss and not is_dca:
                    new_stop = round(float(stop_loss) * 0.97, 2)
                    change_reason.append(f"RSI {rsi:.1f} 과매도")

                # 급등(+5%) → 목표가 상향
                if pct >= 5 and target:
                    new_target = round(float(target) * 1.05, 2)
                    change_reason.append(f"급등 {pct:+.1f}%")

                # 급락(-5%) + 손절가 근접 → 알림
                if pct <= -5 and stop_loss:
                    proximity = (price - float(stop_loss)) / float(stop_loss) * 100
                    if proximity <= 3:
                        alerts.append(
                            f"🔴 <b>{name}({ticker}) 손절가 근접 경고!</b>\n"
                            f"현재가: {price:,.2f} / 손절가: {float(stop_loss):,.2f}\n"
                            f"손절가까지: {proximity:.1f}%\n"
                            f"→ 지금 당장 매도 준비"
                        )

                # 변경사항 저장
                if change_reason:
                    if new_target != target:
                        it["target_price"] = new_target
                        pf_changed = True
                    if new_stop != stop_loss:
                        it["stop_loss"] = new_stop
                        pf_changed = True
                    target_changes.append(
                        f"🔄 <b>{name}({ticker}) 목표가/손절가 자동 조정</b>\n"
                        f"사유: {', '.join(change_reason)}\n"
                        f"목표가: {target} → {new_target}\n"
                        f"손절가: {stop_loss} → {new_stop}\n"
                        f"→ 내일 보고서에 반영됨"
                    )

        # 분할 계획 진입 타이밍
        for a in pf.get("pending_actions", []):
            if a.get("status") != "진행중" or a.get("ticker") != ticker:
                continue
            done = a.get("done_units", 0)
            total = a.get("total_units", 0)
            remaining = total - done
            entry_price = a.get("unit_amount_usd")
            if remaining <= 0 or not entry_price:
                continue
            if price <= float(entry_price) * 1.02:
                alerts.append(
                    f"🔄 <b>{name}({ticker}) 분할 진입 타이밍!</b>\n"
                    f"현재가: {price:,.2f} / 목표 진입가: {entry_price}\n"
                    f"진행: {done}/{total}회 완료, {remaining}회 남음\n"
                    f"→ 지금 {done+1}회차 진입 검토"
                )

    # 변경사항 저장 및 알림
    all_alerts = alerts + target_changes
    if pf_changed:
        save_portfolio(pf)

    if all_alerts:
        header = f"📊 <b>포트폴리오 실시간 알림</b> ({now})\n{'='*30}\n\n"
        msg = header + "\n\n".join(all_alerts)
        send_telegram(msg)
        print(f"[{now}] 알림 {len(all_alerts)}건 발송")
    else:
        print(f"[{now}] 이상 없음")

if __name__ == "__main__":
    check_alerts()
