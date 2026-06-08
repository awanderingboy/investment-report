"""
alert_engine.py — 총괄 AI 알림 엔진
4단계 알림 체계: emergency / important / daily_summary / weekly_summary

원칙:
- 실제 매수/매도/주문 절대 실행 안 함
- 감시 + 분류 + 보고만 담당
- investment_report.py 수정 없음
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── 환경변수 ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── KST 시간 헬퍼 ──────────────────────────────────────────────────────────────
def _now_kst():
    return datetime.now(tz=timezone(timedelta(hours=9)))

def _now_kst_str():
    return _now_kst().strftime("%Y-%m-%d %H:%M:%S KST")

# ── 알림 등급 상수 ────────────────────────────────────────────────────────────
EMERGENCY       = "emergency"       # 즉시 텔레그램 발송
IMPORTANT       = "important"       # 당일 요약에 포함
DAILY_SUMMARY   = "daily_summary"   # 하루 1회 총괄 요약
WEEKLY_SUMMARY  = "weekly_summary"  # 주간 요약

LEVEL_EMOJI = {
    EMERGENCY:      "🚨",
    IMPORTANT:      "🟠",
    DAILY_SUMMARY:  "📋",
    WEEKLY_SUMMARY: "📊",
}

LEVEL_LABEL = {
    EMERGENCY:      "긴급",
    IMPORTANT:      "중요",
    DAILY_SUMMARY:  "일일요약",
    WEEKLY_SUMMARY: "주간요약",
}

# ── 알림 조건 정의 ────────────────────────────────────────────────────────────

# 긴급 알림 — 가격 트리거 기준
PRICE_TRIGGERS = {
    "셀트리온제약_손절": {
        "ticker": "068760.KS",
        "name": "셀트리온제약",
        "condition": "below",
        "price": 46500,
        "message": "셀트리온제약 46,500원 종가 이탈 — 손절/축소 즉시 검토",
        "level": EMERGENCY,
    },
    "셀트리온제약_재검토구간": {
        "ticker": "068760.KS",
        "name": "셀트리온제약",
        "condition": "between",
        "price_low": 58000,
        "price_high": 62000,
        "message": "셀트리온제약 58,000~62,000원 진입 — 재검토 구간",
        "level": EMERGENCY,
    },
    "RGTI_손절": {
        "ticker": "RGTI",
        "name": "RGTI",
        "condition": "below",
        "price": 22,
        "message": "RGTI 22달러 종가 이탈 — 절반 축소 검토",
        "level": EMERGENCY,
    },
    "RGTI_위험": {
        "ticker": "RGTI",
        "name": "RGTI",
        "condition": "below",
        "price": 20,
        "message": "RGTI 19~20달러 이탈 — 즉시 전량 축소 검토",
        "level": EMERGENCY,
    },
    "한미반도체_손절": {
        "ticker": "042700.KS",
        "name": "한미반도체",
        "condition": "below",
        "price": 280000,
        "message": "한미반도체 280,000원 종가 이탈 — 손절/축소 즉시 검토",
        "level": EMERGENCY,
    },
}

# ── 알림 이벤트 클래스 ────────────────────────────────────────────────────────
class AlertEvent:
    def __init__(self, level: str, category: str, title: str, detail: str, source: str = "chief_ai"):
        self.level    = level
        self.category = category
        self.title    = title
        self.detail   = detail
        self.source   = source
        self.timestamp = _now_kst_str()

    def to_dict(self):
        return {
            "level":     self.level,
            "category":  self.category,
            "title":     self.title,
            "detail":    self.detail,
            "source":    self.source,
            "timestamp": self.timestamp,
        }

    def to_telegram_text(self):
        emoji = LEVEL_EMOJI.get(self.level, "❓")
        label = LEVEL_LABEL.get(self.level, self.level)
        return (
            f"{emoji} [{label}] {self.title}\n"
            f"─────────────────────\n"
            f"{self.detail}\n"
            f"─────────────────────\n"
            f"🕐 {self.timestamp}"
        )

# ── 텔레그램 발송 ─────────────────────────────────────────────────────────────
def send_telegram_alert(text: str) -> bool:
    """텔레그램으로 알림 발송. 성공 시 True 반환."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  [ALERT] 텔레그램 토큰/채팅ID 없음 — 콘솔 출력만:\n{text}", flush=True)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code == 200:
            print(f"  [ALERT] 텔레그램 발송 완료", flush=True)
            return True
        else:
            # HTML 파싱 오류 시 plain text 재시도
            resp2 = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            }, timeout=15)
            return resp2.status_code == 200
    except Exception as e:
        print(f"  [ALERT] 텔레그램 발송 실패: {e}", flush=True)
        return False

# ── 알림 조건 판단 함수들 ──────────────────────────────────────────────────────

def check_report_sent(sent_reports_path: str) -> list[AlertEvent]:
    """보고서 미발송 여부 확인"""
    alerts = []
    try:
        with open(sent_reports_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = _now_kst().strftime("%Y-%m-%d")
        entry = data.get(today, {})
        if entry.get("status") != "sent":
            alerts.append(AlertEvent(
                level=EMERGENCY,
                category="system",
                title="보고서 미발송",
                detail=f"오늘({today}) 투자 보고서가 발송되지 않았습니다. 수동 확인 필요.",
            ))
    except FileNotFoundError:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="system",
            title="sent_reports 파일 없음",
            detail=".sent_reports.json 파일이 없습니다. 보고서 발송 여부 확인 불가.",
        ))
    except Exception as e:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="system",
            title="보고서 상태 확인 실패",
            detail=str(e),
        ))
    return alerts


def check_portfolio_loss(portfolio_path: str, stock_data: Optional[dict] = None) -> list[AlertEvent]:
    """포트폴리오 하루 손실 -3% 이상 감지"""
    alerts = []
    if stock_data is None:
        return alerts
    try:
        with open(portfolio_path, "r", encoding="utf-8") as f:
            pf = json.load(f)

        total_cost = 0
        total_value = 0
        for cat in ["category1", "category2"]:
            for it in pf.get(cat, []):
                ticker = it["ticker"]
                avg = float(it["avg_price"])
                shares = float(it["shares"])
                price = stock_data.get(ticker, {}).get("현재가")
                if not price:
                    continue
                price = float(price)
                if it["currency"] == "USD":
                    cost  = avg * shares
                    value = price * shares
                else:
                    cost  = avg * shares
                    value = price * shares
                total_cost  += cost
                total_value += value

        if total_cost > 0:
            loss_pct = (total_value / total_cost - 1) * 100
            if loss_pct <= -3.0:
                alerts.append(AlertEvent(
                    level=EMERGENCY,
                    category="portfolio",
                    title=f"포트폴리오 손실 경고: {loss_pct:.1f}%",
                    detail=f"평가손실 {loss_pct:.1f}% 감지. 즉시 확인 필요. (기준: -3% 이상)",
                ))
    except Exception as e:
        print(f"  [ALERT] 포트폴리오 손실 확인 실패: {e}", flush=True)
    return alerts


def check_price_triggers(stock_data: dict) -> list[AlertEvent]:
    """사전 정의된 가격 트리거 조건 확인"""
    alerts = []
    for key, cfg in PRICE_TRIGGERS.items():
        ticker = cfg["ticker"]
        price  = stock_data.get(ticker, {}).get("현재가")
        if price is None:
            continue
        price = float(price)
        triggered = False

        if cfg["condition"] == "below":
            triggered = price < cfg["price"]
        elif cfg["condition"] == "above":
            triggered = price > cfg["price"]
        elif cfg["condition"] == "between":
            triggered = cfg["price_low"] <= price <= cfg["price_high"]

        if triggered:
            alerts.append(AlertEvent(
                level=cfg["level"],
                category="price_trigger",
                title=f"가격 조건 발동: {cfg['name']}",
                detail=f"{cfg['message']} (현재가: {price:,.0f})",
            ))
    return alerts


def check_learning_log(learning_log_path: str) -> list[AlertEvent]:
    """전일 추천 성공/실패 패턴 감지"""
    alerts = []
    try:
        with open(learning_log_path, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not log:
            return alerts

        dates = sorted(log.keys())
        recent = dates[-1] if dates else None
        if not recent:
            return alerts

        entry = log[recent]
        recs  = entry.get("추천종목", [])
        judged = [r for r in recs if r.get("성공여부") is not None]
        if not judged:
            return alerts

        success = sum(1 for r in judged if r.get("성공여부") is True)
        win_rate = success / len(judged) * 100

        if win_rate < 30:
            alerts.append(AlertEvent(
                level=IMPORTANT,
                category="learning",
                title=f"전일 추천 승률 저조: {win_rate:.0f}%",
                detail=f"{recent} 기준 {len(judged)}건 중 {success}건 적중. 모델 신뢰도 저하 — 신규 매수 주의.",
            ))
        elif win_rate >= 70:
            alerts.append(AlertEvent(
                level=IMPORTANT,
                category="learning",
                title=f"전일 추천 승률 양호: {win_rate:.0f}%",
                detail=f"{recent} 기준 {len(judged)}건 중 {success}건 적중.",
            ))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [ALERT] learning_log 확인 실패: {e}", flush=True)
    return alerts


def check_vix(macro_data: Optional[dict] = None) -> list[AlertEvent]:
    """VIX 급등 감지"""
    alerts = []
    if not macro_data:
        return alerts
    vix = macro_data.get("VIX", {})
    if not vix:
        return alerts
    val = vix.get("value")
    chg = vix.get("change_pct")
    if val and val >= 30:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="macro",
            title=f"VIX 급등: {val}",
            detail=f"VIX {val} (변동: {chg:+.1f}%) — 시장 공포 구간 진입. 신규 매수 보수적 판단 필요.",
        ))
    elif val and val >= 25:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="macro",
            title=f"VIX 주의: {val}",
            detail=f"VIX {val} — 변동성 확대 구간. 포지션 점검 권고.",
        ))
    return alerts


# ── 비즈니스 알림 (구조만 — 카페24 연동 전) ──────────────────────────────────
def check_business_alerts(business_data: Optional[dict] = None) -> list[AlertEvent]:
    """
    베이그/베르토 비즈니스 알림.
    현재는 구조만 존재. 카페24 연동 후 실제 데이터 연결 예정.
    """
    alerts = []
    if business_data is None:
        return alerts

    # 일매출 전주 대비 -20% 이하
    sales_change = business_data.get("sales_change_pct")
    if sales_change is not None and sales_change <= -20:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="business",
            title=f"매출 급감: {sales_change:.1f}%",
            detail=f"전주 같은 요일 대비 매출 {sales_change:.1f}% 하락. 카페24 데이터 확인 필요.",
        ))

    # 광고 ROAS 급락
    roas = business_data.get("roas")
    roas_prev = business_data.get("roas_prev")
    if roas and roas_prev and roas < roas_prev * 0.7:
        alerts.append(AlertEvent(
            level=IMPORTANT,
            category="business",
            title=f"광고 ROAS 급락: {roas:.2f}",
            detail=f"ROAS {roas_prev:.2f} → {roas:.2f} (전주 대비 {(roas/roas_prev-1)*100:.1f}%). 광고 점검 필요.",
        ))

    return alerts


# ── 알림 수집 + 분류 메인 함수 ────────────────────────────────────────────────
def collect_all_alerts(
    base_dir: str = ".",
    stock_data: Optional[dict] = None,
    macro_data: Optional[dict] = None,
    business_data: Optional[dict] = None,
) -> dict:
    """
    모든 알림 조건을 확인하고 등급별로 분류해서 반환.
    반환값: {"emergency": [...], "important": [...], "daily_summary": [...], "weekly_summary": [...]}
    """
    all_alerts: list[AlertEvent] = []

    # 1. 보고서 발송 여부
    sent_path = os.path.join(base_dir, ".sent_reports.json")
    all_alerts += check_report_sent(sent_path)

    # 2. 포트폴리오 손실
    pf_path = os.path.join(base_dir, "portfolio.json")
    all_alerts += check_portfolio_loss(pf_path, stock_data)

    # 3. 가격 트리거
    if stock_data:
        all_alerts += check_price_triggers(stock_data)

    # 4. 학습 로그 패턴
    log_path = os.path.join(base_dir, "learning_log.json")
    all_alerts += check_learning_log(log_path)

    # 5. VIX 급등
    all_alerts += check_vix(macro_data)

    # 6. 비즈니스 알림 (구조만)
    all_alerts += check_business_alerts(business_data)

    # 등급별 분류
    result = {
        EMERGENCY:      [],
        IMPORTANT:      [],
        DAILY_SUMMARY:  [],
        WEEKLY_SUMMARY: [],
    }
    for alert in all_alerts:
        level = alert.level if alert.level in result else DAILY_SUMMARY
        result[level].append(alert.to_dict())

    return result


# ── 즉시 발송 (긴급만) ────────────────────────────────────────────────────────
def dispatch_emergency_alerts(alerts_by_level: dict) -> int:
    """긴급 알림만 즉시 텔레그램 발송. 발송 건수 반환."""
    emergency = alerts_by_level.get(EMERGENCY, [])
    sent = 0
    for alert in emergency:
        event = AlertEvent(
            level=alert["level"],
            category=alert["category"],
            title=alert["title"],
            detail=alert["detail"],
        )
        if send_telegram_alert(event.to_telegram_text()):
            sent += 1
    return sent


# ── 단독 실행 테스트 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("[alert_engine] 단독 테스트 실행")
    print("=" * 50)

    # 더미 데이터로 테스트
    dummy_stock = {
        "068760.KS": {"현재가": 45000},   # 셀트리온제약 — 손절선 이탈 테스트
        "RGTI":      {"현재가": 21},       # RGTI — 22달러 이탈 테스트
        "042700.KS": {"현재가": 290000},   # 한미반도체 — 정상
    }
    dummy_macro = {
        "VIX": {"value": 32, "change_pct": 5.2},
    }

    alerts = collect_all_alerts(
        base_dir=".",
        stock_data=dummy_stock,
        macro_data=dummy_macro,
    )

    print(f"\n[결과]")
    for level, items in alerts.items():
        emoji = LEVEL_EMOJI.get(level, "")
        label = LEVEL_LABEL.get(level, level)
        print(f"\n{emoji} {label}: {len(items)}건")
        for a in items:
            print(f"  - [{a['category']}] {a['title']}")
            print(f"    {a['detail']}")

    print("\n[텔레그램 발송 테스트]")
    sent = dispatch_emergency_alerts(alerts)
    print(f"  → 긴급 알림 발송: {sent}건")
    print("\n[완료]")
