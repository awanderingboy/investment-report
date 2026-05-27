#!/usr/bin/env python3
"""텔레그램 봇 — 포트폴리오 관리 (매수/매도/현금/잔고/포트폴리오)"""

import os
import json
import logging
import math
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 한국어 이름 → 티커 별칭
ALIAS: dict[str, str] = {
    "알파벳a": "GOOGL", "알파벳": "GOOGL", "구글": "GOOGL",
    "프리포트맥모란": "FCX", "프리포트": "FCX",
    "셀트리온제약": "068760.KS",
    "카카오": "035720.KS",
    "네이버": "035420.KS",
    "빔테라퓨틱스": "BEAM", "빔": "BEAM",
    "엔비디아": "NVDA",
    "팔란티어": "PLTR",
    "파워플리트": "PWFL",
    "삼성전자": "005930.KS",
    "sk하이닉스": "000660.KS", "하이닉스": "000660.KS",
    "한화에어로스페이스": "012450.KS", "한화에어로": "012450.KS",
    "셀트리온": "068270.KS",
    "한미반도체": "042700.KS",
    "마이크로소프트": "MSFT",
    "리게티": "RGTI", "리게티컴퓨팅": "RGTI",
    "voo": "VOO",
}

# 확인 대기 중인 매수 요청: chat_id → pending dict
_pending: dict[int, dict] = {}


# ── 파일 IO ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(pf: dict):
    pf["last_updated"] = date.today().isoformat()
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)
    import subprocess
    repo = os.path.dirname(os.path.abspath(PORTFOLIO_FILE))
    subprocess.run(["git", "-C", repo, "add", "portfolio.json"], capture_output=True)
    result = subprocess.run(
        ["git", "-C", repo, "commit", "-m", f"auto: portfolio update {pf.get('last_updated', '')}"],
        capture_output=True, text=True
    )
    if "nothing to commit" not in result.stdout:
        subprocess.run(["git", "-C", repo, "push"], capture_output=True)


# ── 종목 검색 ──────────────────────────────────────────────────────────────────

def _resolve(name_or_ticker: str, pf: dict) -> tuple:
    """(ticker, item_dict, category_key) 또는 (None, None, None)"""
    key = name_or_ticker.lower().strip()
    ticker = ALIAS.get(key, name_or_ticker.upper())
    for cat in ("category1", "category2", "category3"):
        for item in pf.get(cat, []):
            if item["ticker"].upper() == ticker or item["name"].lower() == key:
                return item["ticker"], item, cat
    return None, None, None

def find_holding(pf: dict, ticker: str) -> tuple:
    """(cat, item_dict) 또는 (None, None)"""
    for cat in ("category1", "category2", "category3"):
        for it in pf.get(cat, []):
            if it["ticker"].upper() == ticker.upper() or \
               it["ticker"].split(".")[0].upper() == ticker.upper():
                return cat, it
    return None, None


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def calc_new_avg(old_shares: float, old_avg: float, new_shares: float, new_price: float) -> tuple:
    """평단 재계산 → (new_avg, total_shares)"""
    total_shares = old_shares + new_shares
    total_cost = (old_shares * old_avg) + (new_shares * new_price)
    return round(total_cost / total_shares, 4), total_shares

def add_executed_action(pf: dict, action: str, ticker: str, name: str,
                        shares: float, price: float, memo: str):
    """executed_actions에 기록 추가 (90일 롤링)"""
    if "executed_actions" not in pf:
        pf["executed_actions"] = []
    pf["executed_actions"].append({
        "date": str(date.today()),
        "action": action,
        "ticker": ticker,
        "name": name,
        "shares": shares,
        "price_krw": price,
        "memo": memo
    })
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    pf["executed_actions"] = [
        a for a in pf["executed_actions"]
        if a.get("date", "") >= cutoff
    ]


# ── 표시 헬퍼 ─────────────────────────────────────────────────────────────────

def _price_str(amount: float, currency: str) -> str:
    if currency == "KRW":
        return f"{int(amount):,}원"
    return f"${amount:,.2f}"

def _portfolio_text(pf: dict) -> str:
    lines = ["📊 <b>포트폴리오 현황</b>"]
    cat_labels = {
        "category1": "카테고리 1 (자동적립)",
        "category2": "카테고리 2 (보유)",
        "category3": "카테고리 3 (500만 프로젝트)",
    }
    for cat, label in cat_labels.items():
        items = pf.get(cat, [])
        lines.append(f"\n<b>{label}</b>")
        if not items:
            lines.append("  (보유 없음)")
            continue
        for it in items:
            avg_s = _price_str(it["avg_price"], it["currency"])
            daily = f"  [매일 ${it['daily_buy']} 적립]" if it.get("daily_buy") else ""
            lines.append(f"  {it['name']}({it['ticker']}): 평단 {avg_s} / {it['shares']}주{daily}")

    cash = pf.get("cash", {})
    cat3_cash = pf.get("category3_cash", 0)
    lines.append(f"\n💰 <b>현금</b>: {cash.get('krw', 0):,}원 / ${cash.get('usd', 0):,}")
    lines.append(f"🌱 <b>카테고리3 시드</b>: {cat3_cash:,}원")
    return "\n".join(lines)

def _balance_text(pf: dict) -> str:
    cash = pf.get("cash", {})
    cat3_cash = pf.get("category3_cash", 0)
    lines = [
        f"💰 <b>잔고</b>",
        f"원화: {cash.get('krw', 0):,}원",
        f"달러: ${cash.get('usd', 0):,}",
        f"카테고리3 시드: {cat3_cash:,}원",
    ]
    pending = [a for a in pf.get("pending_actions", []) if a.get("status") == "진행중"]
    if pending:
        lines.append("\n🔄 <b>진행 중인 분할 계획</b>")
        for a in pending:
            remaining = a["total_units"] - a["done_units"]
            lines.append(f"  {a['name']}: {a['done_units']}/{a['total_units']}회 완료 ({remaining}회 남음)")
    return "\n".join(lines)


# ── 매수 실행 ─────────────────────────────────────────────────────────────────

async def _execute_buy(update: Update, data: dict):
    ticker  = data["ticker"]
    item    = data["item"]
    cat     = data["cat"]
    shares  = data["shares"]
    price   = data["price"]
    pf      = data["pf"]

    old_shares = item.get("shares", 0)
    old_avg    = item.get("avg_price", 0)
    new_shares = old_shares + shares
    new_avg    = (old_shares * old_avg + shares * price) / new_shares if new_shares > 0 else price
    total_cost = shares * price
    currency   = item["currency"]

    item["shares"]    = round(new_shares, 6)
    item["avg_price"] = round(new_avg, 4)

    if cat == "category3":
        pf["category3_cash"] = max(0, pf.get("category3_cash", 0) - (int(total_cost) if currency == "KRW" else 0))
        if currency == "USD":
            pf["category3_cash"] = max(0, pf.get("category3_cash", 0))
    elif currency == "KRW":
        pf["cash"]["krw"] = pf["cash"].get("krw", 0) - int(total_cost)
    else:
        pf["cash"]["usd"] = round(pf["cash"].get("usd", 0) - total_cost, 2)

    # pending_actions done_units 자동 업데이트
    for a in pf.get("pending_actions", []):
        if a.get("ticker", "").upper() == ticker.upper() and a.get("status") == "진행중":
            unit_shares = a.get("unit_shares", 0)
            start_shares = a.get("start_shares", 0)
            if unit_shares > 0:
                done = min(int((new_shares - start_shares) / unit_shares), a["total_units"])
                a["done_units"] = max(done, 0)
                if a["done_units"] >= a["total_units"]:
                    a["status"] = "완료"

    # 실행 기록
    add_executed_action(pf, "매수", ticker, item["name"], shares, price, "텔레그램 매수 명령 실행")

    _save(pf)
    await update.message.reply_text(
        f"✅ <b>매수 완료</b>\n"
        f"종목: {item['name']}({ticker})\n"
        f"수량: {shares}주 @ {_price_str(price, currency)}\n"
        f"총 비용: {_price_str(total_cost, currency)}\n"
        f"새 평단: {_price_str(new_avg, currency)}\n"
        f"총 보유: {item['shares']}주",
        parse_mode="HTML",
    )


# ── 메인 핸들러 ───────────────────────────────────────────────────────────────

async def _handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if ALLOWED_ID and update.effective_chat.id != ALLOWED_ID:
        return

    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    # ── 확인 대기 응답 처리 ───────────────────────────────────────────────────
    if chat_id in _pending:
        pending = _pending.pop(chat_id)
        if text.lower() in ("확인", "yes", "y", "예", "ㅇ"):
            await _execute_buy(update, pending)
        else:
            await update.message.reply_text("❌ 매수를 취소했습니다.")
        return

    parts = text.split()
    if not parts:
        return
    cmd = parts[0].lstrip("/").lower()

    # ── 도움말 ───────────────────────────────────────────────────────────────
    if cmd in ("도움말", "help", "start"):
        await update.message.reply_text(
            "📌 <b>사용 가능한 명령어</b>\n\n"
            "<b>매수</b> [종목] [수량] [단가]\n"
            "  예: <code>매수 NVDA 5 220</code>\n\n"
            "<b>매도</b> [종목] [수량] [단가]\n"
            "  예: <code>매도 GOOGL 1 400</code>\n\n"
            "<b>현금</b> [원화|달러] [금액]\n"
            "  예: <code>현금 원화 15000000</code>\n\n"
            "<b>잔고</b> — 현금 잔고 + 진행 중인 분할 계획 조회\n"
            "<b>포트폴리오</b> — 전체 보유 현황 조회\n\n"
            "<b>분할완료</b> [ticker] — 분할 1회 완료 처리\n"
            "  예: <code>분할완료 NVDA</code>\n\n"
            "<b>분할추가</b> [ticker] [총회수] [1회당주수] [메모]\n"
            "  예: <code>분할추가 NVDA 3 1 $210~215 분할</code>\n\n"
            "<b>카테고리3시드</b> [금액] — 카테고리3 시드 설정\n\n"
            "<b>도움말</b> — 이 메시지 표시",
            parse_mode="HTML",
        )
        return

    # ── 잔고 ─────────────────────────────────────────────────────────────────
    if cmd == "잔고":
        pf = _load()
        await update.message.reply_text(_balance_text(pf), parse_mode="HTML")
        return

    # ── 포트폴리오 ────────────────────────────────────────────────────────────
    if cmd == "포트폴리오":
        pf = _load()
        await update.message.reply_text(_portfolio_text(pf), parse_mode="HTML")
        return

    # ── 현금 설정 ─────────────────────────────────────────────────────────────
    if cmd == "현금":
        if len(parts) < 3:
            await update.message.reply_text("사용법: <code>현금 [원화|달러] [금액]</code>", parse_mode="HTML")
            return
        cur_str = parts[1].lower()
        try:
            amount = float(parts[2].replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ 금액 형식이 올바르지 않습니다.")
            return
        pf = _load()
        if cur_str in ("원화", "krw", "원"):
            pf["cash"]["krw"] = int(amount)
            await update.message.reply_text(f"✅ 원화 현금 → {int(amount):,}원", parse_mode="HTML")
        elif cur_str in ("달러", "usd", "dollar", "$"):
            pf["cash"]["usd"] = round(amount, 2)
            await update.message.reply_text(f"✅ 달러 현금 → ${amount:,.2f}", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ '원화' 또는 '달러'를 지정해주세요.")
            return
        _save(pf)
        return

    # ── 카테고리3 시드 설정 ───────────────────────────────────────────────────
    if cmd in ("카테고리3시드", "시드"):
        if len(parts) < 2:
            await update.message.reply_text("사용법: <code>카테고리3시드 [금액]</code>", parse_mode="HTML")
            return
        try:
            amount = int(float(parts[1].replace(",", "")))
        except ValueError:
            await update.message.reply_text("❌ 금액 형식이 올바르지 않습니다.")
            return
        pf = _load()
        pf["category3_cash"] = amount
        _save(pf)
        await update.message.reply_text(f"✅ 카테고리3 시드 → {amount:,}원")
        return

    # ── 매수 ─────────────────────────────────────────────────────────────────
    if cmd == "매수":
        if len(parts) < 4:
            await update.message.reply_text(
                "사용법: <code>매수 [종목] [수량] [단가]</code>\n예: <code>매수 NVDA 5 220</code>",
                parse_mode="HTML",
            )
            return
        name_or_ticker = parts[1]
        try:
            shares = float(parts[2].replace(",", ""))
            price  = float(parts[3].replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ 수량 또는 단가 형식이 올바르지 않습니다.")
            return
        pf = _load()
        ticker, item, cat = _resolve(name_or_ticker, pf)
        if not ticker:
            await update.message.reply_text(f"❌ 종목을 찾을 수 없습니다: <b>{name_or_ticker}</b>", parse_mode="HTML")
            return

        currency   = item["currency"]
        total_cost = shares * price

        if cat == "category3":
            available = pf.get("category3_cash", 0) if currency == "KRW" else pf.get("category3_cash", 0)
            low_threshold_krw, low_threshold_usd = 500000, 200
        else:
            available = pf["cash"].get("krw", 0) if currency == "KRW" else pf["cash"].get("usd", 0)
            low_threshold_krw, low_threshold_usd = 1000000, 500

        cost_in_currency = int(total_cost) if currency == "KRW" else total_cost
        if currency == "KRW":
            after = available - cost_in_currency
            low_threshold = low_threshold_krw
        else:
            after = available - total_cost
            low_threshold = low_threshold_usd

        if after < 0:
            avail_str = _price_str(available, currency)
            cost_str  = _price_str(cost_in_currency if currency == "KRW" else total_cost, currency)
            await update.message.reply_text(
                f"❌ 현금 부족!\n필요: {cost_str}\n가용: {avail_str}"
            )
            return

        payload = {"ticker": ticker, "item": item, "cat": cat, "shares": shares, "price": price, "pf": pf}

        if after < low_threshold:
            after_str = _price_str(int(after) if currency == "KRW" else after, currency)
            _pending[chat_id] = payload
            await update.message.reply_text(
                f"⚠️ 매수 후 잔고가 <b>{after_str}</b>으로 낮아집니다.\n"
                f"계속하시겠습니까? (<code>확인</code> 또는 <code>취소</code>)",
                parse_mode="HTML",
            )
            return

        await _execute_buy(update, payload)
        return

    # ── 매도 ─────────────────────────────────────────────────────────────────
    if cmd == "매도":
        if len(parts) < 4:
            await update.message.reply_text(
                "사용법: <code>매도 [종목] [수량] [단가]</code>\n예: <code>매도 NVDA 2 230</code>",
                parse_mode="HTML",
            )
            return
        name_or_ticker = parts[1]
        try:
            shares = float(parts[2].replace(",", ""))
            price  = float(parts[3].replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ 수량 또는 단가 형식이 올바르지 않습니다.")
            return
        pf = _load()
        ticker, item, cat = _resolve(name_or_ticker, pf)
        if not ticker:
            await update.message.reply_text(f"❌ 종목을 찾을 수 없습니다: <b>{name_or_ticker}</b>", parse_mode="HTML")
            return

        current_shares = item.get("shares", 0)
        if shares > current_shares + 1e-9:
            await update.message.reply_text(f"❌ 보유 수량 초과! 현재 {current_shares}주 보유 중")
            return

        currency      = item["currency"]
        total_proceeds = shares * price
        avg = item["avg_price"]
        pnl_pct = (price / avg - 1) * 100
        item["shares"] = round(current_shares - shares, 6)

        if cat == "category3" and currency == "KRW":
            pf["category3_cash"] = pf.get("category3_cash", 0) + int(total_proceeds)
        elif currency == "KRW":
            pf["cash"]["krw"] = pf["cash"].get("krw", 0) + int(total_proceeds)
        else:
            pf["cash"]["usd"] = round(pf["cash"].get("usd", 0) + total_proceeds, 2)

        # 실행 기록
        add_executed_action(pf, "매도", ticker, item["name"], shares, price,
                            f"텔레그램 매도 명령 실행 (수익률 {pnl_pct:+.1f}%)")

        _save(pf)
        await update.message.reply_text(
            f"✅ <b>매도 완료</b>\n"
            f"종목: {item['name']}({ticker})\n"
            f"수량: {shares}주 @ {_price_str(price, currency)}\n"
            f"수익률: {pnl_pct:+.1f}%\n"
            f"수익금: {_price_str(int(total_proceeds) if currency == 'KRW' else total_proceeds, currency)}\n"
            f"잔여 보유: {item['shares']}주",
            parse_mode="HTML",
        )
        return

    # ── 분할완료 ──────────────────────────────────────────────────────────────
    if cmd == "분할완료":
        if len(parts) < 2:
            await update.message.reply_text(
                "사용법: <code>분할완료 [ticker]</code>\n예: <code>분할완료 NVDA</code>",
                parse_mode="HTML",
            )
            return
        ticker = parts[1].upper()
        pf = _load()
        found = False
        msg = ""
        for a in pf.get("pending_actions", []):
            if a.get("ticker", "").upper() == ticker and a.get("status") == "진행중":
                a["done_units"] = min(a["done_units"] + 1, a["total_units"])
                if a["done_units"] >= a["total_units"]:
                    a["status"] = "완료"
                    msg = f"✅ {ticker} 분할 계획 완료!\n{a['total_units']}회 전체 완료"
                else:
                    remaining = a["total_units"] - a["done_units"]
                    msg = f"✅ {ticker} 분할 진행\n{a['done_units']}/{a['total_units']}회 완료\n남은 횟수: {remaining}회"
                found = True
                break
        if not found:
            await update.message.reply_text(f"❌ {ticker} 진행 중인 분할 계획이 없습니다.")
            return
        _save(pf)
        await update.message.reply_text(msg)
        return

    # ── 분할추가 ──────────────────────────────────────────────────────────────
    if cmd == "분할추가":
        if len(parts) < 4:
            await update.message.reply_text(
                "사용법: <code>분할추가 [ticker] [총회수] [1회당주수] [메모]</code>\n"
                "예: <code>분할추가 NVDA 3 1 $210~215 분할</code>",
                parse_mode="HTML",
            )
            return
        ticker_raw = parts[1].upper()
        try:
            total_units = int(parts[2])
            unit_shares = float(parts[3])
        except ValueError:
            await update.message.reply_text("❌ 총회수/1회당주수 형식이 올바르지 않습니다.")
            return
        memo = " ".join(parts[4:]) if len(parts) > 4 else ""

        pf = _load()
        _, item, _ = _resolve(ticker_raw, pf)
        if item is None:
            await update.message.reply_text(f"❌ {ticker_raw} 보유 종목에 없습니다.")
            return

        start_shares = item["shares"]
        if "pending_actions" not in pf:
            pf["pending_actions"] = []

        pf["pending_actions"].append({
            "id": f"{ticker_raw.lower()}_split_{str(date.today()).replace('-', '')}",
            "action": "분할매수",
            "ticker": item["ticker"],
            "name": item["name"],
            "total_units": total_units,
            "done_units": 0,
            "unit_shares": unit_shares,
            "start_shares": start_shares,
            "unit_amount_usd": None,
            "memo": memo,
            "created_date": str(date.today()),
            "status": "진행중"
        })

        _save(pf)
        await update.message.reply_text(
            f"✅ 분할 계획 추가\n"
            f"종목: {item['name']}({item['ticker']})\n"
            f"계획: {total_units}회 분할 (1회당 {unit_shares}주)\n"
            f"메모: {memo}",
            parse_mode="HTML",
        )
        return

    # ── 알 수 없는 명령어 ─────────────────────────────────────────────────────
    await update.message.reply_text("❓ 알 수 없는 명령어입니다. <code>도움말</code>을 입력하세요.", parse_mode="HTML")


# ── 실행 ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, _handle))
    logger.info("텔레그램 봇 시작 (chat_id=%s)", ALLOWED_ID or "제한없음")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
