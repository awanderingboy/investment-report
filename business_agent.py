"""
business_agent.py — 비즈니스 애널리스트 에이전트
베이그/베르토 쇼핑몰 분석 담당

현재 상태: 카페24 연동 준비 단계
- 카페24 API 연동 구조 완료
- 실제 데이터 수집은 토큰 발급 후 진행

원칙:
- 실제 광고비 변경/발주/할인율 변경 절대 실행 안 함
- 분석 및 보고만 담당
- investment_report.py 수정 없음
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
from datetime import datetime, timezone, timedelta
from cafe24_client import check_connection_status, get_today_orders, get_sales_summary

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
AGENT_STATUS_PATH = os.path.join(BASE_DIR, "agent_status.json")

def _now_kst_str() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%d %H:%M:%S KST")

def _now_iso() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).isoformat(timespec="seconds")

# ── agent_status 읽기/쓰기 ────────────────────────────────────────────────────
def load_agent_status() -> dict:
    try:
        with open(AGENT_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_agent_status(status: dict):
    status["updated_at"]     = _now_kst_str()
    status["updated_at_iso"] = _now_iso()
    with open(AGENT_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

# ── 카페24 연동 상태 확인 ─────────────────────────────────────────────────────
def check_cafe24_status() -> dict:
    """카페24 연동 준비 상태 확인. 실제 API 호출 없음."""
    conn = check_connection_status()

    status_map = {
        "ready":          ("대기 중", "low",    "카페24 연동 완료 — 데이터 수집 가능"),
        "token_needed":   ("연동 대기", "low",  "카페24 Access Token 발급 필요"),
        "not_configured": ("연동 대기", "low",  "카페24 API 설정 필요"),
        "unknown":        ("확인 불가", "medium", "카페24 연동 상태 확인 불가"),
    }

    status_label, risk, summary = status_map.get(
        conn["status"], ("연동 대기", "low", "카페24 연동 준비 중")
    )

    return {
        "cafe24_status":  conn["status"],
        "status_label":   status_label,
        "risk_level":     risk,
        "summary":        summary,
        "connection":     conn,
    }

# ── 비즈니스 데이터 수집 (토큰 준비 후 실제 수집) ────────────────────────────
def collect_business_data() -> dict:
    """
    카페24 데이터 수집.
    토큰 미설정 시 준비 상태만 반환.
    """
    conn = check_connection_status()

    if conn["status"] != "ready":
        return {
            "success":  False,
            "status":   "token_not_ready",
            "message":  conn["message"],
            "data":     None,
        }

    # 토큰 준비 완료 시 실제 데이터 수집
    try:
        from datetime import date, timedelta as _td
        today      = date.today().isoformat()
        week_ago   = (date.today() - _td(days=7)).isoformat()

        orders  = get_today_orders()
        sales   = get_sales_summary(week_ago, today)

        return {
            "success":      True,
            "status":       "collected",
            "collected_at": _now_kst_str(),
            "data": {
                "today_orders": orders.get("data", {}),
                "weekly_sales": sales.get("data", {}),
            },
        }
    except Exception as e:
        return {
            "success": False,
            "status":  "error",
            "message": str(e),
            "data":    None,
        }

# ── 알림 조건 판단 ────────────────────────────────────────────────────────────
def analyze_business_alerts(data: dict) -> list:
    """
    비즈니스 데이터 기반 알림 조건 판단.
    현재는 구조만 — 실제 데이터 연동 후 활성화.
    """
    alerts = []
    if not data or not data.get("success"):
        return alerts

    business_data = data.get("data", {})

    # 일매출 전주 대비 -20% 이하
    sales_change = business_data.get("sales_change_pct")
    if sales_change is not None and sales_change <= -20:
        alerts.append({
            "level":    "important",
            "category": "business",
            "title":    f"매출 급감: {sales_change:.1f}%",
            "detail":   f"전주 같은 요일 대비 매출 {sales_change:.1f}% 하락",
        })

    # 반품률 급증
    return_rate = business_data.get("return_rate_pct")
    if return_rate is not None and return_rate >= 10:
        alerts.append({
            "level":    "important",
            "category": "business",
            "title":    f"반품률 급증: {return_rate:.1f}%",
            "detail":   f"반품률 {return_rate:.1f}% — 즉시 확인 필요",
        })

    return alerts

# ── agent_status 업데이트 ─────────────────────────────────────────────────────
def update_agent_status():
    """비즈니스 애널리스트 상태를 agent_status.json에 업데이트"""
    status = load_agent_status()

    cafe24 = check_cafe24_status()
    data   = collect_business_data()
    alerts = analyze_business_alerts(data)

    # 카페24 연동 상태에 따라 에이전트 상태 결정
    if cafe24["cafe24_status"] == "ready":
        agent_status = "completed" if data.get("success") else "error"
        status_label = "완료" if data.get("success") else "오류"
        last_result  = data.get("message", "데이터 수집 완료")
    else:
        agent_status = "not_started"
        status_label = "카페24 연동 대기"
        last_result  = cafe24["summary"]

    # agent_status.json 업데이트
    if "agents" not in status:
        status["agents"] = {}

    status["agents"]["business_analyst"] = {
        "id":           "business_analyst",
        "display_name": "비즈니스 애널리스트",
        "icon":         "building-store",
        "description":  "베이그/베르토 · 카페24 연동",
        "status":       agent_status,
        "status_label": status_label,
        "phase":        "ready" if cafe24["cafe24_status"] == "ready" else "development",
        "last_run_at":  _now_kst_str(),
        "last_run_at_iso": _now_iso(),
        "next_run_at":  "카페24 토큰 설정 후 자동 실행",
        "last_result":  last_result,
        "last_result_detail": cafe24["connection"].get("message", ""),
        "risk_level":   cafe24["risk_level"],
        "needs_owner_approval": False,
        "tasks_completed_today": 1 if data.get("success") else 0,
        "recent_tasks": [{
            "task":   "카페24 연동 상태 확인",
            "status": "completed",
            "time":   _now_kst_str(),
            "detail": cafe24["summary"],
        }],
        "errors": [] if data.get("success") or cafe24["cafe24_status"] != "ready" else [data.get("message", "")],
        "metrics": {
            "cafe24_connected":    cafe24["cafe24_status"] == "ready",
            "daily_sales_change_pct": None,
            "roas":                None,
            "return_rate_pct":     None,
        },
        "alerts": alerts,
        "cafe24_connection": cafe24["connection"],
    }

    save_agent_status(status)
    print(f"  [BUSINESS] agent_status.json 업데이트 완료", flush=True)
    print(f"  [BUSINESS] 카페24 상태: {cafe24['cafe24_status']}", flush=True)
    print(f"  [BUSINESS] 에이전트 상태: {agent_status} / {status_label}", flush=True)

    return status["agents"]["business_analyst"]

# ── 단독 실행 ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("[business_agent] 실행 시작")
    print("=" * 50)
    result = update_agent_status()
    print(f"\n[완료] 상태: {result['status_label']}")
    print(f"메시지: {result['last_result']}")
