"""
chief_ai.py — 총괄 AI (Chief of Staff) v2.0
역할: 감시자 + 보고자 + 승인 요청자

원칙:
- 실제 매수/매도/주문 절대 실행 안 함
- investment_report.py 수정 없음
- portfolio.json, learning_log.json, .sent_reports.json 읽기만
- agent_status.json은 대시보드 직접 연동 가능한 구조로 유지
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from alert_engine import (
    collect_all_alerts,
    dispatch_emergency_alerts,
    send_telegram_alert,
    EMERGENCY, IMPORTANT, DAILY_SUMMARY, WEEKLY_SUMMARY,
    LEVEL_EMOJI, LEVEL_LABEL,
    _now_kst_str, _now_kst,
)

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
AGENT_STATUS_PATH = os.path.join(BASE_DIR, "agent_status.json")
CHIEF_LOG_PATH    = os.path.join(BASE_DIR, "logs", "chief_ai_log.json")
PORTFOLIO_PATH    = os.path.join(BASE_DIR, "portfolio.json")
LEARNING_LOG_PATH = os.path.join(BASE_DIR, "learning_log.json")
SENT_REPORTS_PATH = os.path.join(BASE_DIR, ".sent_reports.json")

# ── 상태 레이블 매핑 (대시보드용) ────────────────────────────────────────────
STATUS_LABELS = {
    "running":     "실행 중",
    "completed":   "완료",
    "idle":        "대기 중",
    "error":       "오류",
    "not_started": "개발 중",
    "planned":     "계획 중",
    "unknown":     "확인 불가",
}

SYSTEM_STATUS_LABELS = {
    "normal":  "정상 운영 중",
    "warning": "주의 필요",
    "error":   "오류 발생",
    "halted":  "시스템 정지",
}

# ── ISO 시간 헬퍼 ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return _now_kst().isoformat(timespec="seconds")

# ── 로그 저장 ─────────────────────────────────────────────────────────────────
def _save_log(entry: dict):
    os.makedirs(os.path.dirname(CHIEF_LOG_PATH), exist_ok=True)
    try:
        with open(CHIEF_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"logs": []}
    data["logs"].append(entry)
    data["logs"] = data["logs"][-100:]
    with open(CHIEF_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── agent_status.json 읽기/쓰기 ──────────────────────────────────────────────
def load_agent_status() -> dict:
    try:
        with open(AGENT_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_agent_status(status: dict):
    now_str = _now_kst_str()
    now_iso = _now_iso()
    status["updated_at"]     = now_str
    status["updated_at_iso"] = now_iso
    with open(AGENT_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(f"  [CHIEF] agent_status.json 저장 완료", flush=True)

# ── 투자 애널리스트 상태 확인 ─────────────────────────────────────────────────
def check_investment_analyst(status: dict) -> dict:
    """기존 파일 읽기만. investment_report.py 수정 없음."""
    agent = status.get("agents", {}).get("investment_analyst", {})
    agent["errors"] = []

    # 1. 보고서 발송 여부
    try:
        with open(SENT_REPORTS_PATH, "r", encoding="utf-8") as f:
            sent = json.load(f)
        today = _now_kst().strftime("%Y-%m-%d")
        entry = sent.get(today, {})
        if entry.get("status") == "sent":
            agent["status"]              = "completed"
            agent["status_label"]        = STATUS_LABELS["completed"]
            agent["last_run_at"]         = entry.get("sent_at", "")
            agent["last_run_at_iso"]     = entry.get("sent_at", "")
            agent["last_result"]         = "오늘 보고서 발송 완료"
            agent["tasks_completed_today"] = 1
            agent["metrics"]["report_sent_today"] = True
            agent["recent_tasks"] = [{
                "task":   "투자 보고서 생성 및 발송",
                "status": "completed",
                "time":   entry.get("sent_at", ""),
            }]
        else:
            agent["status"]       = "idle"
            agent["status_label"] = STATUS_LABELS["idle"]
            agent["last_result"]  = "오늘 보고서 미발송"
            agent["metrics"]["report_sent_today"] = False
    except FileNotFoundError:
        agent["status"]       = "unknown"
        agent["status_label"] = STATUS_LABELS["unknown"]
        agent["errors"].append(".sent_reports.json 없음")
    except Exception as e:
        agent["errors"].append(f"sent_reports 읽기 실패: {e}")

    # 2. 학습 로그 승률 확인
    try:
        with open(LEARNING_LOG_PATH, "r", encoding="utf-8") as f:
            log = json.load(f)
        if log:
            recent = sorted(log.keys())[-1]
            recs   = log[recent].get("추천종목", [])
            judged = [r for r in recs if r.get("성공여부") is not None]
            if judged:
                win = sum(1 for r in judged if r.get("성공여부") is True)
                wr  = round(win / len(judged) * 100, 1)
                agent["metrics"]["win_rate_5d"] = wr
                if wr < 30:
                    agent["risk_level"]            = "high"
                    agent["needs_owner_approval"]  = True
                    agent["errors"].append(f"전일 승률 {wr}% — 신규 매수 주의")
                elif wr < 50:
                    agent["risk_level"] = "medium"
                else:
                    agent["risk_level"] = "low"
    except Exception:
        pass

    return agent

# ── dashboard 집계 업데이트 ───────────────────────────────────────────────────
def update_dashboard_stats(status: dict, alerts_by_level: dict) -> dict:
    """대시보드에서 바로 읽을 수 있는 집계값 업데이트"""
    agents = status.get("agents", {})

    running   = sum(1 for a in agents.values() if a.get("status") == "running")
    completed = sum(1 for a in agents.values() if a.get("status") == "completed")
    error     = sum(1 for a in agents.values() if a.get("status") == "error")
    total_tasks = sum(a.get("tasks_completed_today", 0) for a in agents.values())

    e_cnt = len(alerts_by_level.get(EMERGENCY, []))
    i_cnt = len(alerts_by_level.get(IMPORTANT, []))
    needs_attention = e_cnt > 0 or any(
        a.get("needs_owner_approval") for a in agents.values()
    )

    if e_cnt > 0 or error > 0:
        sys_status = "warning"
    else:
        sys_status = "normal"

    status["dashboard"] = {
        "total_tasks_today":    total_tasks,
        "total_agents":         len(agents),
        "running_agents":       running,
        "completed_agents":     completed,
        "error_agents":         error,
        "pending_agents":       sum(1 for a in agents.values() if a.get("status") == "not_started"),
        "emergency_alert_count": e_cnt,
        "important_alert_count": i_cnt,
        "needs_owner_attention": needs_attention,
        "system_halted":        False,
        "system_status":        sys_status,
        "system_status_label":  SYSTEM_STATUS_LABELS.get(sys_status, sys_status),
    }
    return status

# ── owner 섹션 업데이트 ───────────────────────────────────────────────────────
def update_owner_section(status: dict, alerts_by_level: dict) -> dict:
    """대표가 오늘 봐야 할 것들 정리"""
    tasks_today      = []
    approval_required = []
    todays_focus     = []

    # 긴급 알림 → 즉시 확인
    for alert in alerts_by_level.get(EMERGENCY, []):
        tasks_today.append({
            "priority":    "emergency",
            "priority_label": "🔴 긴급",
            "task":        alert["title"],
            "detail":      alert["detail"],
            "action":      "즉시 확인 및 판단 필요",
            "category":    alert.get("category", ""),
            "created_at":  alert.get("timestamp", _now_kst_str()),
        })

    # 중요 알림 → 당일 확인
    for alert in alerts_by_level.get(IMPORTANT, []):
        tasks_today.append({
            "priority":    "important",
            "priority_label": "🟠 중요",
            "task":        alert["title"],
            "detail":      alert["detail"],
            "action":      "당일 확인 권고",
            "category":    alert.get("category", ""),
            "created_at":  alert.get("timestamp", _now_kst_str()),
        })

    # 에이전트별 승인 필요
    for agent_id, agent in status.get("agents", {}).items():
        if agent.get("needs_owner_approval"):
            approval_required.append({
                "agent":   agent.get("display_name", agent_id),
                "reason":  agent.get("last_result", ""),
                "detail":  ", ".join(agent.get("errors", [])),
                "action":  "대표 판단 후 승인",
            })

    # 오늘 포커스 (상위 3개)
    all_tasks = tasks_today + [
        {"priority": "approval", "priority_label": "🟡 승인필요",
         "task": f"{a['agent']} 승인 필요", "detail": a["reason"],
         "action": a["action"]}
        for a in approval_required
    ]
    todays_focus = all_tasks[:3]

    status["owner"] = {
        "tasks_today":       tasks_today,
        "approval_required": approval_required,
        "todays_focus":      todays_focus,
    }
    return status

# ── activity_log 업데이트 ─────────────────────────────────────────────────────
def add_activity_log(status: dict, message: str, level: str = "info", agent: str = "chief_ai"):
    """대시보드 활동 로그에 항목 추가 (최근 20개 유지)"""
    log = status.get("activity_log", [])
    log.insert(0, {
        "time":    _now_kst_str(),
        "time_iso": _now_iso(),
        "agent":   agent,
        "level":   level,
        "message": message,
    })
    status["activity_log"] = log[:20]
    return status

# ── 일일 요약 텔레그램 메시지 ─────────────────────────────────────────────────
def build_daily_summary_message(status: dict) -> str:
    dash   = status.get("dashboard", {})
    owner  = status.get("owner", {})
    agents = status.get("agents", {})
    now    = _now_kst_str()

    inv    = agents.get("investment_analyst", {})
    inv_st = inv.get("status_label", "확인 불가")
    inv_wr = inv.get("metrics", {}).get("win_rate_5d")
    inv_wr_str = f" | 승률 {inv_wr}%" if inv_wr is not None else ""

    lines = [
        "📋 <b>총괄 AI 일일 요약</b>",
        f"🕐 {now}",
        "",
        "─────────────────────",
        f"📊 전체 에이전트: {dash.get('total_agents')}명",
        f"✅ 완료: {dash.get('completed_agents')}  |  "
        f"🔄 실행중: {dash.get('running_agents')}  |  "
        f"❌ 오류: {dash.get('error_agents')}",
        f"🚨 긴급 알림: {dash.get('emergency_alert_count')}건  |  "
        f"🟠 중요 알림: {dash.get('important_alert_count')}건",
        "",
        "─────────────────────",
        "🤖 <b>에이전트 현황</b>",
        f"• 투자 애널리스트: {inv_st}{inv_wr_str}",
        f"• 퀀트 트레이더: 개발 중",
        f"• 비즈니스 애널리스트: 카페24 연동 예정",
        f"• 마케터/크리에이터: 계획 중",
        "",
    ]

    focus = owner.get("todays_focus", [])
    if focus:
        lines += ["─────────────────────", "📌 <b>오늘 확인 항목</b>"]
        for i, t in enumerate(focus[:3], 1):
            lines.append(f"{i}. {t.get('priority_label','')} {t.get('task','')}")
        lines.append("")

    lines += [
        "─────────────────────",
        "🔒 실제 매수/매도/발주는 실행되지 않습니다",
        f"총괄 AI v2.0 | {dash.get('system_status_label','정상')}",
    ]
    return "\n".join(lines)

# ── 텔레그램 테스트 메시지 ────────────────────────────────────────────────────
def send_test_message() -> bool:
    text = (
        "✅ <b>[총괄 AI 테스트]</b>\n"
        "─────────────────────\n"
        "• 총괄 AI 실행 완료\n"
        "• 투자 애널리스트 상태 확인 완료\n"
        "• 자동매매/비즈니스/마케팅 에이전트는 개발 예정\n"
        "• 실제 주문/광고/발주는 실행되지 않음\n"
        "─────────────────────\n"
        f"🕐 {_now_kst_str()}"
    )
    return send_telegram_alert(text)

# ── 메인 실행 ─────────────────────────────────────────────────────────────────
def run_chief_ai(test_mode: bool = False):
    print(f"\n{'='*50}", flush=True)
    print(f"[총괄 AI v2.0] 실행 시작 — {_now_kst_str()}", flush=True)
    print(f"test_mode: {test_mode}", flush=True)
    print(f"{'='*50}", flush=True)

    log_entry = {
        "run_at":    _now_kst_str(),
        "test_mode": test_mode,
        "steps":     [],
        "result":    "unknown",
        "errors":    [],
    }

    try:
        # Step 1: agent_status 로드
        print(f"\n[1/6] agent_status.json 로드", flush=True)
        status = load_agent_status()
        log_entry["steps"].append("agent_status 로드 완료")

        # Step 2: 투자 애널리스트 상태 확인
        print(f"\n[2/6] 투자 애널리스트 상태 확인", flush=True)
        status["agents"]["investment_analyst"] = check_investment_analyst(status)
        inv = status["agents"]["investment_analyst"]
        print(f"  → 상태: {inv['status_label']} / 리스크: {inv['risk_level']}", flush=True)
        if inv["errors"]:
            for e in inv["errors"]:
                print(f"  ⚠️  {e}", flush=True)
        log_entry["steps"].append(f"투자 애널리스트: {inv['status']}")

        # Step 3: 알림 조건 판단
        print(f"\n[3/6] 알림 조건 판단", flush=True)
        alerts = collect_all_alerts(base_dir=BASE_DIR)
        status["alerts"] = alerts
        e_cnt = len(alerts.get(EMERGENCY, []))
        i_cnt = len(alerts.get(IMPORTANT, []))
        print(f"  → 긴급: {e_cnt}건 / 중요: {i_cnt}건", flush=True)
        log_entry["steps"].append(f"알림 판단 완료 — 긴급:{e_cnt} 중요:{i_cnt}")

        # Step 4: dashboard + owner 집계
        print(f"\n[4/6] 대시보드 집계 및 owner 항목 정리", flush=True)
        status = update_dashboard_stats(status, alerts)
        status = update_owner_section(status, alerts)
        focus_cnt = len(status["owner"]["todays_focus"])
        print(f"  → 오늘 확인 항목: {focus_cnt}건", flush=True)
        log_entry["steps"].append(f"owner tasks: {focus_cnt}건")

        # Step 5: 활동 로그 추가
        status = add_activity_log(
            status,
            f"총괄 AI 실행 완료 — 긴급:{e_cnt} 중요:{i_cnt} 확인항목:{focus_cnt}",
            level="info",
        )

        # Step 6: 알림/요약 발송
        print(f"\n[5/6] 알림 발송", flush=True)
        if test_mode:
            sent = send_test_message()
            log_entry["steps"].append(f"테스트 메시지: {'완료' if sent else '실패'}")
            print(f"  → 테스트 메시지 발송: {'완료' if sent else '실패'}", flush=True)
        else:
            if e_cnt > 0:
                sent_cnt = dispatch_emergency_alerts(alerts)
                print(f"  → 긴급 알림 {sent_cnt}건 즉시 발송", flush=True)
            summary_msg = build_daily_summary_message(status)
            sent = send_telegram_alert(summary_msg)
            log_entry["steps"].append(f"일일 요약 발송: {'완료' if sent else '실패'}")

        # Step 7: agent_status 저장
        print(f"\n[6/6] agent_status.json 저장", flush=True)
        save_agent_status(status)
        log_entry["result"] = "success"

    except Exception as e:
        print(f"\n❌ 총괄 AI 오류: {e}", flush=True)
        log_entry["errors"].append(str(e))
        log_entry["result"] = "error"
        import traceback
        traceback.print_exc()

    finally:
        _save_log(log_entry)
        print(f"\n{'='*50}", flush=True)
        print(f"[총괄 AI] 완료 — {log_entry['result']}", flush=True)
        print(f"{'='*50}\n", flush=True)

    return log_entry

if __name__ == "__main__":
    import sys
    test_mode = "--test" in sys.argv or os.environ.get("CHIEF_TEST_MODE", "").lower() == "true"
    run_chief_ai(test_mode=test_mode)
