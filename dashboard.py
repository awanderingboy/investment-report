"""
dashboard.py — AI 조직 현황 대시보드
agent_status.json을 읽어서 실시간 현황 표시

실행: streamlit run dashboard.py
"""

import json
import os
import time
from datetime import datetime
import streamlit as st

# ── 설정 ──────────────────────────────────────────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
AGENT_STATUS_PATH = os.path.join(BASE_DIR, "agent_status.json")

st.set_page_config(
    page_title="AI 조직 대시보드",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── 스타일 ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.main { padding: 1rem 2rem; }
.metric-card {
    background: #f8f9fa;
    border-radius: 12px;
    padding: 1rem 1.25rem;
    text-align: center;
    border: 1px solid #e9ecef;
}
.metric-value { font-size: 2rem; font-weight: 600; }
.metric-label { font-size: 0.8rem; color: #6c757d; margin-top: 4px; }
.agent-card {
    background: white;
    border-radius: 12px;
    padding: 1rem 1.25rem;
    border: 1px solid #e9ecef;
    margin-bottom: 1rem;
}
.status-running  { color: #1D9E75; font-weight: 600; }
.status-completed { color: #1D9E75; font-weight: 600; }
.status-idle     { color: #888780; }
.status-error    { color: #E24B4A; font-weight: 600; }
.status-waiting  { color: #378ADD; }
.status-dev      { color: #7F77DD; }
.risk-high   { color: #E24B4A; }
.risk-medium { color: #BA7517; }
.risk-low    { color: #1D9E75; }
.alert-emergency { background: #FCEBEB; border-left: 4px solid #E24B4A; padding: 0.75rem 1rem; border-radius: 8px; margin: 0.5rem 0; }
.alert-important { background: #FAEEDA; border-left: 4px solid #BA7517; padding: 0.75rem 1rem; border-radius: 8px; margin: 0.5rem 0; }
.task-item { background: #f8f9fa; border-radius: 8px; padding: 0.75rem 1rem; margin: 0.5rem 0; }
.log-item  { font-size: 0.85rem; color: #6c757d; padding: 0.4rem 0; border-bottom: 1px solid #f1f3f5; }
</style>
""", unsafe_allow_html=True)

# ── 데이터 로드 ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_status():
    try:
        with open(AGENT_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return None

def get_status_class(status: str) -> str:
    mapping = {
        "running":     "status-running",
        "completed":   "status-completed",
        "idle":        "status-idle",
        "error":       "status-error",
        "not_started": "status-dev",
        "planned":     "status-dev",
        "unknown":     "status-idle",
    }
    return mapping.get(status, "status-idle")

def get_status_emoji(status: str) -> str:
    mapping = {
        "running":     "🔄",
        "completed":   "✅",
        "idle":        "⏸️",
        "error":       "❌",
        "not_started": "⏳",
        "planned":     "📋",
        "unknown":     "❓",
    }
    return mapping.get(status, "❓")

def get_risk_class(risk: str) -> str:
    return {"high": "risk-high", "medium": "risk-medium", "low": "risk-low"}.get(risk, "risk-low")

def get_risk_label(risk: str) -> str:
    return {"high": "🔴 높음", "medium": "🟡 중간", "low": "🟢 낮음"}.get(risk, "🟢 낮음")

# ── 메인 렌더링 ───────────────────────────────────────────────────────────────
status = load_status()

# 헤더
col_title, col_refresh = st.columns([4, 1])
with col_title:
    st.markdown("## 🤖 AI 조직 현황 대시보드")
    if status:
        st.caption(f"마지막 업데이트: {status.get('updated_at', '알 수 없음')}")
    else:
        st.caption("agent_status.json 로드 실패")
with col_refresh:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄 새로고침"):
        st.cache_data.clear()
        st.rerun()

if not status:
    st.error("agent_status.json 파일을 찾을 수 없습니다. chief_ai.py를 먼저 실행해주세요.")
    st.stop()

dash   = status.get("dashboard", {})
agents = status.get("agents", {})
owner  = status.get("owner", {})
alerts = status.get("alerts", {})
logs   = status.get("activity_log", [])

# ── 상단 메트릭 카드 ──────────────────────────────────────────────────────────
st.markdown("---")
m1, m2, m3, m4, m5 = st.columns(5)

with m1:
    sys_label = dash.get("system_status_label", "정상")
    sys_color = "#E24B4A" if dash.get("system_status") != "normal" else "#1D9E75"
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value" style="color:{sys_color}">{sys_label}</div>
        <div class="metric-label">시스템 상태</div>
    </div>""", unsafe_allow_html=True)

with m2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value">{dash.get('total_agents', 0)}</div>
        <div class="metric-label">전체 에이전트</div>
    </div>""", unsafe_allow_html=True)

with m3:
    running = dash.get('running_agents', 0) + dash.get('completed_agents', 0)
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value" style="color:#1D9E75">{running}</div>
        <div class="metric-label">오늘 실행 완료</div>
    </div>""", unsafe_allow_html=True)

with m4:
    e_cnt = dash.get('emergency_alert_count', 0)
    e_color = "#E24B4A" if e_cnt > 0 else "#1D9E75"
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value" style="color:{e_color}">{e_cnt}</div>
        <div class="metric-label">긴급 알림</div>
    </div>""", unsafe_allow_html=True)

with m5:
    tasks = len(owner.get("tasks_today", []))
    t_color = "#BA7517" if tasks > 0 else "#1D9E75"
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value" style="color:{t_color}">{tasks}</div>
        <div class="metric-label">오늘 확인 항목</div>
    </div>""", unsafe_allow_html=True)

st.markdown("---")

# ── 에이전트 현황 + 알림/로그 ─────────────────────────────────────────────────
left_col, right_col = st.columns([3, 2])

with left_col:
    st.markdown("### 🤖 에이전트 현황")

    agent_list = [
        ("investment_analyst", "📈 투자 애널리스트"),
        ("auto_trader",        "🤖 퀀트 트레이더"),
        ("business_analyst",   "🏪 비즈니스 애널리스트"),
        ("marketing_creator",  "✨ 마케터 / 크리에이터"),
    ]

    for agent_id, display_name in agent_list:
        agent = agents.get(agent_id, {})
        status_str  = agent.get("status", "unknown")
        status_label = agent.get("status_label", "확인 불가")
        risk        = agent.get("risk_level", "low")
        summary     = agent.get("summary", agent.get("last_result", ""))
        last_run    = agent.get("last_run_at", "")
        tasks_done  = agent.get("tasks_completed_today", 0)
        errors      = agent.get("errors", [])
        approval    = agent.get("needs_owner_approval", False)
        metrics     = agent.get("metrics", {})

        status_emoji = get_status_emoji(status_str)
        status_cls   = get_status_class(status_str)
        risk_label   = get_risk_label(risk)

        with st.expander(f"{display_name}  —  {status_emoji} {status_label}", expanded=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f"**리스크** {risk_label}")
            with c2:
                st.markdown(f"**오늘 작업** {tasks_done}건")
            with c3:
                if approval:
                    st.markdown("**🟡 승인 필요**")
                else:
                    st.markdown("**승인** 불필요")

            if summary:
                st.caption(summary)

            if last_run:
                st.caption(f"마지막 실행: {last_run}")

            # 투자 애널리스트 전용 메트릭
            if agent_id == "investment_analyst" and metrics:
                wr = metrics.get("win_rate_5d")
                sent = metrics.get("report_sent_today")
                mc1, mc2 = st.columns(2)
                with mc1:
                    if wr is not None:
                        st.metric("5일 승률", f"{wr}%")
                with mc2:
                    st.metric("오늘 보고서", "발송완료 ✅" if sent else "미발송 ⚠️")

            # 비즈니스 애널리스트 전용 메트릭
            if agent_id == "business_analyst":
                cafe24 = agent.get("cafe24_connection", {})
                bc1, bc2 = st.columns(2)
                with bc1:
                    token_ok = cafe24.get("token_set", False)
                    st.metric("카페24 토큰", "설정됨 ✅" if token_ok else "미설정 ⚠️")
                with bc2:
                    client_ok = cafe24.get("client_set", False)
                    st.metric("API 클라이언트", "설정됨 ✅" if client_ok else "미설정 ⚠️")

            # 최근 작업
            recent = agent.get("recent_tasks", [])
            if recent:
                st.markdown("**최근 작업**")
                for t in recent[:3]:
                    t_emoji = "✅" if t.get("status") == "completed" else "🔄"
                    st.markdown(f"""<div class="log-item">{t_emoji} {t.get('task','')} <span style="float:right">{t.get('time','')[-8:]}</span></div>""", unsafe_allow_html=True)

            # 오류
            if errors:
                for err in errors[:2]:
                    st.warning(f"⚠️ {err}")

with right_col:
    # ── 대표 확인 항목 ────────────────────────────────────────────────────────
    st.markdown("### 📌 오늘 확인 항목")
    tasks_today = owner.get("tasks_today", [])
    if tasks_today:
        for task in tasks_today[:5]:
            priority = task.get("priority_label", "")
            alert_cls = "alert-emergency" if task.get("priority") == "emergency" else "alert-important"
            st.markdown(f"""
            <div class="{alert_cls}">
                <b>{priority} {task.get('task','')}</b><br>
                <small>{task.get('detail','')[:80]}</small>
            </div>""", unsafe_allow_html=True)
    else:
        st.success("✅ 오늘 확인할 긴급/중요 항목 없음")

    st.markdown("---")

    # ── 알림 현황 ─────────────────────────────────────────────────────────────
    st.markdown("### 🔔 알림 현황")
    emergency = alerts.get("emergency", [])
    important = alerts.get("important", [])

    if emergency:
        st.markdown("**🚨 긴급**")
        for a in emergency[:3]:
            st.markdown(f"""<div class="alert-emergency"><b>{a.get('title','')}</b><br><small>{a.get('detail','')}</small></div>""", unsafe_allow_html=True)

    if important:
        st.markdown("**🟠 중요**")
        for a in important[:3]:
            st.markdown(f"""<div class="alert-important"><b>{a.get('title','')}</b><br><small>{a.get('detail','')}</small></div>""", unsafe_allow_html=True)

    if not emergency and not important:
        st.success("✅ 현재 알림 없음")

    st.markdown("---")

    # ── 활동 로그 ─────────────────────────────────────────────────────────────
    st.markdown("### 📋 최근 활동")
    if logs:
        for log in logs[:8]:
            level_emoji = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(log.get("level","info"), "ℹ️")
            time_str = log.get("time", "")[-8:] if log.get("time") else ""
            st.markdown(f"""<div class="log-item">{level_emoji} {log.get('message','')} <span style="float:right;color:#adb5bd">{time_str}</span></div>""", unsafe_allow_html=True)
    else:
        st.caption("활동 로그 없음")

st.markdown("---")

# ── 권한 현황 ─────────────────────────────────────────────────────────────────
with st.expander("🔒 권한 현황", expanded=False):
    perms = status.get("permissions", {})
    pc1, pc2 = st.columns(2)
    with pc1:
        st.markdown("**✅ 자동 실행 가능**")
        for a in perms.get("allowed", []):
            st.markdown(f"- {a}")
    with pc2:
        st.markdown("**🚫 절대 자동 실행 금지**")
        for f in perms.get("forbidden", []):
            st.markdown(f"- {f}")

# ── 자동 새로고침 ─────────────────────────────────────────────────────────────
st.markdown("---")
auto_refresh = st.checkbox("30초마다 자동 새로고침", value=False)
if auto_refresh:
    time.sleep(30)
    st.cache_data.clear()
    st.rerun()

st.markdown("---")
st.markdown("### 🏭 생산 작업 현황")
st.caption("단순 보고를 넘어 각 직원이 자율적으로 만들어내는 가치")

total_ideas = sum(len(a.get("ideas_generated", [])) for a in agents.values())
total_opps  = sum(len(a.get("opportunities_found", [])) for a in agents.values())
total_review = sum(len(a.get("owner_review_items", [])) for a in agents.values())
total_backlog = sum(len(a.get("productive_backlog", [])) for a in agents.values())

pc1, pc2, pc3, pc4 = st.columns(4)
with pc1:
    st.metric("💡 오늘 아이디어", total_ideas)
with pc2:
    st.metric("🎯 발견된 기회", total_opps)
with pc3:
    st.metric("📋 대표 검토 항목", total_review)
with pc4:
    st.metric("📝 전체 백로그", total_backlog)

st.markdown("")
prod_cols = st.columns(2)
agent_list_prod = [
    ("investment_analyst", "📈 투자 애널리스트"),
    ("auto_trader",        "🤖 퀀트 트레이더"),
    ("business_analyst",   "🏪 비즈니스 애널리스트"),
    ("marketing_creator",  "✨ 마케터 / 크리에이터"),
]

for idx, (agent_id, display_name) in enumerate(agent_list_prod):
    agent = agents.get(agent_id, {})
    col   = prod_cols[idx % 2]
    with col:
        productive = agent.get("productive_mode", False)
        current    = agent.get("current_task", "")
        required   = agent.get("required_tasks", [])
        backlog    = agent.get("productive_backlog", [])
        ideas      = agent.get("ideas_generated", [])
        opps       = agent.get("opportunities_found", [])
        review     = agent.get("owner_review_items", [])
        mode_badge = "🟢 생산 모드" if productive else "⏳ 준비 중"
        with st.expander(f"{display_name}  —  {mode_badge}", expanded=True):
            if current:
                st.markdown(f"**지금 하는 일** — {current}")
            if required:
                st.markdown("**✅ 필수 업무**")
                for r in required:
                    st.markdown(f"<div class='log-item'>• {r}</div>", unsafe_allow_html=True)
            if backlog:
                st.markdown("**📝 생산 백로그**")
                for b in backlog[:4]:
                    st.markdown(f"<div class='log-item'>→ {b}</div>", unsafe_allow_html=True)
                if len(backlog) > 4:
                    st.caption(f"외 {len(backlog)-4}개 더...")
            if ideas:
                st.markdown("**💡 오늘 아이디어**")
                for idea in ideas:
                    st.info(f"💡 {idea.get('title', '')} — {idea.get('detail', '')}")
            if opps:
                st.markdown("**🎯 발견된 기회**")
                for opp in opps:
                    st.success(f"🎯 {opp.get('title', '')} — {opp.get('detail', '')}")
            if review:
                st.markdown("**👀 대표 검토 필요**")
                for item in review:
                    st.warning(f"👀 {item.get('title', '')} — {item.get('detail', '')}")
            if not ideas and not opps and not review:
                st.caption("아직 생성된 아이디어/기회 없음 — 직원 기능 개발 후 자동 채워짐")
