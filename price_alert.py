import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
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
        # 오늘 5분봉 데이터
        hist = stock.history(period="1d", interval="5m")
        if not hist.empty:
            current = float(hist["Close"].iloc[-1])
            open_price = float(hist["Open"].iloc[0])  # 시가
            return current, open_price
    except:
        pass
    return None, None


def get_kr_price(code: str):
    try:
        from bs4 import BeautifulSoup
        url = f"https://finance.naver.com/item/main.nhn?code={code}"
        r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.naver.com"}, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        # 현재가
        tag = soup.find("strong", id="_nowVal")
        current = float(tag.get_text(strip=True).replace(",", "")) if tag else None
        # 시가
        open_price = None
        table = soup.find("table", class_="no_info")
        if table:
            tds = table.find_all("td")
            for i, td in enumerate(tds):
                if "시가" in td.get_text():
                    try:
                        open_price = float(tds[i+1].get_text(strip=True).replace(",", ""))
                    except:
                        pass
        return current, open_price
    except:
        pass
    return None, None


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


def get_market_context() -> dict:
    """실시간 시장 컨텍스트 수집 — VIX, Fear&Greed, 뉴스"""
    context = {}
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d", interval="5m")
        if not hist.empty:
            context["vix"] = round(float(hist["Close"].iloc[-1]), 2)
    except:
        pass

    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            context["fear_greed"] = round(float(data["fear_and_greed"]["score"]), 1)
            context["fear_greed_label"] = data["fear_and_greed"]["rating"]
    except:
        pass

    try:
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="2d")
        if len(hist) >= 2:
            context["dxy"] = round(float(hist["Close"].iloc[-1]), 2)
            context["dxy_chg"] = round((hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100, 2)
    except:
        pass

    try:
        tnx = yf.Ticker("^TNX")
        hist = tnx.history(period="2d")
        if len(hist) >= 2:
            context["tnx"] = round(float(hist["Close"].iloc[-1]), 2)
    except:
        pass

    try:
        r = requests.get(
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8
        )
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            headlines = [item.findtext("title", "") for item in root.findall(".//item")[:5]]
            context["headlines"] = headlines
    except:
        pass

    return context


def analyze_with_claude(
    ticker: str,
    name: str,
    price: float,
    avg_price: float,
    pct_change: float,
    direction: str,
    is_dca: bool,
    context: dict,
    target_price=None,
    stop_loss=None,
) -> str:
    """Claude API로 시장 상황 종합 분석"""
    try:
        vix = context.get("vix", "N/A")
        fg = context.get("fear_greed", "N/A")
        fg_label = context.get("fear_greed_label", "")
        dxy = context.get("dxy", "N/A")
        tnx = context.get("tnx", "N/A")
        headlines = context.get("headlines", [])
        headlines_str = "\n".join(f"- {h}" for h in headlines) if headlines else "뉴스 수집 실패"

        strategy = "적립식 장기 복리 전략 (매도 금지, 급락 시 추가 적립 기회)" if is_dca else f"매매 종목 (목표가: {target_price}, 손절가: {stop_loss})"

        prompt = f"""너는 실시간 포트폴리오 알림 분석 AI다.
아래 상황을 보고 투자자에게 보낼 텔레그램 알림 메시지를 작성해라.

[감지된 이벤트]
종목: {name}({ticker})
현재가: {price:,.2f}
평단가: {avg_price:,.2f}
변동률: {pct_change:+.1f}% ({direction})
전략: {strategy}

[현재 시장 상황]
VIX: {vix} (20 이하 안정 / 20~30 경계 / 30 이상 공포)
Fear & Greed: {fg} ({fg_label})
DXY 달러인덱스: {dxy}
10년 국채금리: {tnx}%

[최신 글로벌 뉴스]
{headlines_str}

[작성 원칙]
1. 이 {direction}이 일시적인지 구조적인지 판단하라
2. 과거 유사 사례 1~2개를 언급하라 (코로나, 러우전쟁, AI랠리 등)
3. 최악/최선 시나리오와 확률을 제시하라
4. 전략에 맞는 구체적인 액션을 제시하라:
   - 적립식: 지금 추가 적립할지 / 기다릴지 / 2배 적립할지
   - 매매 종목: 매도/홀딩/추가매수 중 하나를 명확히
5. 다음 알림 트리거 조건을 명시하라
6. 300자 이내로 간결하게 작성하라
7. 한국어로 작성하라

메시지 형식:
📊 Claude 시장 분석:
[분석 내용]

[판단]: [결론]
→ [액션]
→ [다음 알림 조건]"""

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if response.status_code == 200:
            content = response.json().get("content", [])
            return content[0].get("text", "") if content else ""
        else:
            print(f"Claude API 오류: {response.status_code} {response.text[:100]}")
    except Exception as e:
        print(f"Claude API 호출 실패: {e}")
    return ""


def check_alerts():
    pf = load_portfolio()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    hour = datetime.now().hour
    is_kr_market = 0 <= hour <= 6
    is_us_market = 13 <= hour <= 21

    alerts = []
    target_changes = []
    pf_changed = False

    # 시장 컨텍스트 한 번만 수집
    context = get_market_context()
    vix_val = context.get("vix")

    # VIX 급등 경고 (미국장)
    if is_us_market and vix_val and vix_val >= 22:
        alerts.append(
            f"⚠️ <b>VIX 급등 경고!</b>\n"
            f"현재 VIX: {vix_val:.1f}\n"
            f"→ 진행 중인 분할매수 일시중단 검토"
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
        is_dca = it.get("dca_alert", False)

        # 현재가 수집
        if currency == "KRW":
            if not is_kr_market:
                continue
            price, open_price = get_kr_price(ticker.split(".")[0])
        else:
            if not is_us_market:
                continue
            price, open_price = get_us_price(ticker)

        if not price:
            continue

        # 급등락 감지는 시가 대비 현재가
        pct_from_avg = (price / avg - 1) * 100
        pct = (price / open_price - 1) * 100 if open_price else pct_from_avg

        # 목표가 도달 알림 (적립식 제외)
        if not is_dca and target and price >= float(target):
            claude_analysis = analyze_with_claude(
                ticker, name, price, avg, pct, "급등(목표가 도달)",
                is_dca, context, target, stop_loss
            )
            alerts.append(
                f"🎯 <b>{name}({ticker}) 목표가 도달!</b>\n"
                f"현재가: {price:,.2f} / 목표가: {float(target):,.2f}\n"
                f"수익률: {pct:+.1f}%\n\n"
                f"{claude_analysis}"
            )

        # 손절가 이탈 알림 (적립식 제외)
        elif not is_dca and stop_loss and price <= float(stop_loss):
            claude_analysis = analyze_with_claude(
                ticker, name, price, avg, pct, "급락(손절가 이탈)",
                is_dca, context, target, stop_loss
            )
            alerts.append(
                f"🚨 <b>{name}({ticker}) 손절가 이탈!</b>\n"
                f"현재가: {price:,.2f} / 손절가: {float(stop_loss):,.2f}\n"
                f"손실률: {pct:+.1f}%\n\n"
                f"{claude_analysis}"
            )

        # 급등락 감지 (±3% 이상, 시가 대비) — 모든 종목
        elif abs(pct) >= 3:
            direction = "급등" if pct > 0 else "급락"
            claude_analysis = analyze_with_claude(
                ticker, name, price, avg, pct, direction,
                is_dca, context, target, stop_loss
            )

            if is_dca:
                emoji = "📈" if pct > 0 else "💰"
                base_msg = (
                    f"{emoji} <b>{name}({ticker}) {direction} 감지 ({pct:+.1f}%)</b>\n"
                    f"현재가: {price:,.2f} (시가 대비 {pct:+.1f}% / 평단 대비 {pct_from_avg:+.1f}%)\n\n"
                    f"{claude_analysis}"
                )
            else:
                emoji = "🚀" if pct > 0 else "⚠️"
                base_msg = (
                    f"{emoji} <b>{name}({ticker}) {direction} 감지 ({pct:+.1f}%)</b>\n"
                    f"현재가: {price:,.2f} / 평단: {avg:,.2f}\n"
                    f"수익률: {pct:+.1f}%\n\n"
                    f"{claude_analysis}"
                )
            alerts.append(base_msg)

            # RSI 기반 목표가/손절가 자동 조정 (적립식 제외, 미국주만)
            if not is_dca and currency != "KRW":
                rsi = get_rsi(ticker)
                if rsi:
                    new_target = target
                    new_stop = stop_loss
                    change_reason = []

                    if rsi >= 75 and target:
                        new_target = round(float(target) * 0.95, 2)
                        change_reason.append(f"RSI {rsi:.1f} 과열")
                    if rsi <= 30 and stop_loss:
                        new_stop = round(float(stop_loss) * 0.97, 2)
                        change_reason.append(f"RSI {rsi:.1f} 과매도")
                    if pct >= 3 and target:
                        new_target = round(float(target) * 1.05, 2)
                        change_reason.append(f"급등 {pct:+.1f}%")

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

        # 손절가 근접 경고 (3% 이내, 적립식 제외)
        if not is_dca and stop_loss and price > float(stop_loss):
            proximity = (price - float(stop_loss)) / float(stop_loss) * 100
            if proximity <= 3:
                alerts.append(
                    f"🔴 <b>{name}({ticker}) 손절가 근접!</b>\n"
                    f"현재가: {price:,.2f} / 손절가: {float(stop_loss):,.2f}\n"
                    f"손절가까지 {proximity:.1f}% 남음\n"
                    f"→ 매도 준비"
                )

        # 분할매수 진입 타이밍
        for a in pf.get("pending_actions", []):
            if a.get("status") != "진행중" or a.get("ticker") != ticker:
                continue
            done = a.get("done_units", 0)
            total = a.get("total_units", 0)
            remaining = total - done
            entry_price = a.get("unit_amount_usd")
            if remaining <= 0 or not entry_price:
                continue
            if price and price <= float(entry_price) * 1.02:
                alerts.append(
                    f"🔄 <b>{name}({ticker}) 분할 진입 타이밍!</b>\n"
                    f"현재가: {price:,.2f} / 목표 진입가: {entry_price}\n"
                    f"진행: {done}/{total}회 완료, {remaining}회 남음\n"
                    f"→ 지금 {done+1}회차 진입 검토"
                )

    # 변경사항 저장
    if pf_changed:
        save_portfolio(pf)

    all_alerts = alerts + target_changes
    if all_alerts:
        header = f"📊 <b>포트폴리오 실시간 알림</b> ({now})\n{'='*30}\n\n"
        msg = header + "\n\n".join(all_alerts)
        send_telegram(msg)
        print(f"[{now}] 알림 {len(all_alerts)}건 발송")
    else:
        print(f"[{now}] 이상 없음")


if __name__ == "__main__":
    check_alerts()
