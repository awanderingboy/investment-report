"""
learning_engine.py — 보유 종목 + 신규 후보 통합 자가학습 엔진 (Stage 6)
매일 판단 결과를 기록하고, 실제 가격 결과를 추적하고,
실패/성공 원인을 태그화하고, 다음 보고서에 자동 반영한다.
"""

import os
import sys
import json
import glob
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ── 경로 상수 ──────────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(_BASE, "logs")
PATTERNS_FILE = os.path.join(LOGS_DIR, "learning_patterns.json")

# ── KST 헬퍼 ─────────────────────────────────────────────────────────────────
_KST = timezone(timedelta(hours=9))

def _today_str() -> str:
    return datetime.now(tz=_KST).strftime("%Y-%m-%d")

def _today_ymd() -> str:
    return datetime.now(tz=_KST).strftime("%Y%m%d")

def _days_ago(n: int) -> str:
    return (datetime.now(tz=_KST) - timedelta(days=n)).strftime("%Y-%m-%d")

def _ymd_to_date(ymd: str):
    return datetime.strptime(ymd, "%Y%m%d").date()

# ── 태그 상수 ─────────────────────────────────────────────────────────────────
SUCCESS_TAGS = {
    "correct_reduce", "correct_hold", "correct_take_profit",
    "correct_no_add", "correct_no_buy", "correct_dca_pause",
    "correct_dca_keep", "entry_zone_valid", "risk_control_valid",
}
FAILURE_TAGS = {
    "over_reactive_reduce", "late_reduce", "early_profit_take",
    "missed_add_opportunity", "missed_candidate_opportunity",
    "bad_hold", "bad_buy_allowed", "bad_stop_logic", "bad_target_logic",
    "ignored_trigger", "inconsistent_action",
}
NEUTRAL_TAGS = {
    "risk_control_valid_but_timing_early", "blocked_correctly_due_to_risk",
    "pending_outcome", "data_missing", "market_gap_noise",
    "low_confidence_no_score",
}

REDUCE_ACTIONS = {"strong_reduce", "reduce_review"}
HOLD_ACTIONS   = {"hold", "hold_but_watch"}
PROFIT_ACTIONS = {"take_profit_review"}
DCA_KEEP_ACTIONS  = {"dca_keep"}
DCA_PAUSE_ACTIONS = {"dca_pause"}
NO_ADD_ACTIONS    = {"no_add"}

CANDIDATE_BLOCKED_REASONS = {
    "model_accuracy_below_30", "news_missing", "single_source",
    "blocked_low_model_accuracy", "blocked_news_missing",
}

# ── 파일 경로 헬퍼 ────────────────────────────────────────────────────────────

def _decision_log_path(date_str: str = None) -> str:
    ymd = date_str.replace("-", "") if date_str else _today_ymd()
    return os.path.join(LOGS_DIR, f"trade_decision_log_{ymd}.json")

def _outcomes_path(date_str: str = None) -> str:
    ymd = date_str.replace("-", "") if date_str else _today_ymd()
    return os.path.join(LOGS_DIR, f"decision_outcomes_{ymd}.json")

def _adjustments_path(date_str: str = None) -> str:
    ymd = date_str.replace("-", "") if date_str else _today_ymd()
    return os.path.join(LOGS_DIR, f"learning_adjustments_{ymd}.json")

def _ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)

# ── 60일 초과 파일 정리 ────────────────────────────────────────────────────────

def _purge_old_logs(keep_days: int = 60):
    try:
        cutoff = datetime.now(tz=_KST) - timedelta(days=keep_days)
        patterns = [
            "trade_decision_log_*.json",
            "decision_outcomes_*.json",
            "learning_adjustments_*.json",
        ]
        for pat in patterns:
            for fpath in glob.glob(os.path.join(LOGS_DIR, pat)):
                fname = os.path.basename(fpath)
                # extract YYYYMMDD
                parts = fname.replace(".json", "").split("_")
                ymd = parts[-1]
                if len(ymd) == 8 and ymd.isdigit():
                    try:
                        file_date = datetime.strptime(ymd, "%Y%m%d")
                        if file_date < cutoff:
                            os.remove(fpath)
                    except Exception:
                        pass
    except Exception:
        pass

# ── 이전 decision log 로드 ────────────────────────────────────────────────────

def load_previous_decisions(days_back: int = 1) -> list:
    """
    최근 days_back일치 decision log를 로드.
    Returns list of decision dicts (holding + candidates combined).
    """
    results = []
    for d in range(1, days_back + 1):
        date_str = _days_ago(d)
        path = _decision_log_path(date_str)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("decisions", []):
                    item["_log_date"] = date_str
                    results.append(item)
            except Exception:
                pass
    return results

# ── 오늘 판단 저장 ────────────────────────────────────────────────────────────

def save_today_decisions(holding_decisions: list, candidate_decisions: list) -> str:
    """
    오늘 보유 종목 + 신규 후보 판단을 logs/trade_decision_log_YYYYMMDD.json에 저장.
    Returns saved file path.
    """
    _ensure_logs_dir()
    _purge_old_logs(60)
    path = _decision_log_path()
    payload = {
        "date": _today_str(),
        "generated_at": datetime.now(tz=_KST).isoformat(),
        "holding_count": len(holding_decisions),
        "candidate_count": len(candidate_decisions),
        "decisions": holding_decisions + candidate_decisions,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path

# ── 판단 결과 분류 ────────────────────────────────────────────────────────────

def classify_decision_result(decision: dict, current_price: float) -> dict:
    """
    전일 decision과 오늘 current_price를 비교해 결과 태그를 반환.
    Returns {"tag": str, "detail": str, "is_success": bool, "is_failure": bool}.
    """
    prev_price = decision.get("current_price")
    action     = decision.get("action", "")
    stop_loss  = decision.get("stop_loss")
    target_1   = decision.get("target_1")
    holding    = decision.get("holding_status", True)

    # 가격 데이터 없으면 pending
    if not prev_price or not current_price or current_price <= 0:
        return {"tag": "data_missing", "detail": "가격 데이터 부족", "is_success": False, "is_failure": False}

    pct_chg = (current_price - prev_price) / prev_price * 100

    # 위험 조건 보호 여부 확인
    price_triggered = (decision.get("price_condition") or {}).get("triggered", False)
    high_concentration = "concentration" in (decision.get("risk_tag") or [])
    low_model_acc = "model_accuracy_low" in (decision.get("action_reason") or [])
    risk_protected = price_triggered or high_concentration or low_model_acc

    blocked_reasons = set(decision.get("reason") or [])
    is_blocked_correctly = bool(blocked_reasons & CANDIDATE_BLOCKED_REASONS) or risk_protected

    # ── A. 보유 종목 판단 평가 ──────────────────────────────────────────────────
    if holding:
        # A1. 축소/손절
        if action in REDUCE_ACTIONS:
            if pct_chg < -1.0:
                return {"tag": "correct_reduce",
                        "detail": f"축소 후 {pct_chg:.1f}% 추가 하락",
                        "is_success": True, "is_failure": False}
            elif pct_chg > 5.0:
                if risk_protected:
                    return {"tag": "risk_control_valid_but_timing_early",
                            "detail": f"축소 후 {pct_chg:.1f}% 반등, 단 가격조건/비중 문제로 위험통제 유효",
                            "is_success": False, "is_failure": False}
                else:
                    return {"tag": "over_reactive_reduce",
                            "detail": f"축소 후 {pct_chg:.1f}% 반등 — 과잉 반응 가능성",
                            "is_success": False, "is_failure": True}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"축소 후 {pct_chg:.1f}% 변동 — 추가 추적 필요",
                        "is_success": False, "is_failure": False}

        # A2. 보유
        elif action in HOLD_ACTIONS:
            if stop_loss and current_price < stop_loss:
                return {"tag": "bad_hold",
                        "detail": f"보유 후 손절가({stop_loss}) 이탈 ({current_price:.2f})",
                        "is_success": False, "is_failure": True}
            elif pct_chg < -5.0:
                return {"tag": "late_reduce",
                        "detail": f"보유 판단 후 {pct_chg:.1f}% 급락 — 축소 지연 가능성",
                        "is_success": False, "is_failure": True}
            elif pct_chg >= 0:
                return {"tag": "correct_hold",
                        "detail": f"보유 후 {pct_chg:.1f}% 안정/상승",
                        "is_success": True, "is_failure": False}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"보유 후 {pct_chg:.1f}% 소폭 하락 — 추가 추적 필요",
                        "is_success": False, "is_failure": False}

        # A3. 익절 검토
        elif action in PROFIT_ACTIONS:
            if pct_chg < -2.0:
                return {"tag": "correct_take_profit",
                        "detail": f"익절 후 {pct_chg:.1f}% 하락 — 익절 판단 적절",
                        "is_success": True, "is_failure": False}
            elif pct_chg > 10.0:
                return {"tag": "early_profit_take",
                        "detail": f"익절 후 {pct_chg:.1f}% 추가 상승 — 익절 기준 너무 빨랐을 가능성",
                        "is_success": False, "is_failure": True}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"익절 후 {pct_chg:.1f}% 변동 — 추적 중",
                        "is_success": False, "is_failure": False}

        # A4. 추매금지
        elif action in NO_ADD_ACTIONS:
            if pct_chg < -2.0:
                return {"tag": "correct_no_add",
                        "detail": f"추매금지 후 {pct_chg:.1f}% 하락 — 판단 적절",
                        "is_success": True, "is_failure": False}
            elif pct_chg > 3.0:
                if is_blocked_correctly:
                    return {"tag": "blocked_correctly_due_to_risk",
                            "detail": f"추매금지 후 {pct_chg:.1f}% 상승, 단 위험 요인으로 차단 유효",
                            "is_success": False, "is_failure": False}
                else:
                    return {"tag": "missed_add_opportunity",
                            "detail": f"추매금지 후 {pct_chg:.1f}% 상승 — 기회 놓침 가능성",
                            "is_success": False, "is_failure": True}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"추매금지 후 {pct_chg:.1f}% 변동",
                        "is_success": False, "is_failure": False}

        # A5. DCA 유지
        elif action in DCA_KEEP_ACTIONS:
            if pct_chg >= 0:
                return {"tag": "correct_dca_keep",
                        "detail": f"적립 유지 후 {pct_chg:.1f}% 안정/상승",
                        "is_success": True, "is_failure": False}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"적립 유지 후 {pct_chg:.1f}% 하락 — 추적 중",
                        "is_success": False, "is_failure": False}

        # A6. DCA 보류
        elif action in DCA_PAUSE_ACTIONS:
            if pct_chg < -2.0:
                return {"tag": "correct_dca_pause",
                        "detail": f"적립 보류 후 {pct_chg:.1f}% 하락 — 판단 적절",
                        "is_success": True, "is_failure": False}
            elif pct_chg > 5.0:
                if low_model_acc:
                    return {"tag": "blocked_correctly_due_to_risk",
                            "detail": f"적립 보류 후 상승, 단 모델 정확도 낮음으로 방어 판단 유효",
                            "is_success": False, "is_failure": False}
                else:
                    return {"tag": "missed_dca_opportunity",
                            "detail": f"적립 보류 후 {pct_chg:.1f}% 상승 — 적립 기회 놓침",
                            "is_success": False, "is_failure": True}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"적립 보류 후 {pct_chg:.1f}% 변동",
                        "is_success": False, "is_failure": False}

        else:
            return {"tag": "pending_outcome",
                    "detail": f"action={action} 평가 중",
                    "is_success": False, "is_failure": False}

    # ── B. 신규 후보 판단 평가 ──────────────────────────────────────────────────
    else:
        candidate_action = action  # observe_only, watch_entry_zone, etc.
        entry_zone_lo = None
        stop = decision.get("stop_loss")

        if stop and current_price < stop:
            return {"tag": "candidate_risk_confirmed",
                    "detail": f"손절가({stop:.2f}) 이탈 — 후보 위험 확인됨",
                    "is_success": True, "is_failure": False}

        if candidate_action in ("observe_only", "blocked_news_missing",
                                "blocked_low_model_accuracy", "blocked_volume_low",
                                "blocked_rr_low"):
            if pct_chg > 3.0:
                if is_blocked_correctly:
                    return {"tag": "blocked_correctly_due_to_risk",
                            "detail": f"관찰 후 {pct_chg:.1f}% 상승, 단 뉴스 미수집/정확도 낮음으로 차단 유효",
                            "is_success": False, "is_failure": False}
                else:
                    return {"tag": "missed_candidate_opportunity",
                            "detail": f"관찰 후 {pct_chg:.1f}% 상승 — 진입 기회 놓침",
                            "is_success": False, "is_failure": True}
            elif pct_chg < -2.0:
                return {"tag": "correct_no_buy",
                        "detail": f"관찰 후 {pct_chg:.1f}% 하락 — 매수 안 한 것이 맞음",
                        "is_success": True, "is_failure": False}
            else:
                return {"tag": "pending_outcome",
                        "detail": f"관찰 후 {pct_chg:.1f}% 변동",
                        "is_success": False, "is_failure": False}

        return {"tag": "pending_outcome",
                "detail": f"후보 action={candidate_action} 평가 중",
                "is_success": False, "is_failure": False}


# ── 과거 decision outcome 평가 ────────────────────────────────────────────────

def evaluate_decision_outcomes(current_market_data: dict, days_back: int = 1) -> list:
    """
    최근 days_back일 전 판단을 오늘 시장 데이터로 평가.
    Returns list of outcome dicts.
    """
    prev_decisions = load_previous_decisions(days_back)
    outcomes = []

    for decision in prev_decisions:
        ticker = decision.get("ticker", "")
        if not ticker:
            continue

        current_data = current_market_data.get(ticker, {})
        current_price = None

        # 현재가 추출 (투자보고서 형식 또는 직접 float)
        raw = current_data.get("현재가") or current_data.get("current_price")
        try:
            current_price = float(raw) if raw else None
        except (TypeError, ValueError):
            current_price = None

        result = classify_decision_result(decision, current_price)

        outcome = {
            "date_evaluated": _today_str(),
            "original_date": decision.get("_log_date", decision.get("date", "")),
            "ticker": ticker,
            "name": decision.get("name", ""),
            "holding_status": decision.get("holding_status", True),
            "action": decision.get("action", ""),
            "prev_price": decision.get("current_price"),
            "current_price": current_price,
            "price_change_pct": round(
                (current_price - decision.get("current_price", 0)) / decision.get("current_price", 1) * 100, 2
            ) if current_price and decision.get("current_price") else None,
            "tag": result["tag"],
            "detail": result["detail"],
            "is_success": result["is_success"],
            "is_failure": result["is_failure"],
        }
        outcomes.append(outcome)

    return outcomes


# ── 패턴 집계 및 learning_patterns.json 업데이트 ──────────────────────────────

def _load_patterns() -> dict:
    if os.path.exists(PATTERNS_FILE):
        try:
            with open(PATTERNS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": "", "tag_history": [], "ticker_patterns": {}, "global_patterns": {}}


def _save_patterns(patterns: dict):
    _ensure_logs_dir()
    patterns["updated_at"] = _today_str()
    with open(PATTERNS_FILE, "w", encoding="utf-8") as f:
        json.dump(patterns, f, ensure_ascii=False, indent=2)


def _update_patterns(outcomes: list) -> dict:
    patterns = _load_patterns()
    today = _today_str()

    # 오늘 날짜 태그 항목 추가
    for o in outcomes:
        tag = o.get("tag")
        if not tag or tag in ("pending_outcome", "data_missing"):
            continue
        patterns["tag_history"].append({
            "date": today,
            "ticker": o.get("ticker"),
            "tag": tag,
            "holding": o.get("holding_status"),
        })

    # 60일 초과 항목 정리
    cutoff = (datetime.now(tz=_KST) - timedelta(days=60)).strftime("%Y-%m-%d")
    patterns["tag_history"] = [
        h for h in patterns["tag_history"] if h.get("date", "") >= cutoff
    ]

    # 종목별 패턴 집계
    ticker_tags = {}
    for h in patterns["tag_history"]:
        t = h.get("ticker", "")
        tag = h.get("tag", "")
        if t not in ticker_tags:
            ticker_tags[t] = []
        ticker_tags[t].append(tag)
    patterns["ticker_patterns"] = ticker_tags

    # 전체 태그 빈도 (최근 5일)
    recent_cutoff = (datetime.now(tz=_KST) - timedelta(days=5)).strftime("%Y-%m-%d")
    recent = [h for h in patterns["tag_history"] if h.get("date", "") >= recent_cutoff]
    tag_freq = {}
    for h in recent:
        tag = h.get("tag", "")
        tag_freq[tag] = tag_freq.get(tag, 0) + 1
    patterns["global_patterns"] = tag_freq

    _save_patterns(patterns)
    return patterns


# ── 학습 조정값 생성 ───────────────────────────────────────────────────────────

def build_learning_adjustments(patterns: dict) -> list:
    """
    패턴 분석 결과를 바탕으로 다음 보고서에 반영할 조정값 리스트 반환.
    """
    adjustments = []
    global_pat = patterns.get("global_patterns", {})
    ticker_pat = patterns.get("ticker_patterns", {})

    # 규칙 1: over_reactive_reduce >= 3회 → 축소 완화
    if global_pat.get("over_reactive_reduce", 0) >= 3:
        adjustments.append({
            "type": "global",
            "pattern": "over_reactive_reduce",
            "count": global_pat["over_reactive_reduce"],
            "action_adjustment": "reduce_to_partial_reduce",
            "message": "최근 축소 후 반등 패턴 반복 — '즉시 축소' 대신 '분할 축소/종가 확인' 권장",
            "score_delta": 0,
        })

    # 규칙 2: late_reduce >= 3회 → 손절 강화
    if global_pat.get("late_reduce", 0) >= 3:
        adjustments.append({
            "type": "global",
            "pattern": "late_reduce",
            "count": global_pat["late_reduce"],
            "action_adjustment": "hold_to_reduce_review",
            "message": "최근 손절 지연 패턴 반복 — 가격조건 발동 시 액션 강도 강화 권장",
            "score_delta": -5,
        })

    # 규칙 3: early_profit_take >= 3회 → 익절 기준 상향
    if global_pat.get("early_profit_take", 0) >= 3:
        adjustments.append({
            "type": "global",
            "pattern": "early_profit_take",
            "count": global_pat["early_profit_take"],
            "action_adjustment": "raise_profit_target",
            "message": "최근 익절 기준이 너무 빨랐음 — 목표가 상향 또는 트레일링 스탑 적용 권장",
            "score_delta": 0,
        })

    # 규칙 4: missed_candidate_opportunity >= 3회 → 우선 관찰 승격
    if global_pat.get("missed_candidate_opportunity", 0) >= 3:
        adjustments.append({
            "type": "global",
            "pattern": "missed_candidate_opportunity",
            "count": global_pat["missed_candidate_opportunity"],
            "action_adjustment": "elevate_high_quality_observe",
            "message": "C등급 거래량1.3x+RR2.0+ 후보를 우선 관찰로 승격 — 단, 매수금액 0원 유지",
            "score_delta": 0,
        })

    # 규칙 5: bad_hold >= 3회 → 보유 판단 감점
    if global_pat.get("bad_hold", 0) >= 3:
        adjustments.append({
            "type": "global",
            "pattern": "bad_hold",
            "count": global_pat["bad_hold"],
            "action_adjustment": "penalize_hold_bias",
            "message": "최근 보유 판단 후 손절 이탈 반복 — 보유 판단 점수 감점",
            "score_delta": -7,
        })

    # 규칙 6: 종목별 — 특정 종목 over_reactive_reduce 반복
    for ticker, tags in ticker_pat.items():
        recent_tags = tags[-10:] if len(tags) > 10 else tags
        if recent_tags.count("over_reactive_reduce") >= 2:
            adjustments.append({
                "type": "ticker",
                "ticker": ticker,
                "pattern": "over_reactive_reduce",
                "count": recent_tags.count("over_reactive_reduce"),
                "action_adjustment": "add_close_confirmation",
                "message": f"{ticker}: 손절 후 반등 반복 — '종가 이탈 확인' 조건 추가",
                "score_delta": 0,
            })
        if recent_tags.count("late_reduce") >= 2:
            adjustments.append({
                "type": "ticker",
                "ticker": ticker,
                "pattern": "late_reduce",
                "count": recent_tags.count("late_reduce"),
                "action_adjustment": "strengthen_trigger_response",
                "message": f"{ticker}: 손절 지연 반복 — 가격조건 발동 시 즉시 강도 강화",
                "score_delta": -5,
            })

    return adjustments


# ── 보고서용 학습 요약 생성 ───────────────────────────────────────────────────

def get_learning_summary_for_report(
    outcomes: list,
    adjustments: list,
    prev_decisions: list,
) -> str:
    """
    보고서에 삽입할 '자가학습 반영 요약' 섹션 텍스트 생성.
    핵심 5~8개만 출력, 전체 로그는 파일에 저장.
    """
    lines = []
    lines.append("")
    lines.append("🧠 자가학습 반영 요약")
    lines.append("")

    # 표 1: 통계
    total_prev = len(prev_decisions)
    holding_prev = sum(1 for d in prev_decisions if d.get("holding_status", True))
    candidate_prev = total_prev - holding_prev

    evaluated = [o for o in outcomes if o.get("tag") not in ("pending_outcome", "data_missing")]
    pending   = [o for o in outcomes if o.get("tag") in ("pending_outcome", "data_missing")]
    successes = [o for o in evaluated if o.get("is_success")]
    failures  = [o for o in evaluated if o.get("is_failure")]

    lines.append("| 구분 | 값 | 판단 |")
    lines.append("|------|-----|------|")
    lines.append(f"| 전일 보유종목 판단 수 | {holding_prev} | {'정상' if holding_prev > 0 else '기록 없음'} |")
    lines.append(f"| 전일 신규후보 판단 수 | {candidate_prev} | {'정상' if candidate_prev > 0 else '기록 없음'} |")
    lines.append(f"| 평가 완료 수 | {len(evaluated)} | — |")
    lines.append(f"| 성공 수 | {len(successes)} | {'양호' if len(successes) >= len(failures) else '주의'} |")
    lines.append(f"| 실패 수 | {len(failures)} | {'⚠️ 개선 필요' if failures else '정상'} |")
    lines.append(f"| 평가 보류 수 | {len(pending)} | 1일 이상 추적 필요 |")
    lines.append(f"| 오늘 반영된 패턴 수 | {len(adjustments)} | {'패턴 조정 적용됨' if adjustments else '패턴 없음'} |")
    lines.append("")

    # 표 2: 종목별 결과 (최대 8개)
    if evaluated:
        lines.append("| 종목 | 어제 판단 | 오늘 결과 | 학습 태그 | 오늘 반영 |")
        lines.append("|------|---------|---------|---------|---------|")
        for o in evaluated[:8]:
            ticker = o.get("ticker", "?")
            action = o.get("action", "?")
            price_chg = o.get("price_change_pct")
            chg_str = f"{price_chg:+.1f}%" if price_chg is not None else "?"
            tag = o.get("tag", "?")
            adj_for_ticker = next(
                (a for a in adjustments if a.get("ticker") == ticker), None
            )
            adj_msg = adj_for_ticker.get("action_adjustment", "-") if adj_for_ticker else "-"
            lines.append(f"| {ticker} | {action} | {chg_str} | {tag} | {adj_msg} |")
        lines.append("")

    # 표 3: 패턴 조정 (최대 5개)
    if adjustments:
        lines.append("| 패턴 | 최근 발생 횟수 | 오늘 조정 | 반영 대상 |")
        lines.append("|------|------------|---------|---------|")
        for adj in adjustments[:5]:
            pat = adj.get("pattern", "?")
            cnt = adj.get("count", 0)
            action_adj = adj.get("action_adjustment", "?")
            target = adj.get("ticker", "전체") if adj.get("type") == "ticker" else "전체"
            lines.append(f"| {pat} | {cnt}회 | {action_adj} | {target} |")
        lines.append("")

    # 실패 패턴 경고
    failure_tags = [o.get("tag") for o in failures]
    if failure_tags:
        lines.append("⚠️ 반복 실패 패턴:")
        from collections import Counter
        for tag, cnt in Counter(failure_tags).most_common(3):
            lines.append(f"  - {tag}: {cnt}회")
        lines.append("")

    # 평가 대기
    if pending:
        pending_tickers = [o.get("ticker") for o in pending[:5]]
        lines.append(f"평가 대기 중: {', '.join(pending_tickers)}" + (
            f" 외 {len(pending)-5}개" if len(pending) > 5 else ""
        ))

    return "\n".join(lines)


# ── consistency check ─────────────────────────────────────────────────────────

def run_consistency_check(
    outcomes: list,
    prev_decisions: list,
    today_decisions: list,
    price_trigger_results: list = None,
) -> list:
    """
    보고서 일관성 검증. 문제 발견 시 경고 리스트 반환.
    """
    issues = []

    prev_map = {d.get("ticker"): d for d in prev_decisions}
    today_map = {d.get("ticker"): d for d in today_decisions}

    for ticker, today_dec in today_map.items():
        prev_dec = prev_map.get(ticker)
        if not prev_dec:
            continue

        prev_action = prev_dec.get("action", "")
        today_action = today_dec.get("action", "")
        change_reason = today_dec.get("action_change_reason", [])

        # 규칙 1: 어제 축소→오늘 보유, 이유 없음
        if prev_action in REDUCE_ACTIONS and today_action in HOLD_ACTIONS:
            if not change_reason:
                issues.append({
                    "type": "inconsistent_action",
                    "ticker": ticker,
                    "detail": f"{ticker}: 어제 {prev_action} → 오늘 {today_action}, 변경 이유 없음",
                })

        # 규칙 2: 어제 보유→오늘 축소, 이유 없음
        if prev_action in HOLD_ACTIONS and today_action in REDUCE_ACTIONS:
            if not change_reason:
                issues.append({
                    "type": "inconsistent_action",
                    "ticker": ticker,
                    "detail": f"{ticker}: 어제 {prev_action} → 오늘 {today_action}, 변경 이유 없음",
                })

    # 규칙 3: C등급 후보에 매수금액 > 0
    for dec in today_decisions:
        if not dec.get("holding_status", True):
            grade = dec.get("grade", "")
            buy_amt = dec.get("recommended_buy_amount", 0) or 0
            if grade == "C" and buy_amt > 0:
                issues.append({
                    "type": "grade_c_buy_amount",
                    "ticker": dec.get("ticker"),
                    "detail": f"{dec.get('ticker')}: C등급인데 매수금액 {buy_amt}원 설정됨",
                })

    # 규칙 4: 가격조건 발동 종목 중 오늘 "정상 보유"로만 표시
    if price_trigger_results:
        triggered = [r.get("symbol") for r in price_trigger_results if r.get("status") == "TRIGGERED"]
        for ticker in triggered:
            dec = today_map.get(ticker)
            if dec and dec.get("action") == "hold":
                issues.append({
                    "type": "ignored_trigger",
                    "ticker": ticker,
                    "detail": f"{ticker}: 가격조건 발동 중인데 오늘 액션이 'hold'만으로 표시됨",
                })

    return issues


# ── 오늘 판단 추출 (보유 종목) ────────────────────────────────────────────────

def extract_holding_decisions(
    portfolio: dict,
    all_stock_data: dict,
    tde_results: list,
    price_trigger_results: list,
    prev_decisions: list,
    model_accuracy: float = None,
) -> list:
    """
    TDE 결과와 포트폴리오를 기반으로 오늘 보유 종목 판단 로그 생성.
    """
    decisions = []
    today = _today_str()

    tde_map = {t.get("symbol"): t for t in (tde_results or [])}
    prev_map = {d.get("ticker"): d for d in prev_decisions}
    trigger_map = {r.get("symbol"): r for r in (price_trigger_results or [])}

    for cat in ["category1", "category2"]:
        for item in portfolio.get(cat, []):
            ticker = item.get("ticker", "")
            name   = item.get("name", "")
            avg_p  = float(item.get("avg_price", 0))
            shares = float(item.get("shares", 0))
            curr   = float(item.get("currency") == "KRW" and 1 or 1)

            stock = all_stock_data.get(ticker, {})
            cp = None
            try:
                cp = float(stock.get("현재가") or 0) or None
            except Exception:
                pass

            position_val = round(cp * shares, 2) if cp else None
            unreal = round((cp - avg_p) / avg_p * 100, 2) if cp and avg_p > 0 else None

            tde = tde_map.get(ticker, {})
            buy_grade = (tde.get("trade_eligibility") or {}).get("buy_grade", "")
            tde_reasons = (tde.get("data_status") or {}).get("notes", [])

            trigger = trigger_map.get(ticker, {})
            triggered = trigger.get("status") == "TRIGGERED"

            # 액션 추론
            if buy_grade == "D":
                if triggered:
                    action = "strong_reduce"
                else:
                    action = "reduce_review"
            elif buy_grade == "C":
                if triggered:
                    action = "hold_but_watch"
                else:
                    action = "hold"
            else:
                if cat == "category1":
                    action = "dca_keep" if (tde.get("trade_eligibility") or {}).get("buy_type", {}).get("auto_dca_allowed") else "dca_pause"
                else:
                    action = "hold"

            # 모델 정확도 낮으면 dca_pause
            if model_accuracy is not None and model_accuracy < 30 and action == "dca_keep":
                action = "dca_pause"

            # 전일 대비 변경 이유
            prev_dec = prev_map.get(ticker, {})
            prev_action = prev_dec.get("action", "")
            change_reason = []
            if prev_action and prev_action != action:
                if triggered:
                    change_reason.append("가격조건 발동")
                if cp and prev_dec.get("current_price") and cp < prev_dec["current_price"] * 0.95:
                    change_reason.append("전일 대비 -5% 이상 하락")
                if cp and prev_dec.get("current_price") and cp > prev_dec["current_price"] * 1.05:
                    change_reason.append("전일 대비 +5% 이상 상승")

            decisions.append({
                "date": today,
                "ticker": ticker,
                "name": name,
                "holding_status": True,
                "position_value": position_val,
                "avg_price": avg_p,
                "current_price": cp,
                "unrealized_return_pct": unreal,
                "action": action,
                "action_strength": "strong" if buy_grade == "D" else "normal",
                "action_reason": tde_reasons[:4] if tde_reasons else [],
                "price_condition": {
                    "triggered": triggered,
                    "trigger_price": (trigger.get("price_level")),
                    "trigger_type": "breakdown" if triggered else None,
                },
                "forbidden_action": ["추매", "물타기"] if buy_grade == "D" else [],
                "action_change_reason": change_reason,
                "report_section": "holding_management",
            })

    return decisions


# ── 오늘 판단 추출 (신규 후보) ────────────────────────────────────────────────

def extract_candidate_decisions(screener_data: dict, model_accuracy: float = None) -> list:
    """
    스크리너 결과에서 오늘 신규 후보 판단 로그 생성.
    """
    decisions = []
    today = _today_str()
    if not screener_data:
        return decisions

    for i, c in enumerate(screener_data.get("top_candidates", []), 1):
        ps = c.get("price_strategy") or {}
        news_missing = c.get("_news_missing", True)
        single_source = not (c.get("data_sources") and len(c["data_sources"]) > 1)

        reasons = []
        if (model_accuracy is not None and model_accuracy < 30) or model_accuracy is None:
            reasons.append("model_accuracy_below_30")
        if c.get("grade") == "C":
            reasons.append("grade_C")
        if news_missing:
            reasons.append("news_missing")
        if single_source:
            reasons.append("single_source")

        action = "observe_only"
        if c.get("_sharp_drop_flag"):
            action = "excluded"
        elif news_missing:
            action = "blocked_news_missing"
        elif model_accuracy is not None and model_accuracy < 30:
            action = "blocked_low_model_accuracy"
        elif (c.get("volume_ratio") or 0) < 1.0:
            action = "blocked_volume_low"

        decisions.append({
            "date": today,
            "ticker": c.get("ticker", ""),
            "name": c.get("name", ""),
            "holding_status": False,
            "candidate_rank": i,
            "grade": c.get("grade", "?"),
            "score": c.get("total_score", 0),
            "current_price": c.get("current_price"),
            "action": action,
            "recommended_buy_amount": ps.get("recommended_buy_amount", 0),
            "observe_entry_zone": ps.get("observe_entry_zone", ""),
            "stop_loss": ps.get("stop_loss"),
            "target_1": ps.get("target_1"),
            "target_2": ps.get("target_2"),
            "rr": ps.get("calculated_rr"),
            "volume_ratio": c.get("volume_ratio"),
            "news_status": "missing" if news_missing else "available",
            "data_source_count": 1 if single_source else 2,
            "buy_allowed": ps.get("today_buy_allowed", False),
            "reason": reasons,
            "expected_1d_direction": "unknown",
            "expected_5d_direction": "up_if_conditions_met",
            "expected_20d_direction": "up_if_conditions_met",
        })

    return decisions


# ── 메인 학습 사이클 ──────────────────────────────────────────────────────────

def run_learning_cycle(
    portfolio: dict = None,
    all_stock_data: dict = None,
    tde_results: list = None,
    price_trigger_results: list = None,
    screener_data: dict = None,
    model_accuracy: float = None,
) -> dict:
    """
    전체 학습 사이클 실행.
    Returns {
        "outcomes": list,
        "adjustments": list,
        "patterns": dict,
        "consistency_issues": list,
        "summary_text": str,
        "decision_log_path": str,
        "outcomes_path": str,
        "adjustments_path": str,
        "error": str or None,
    }
    """
    try:
        _ensure_logs_dir()

        # 1. 이전 판단 로드
        prev_decisions = load_previous_decisions(days_back=1)

        # 2. 오늘 판단 추출
        holding_dec = extract_holding_decisions(
            portfolio or {},
            all_stock_data or {},
            tde_results or [],
            price_trigger_results or [],
            prev_decisions,
            model_accuracy,
        )
        candidate_dec = extract_candidate_decisions(screener_data, model_accuracy)

        # 3. 오늘 판단 저장
        dec_path = save_today_decisions(holding_dec, candidate_dec)

        # 4. 과거 판단 결과 평가
        outcomes = evaluate_decision_outcomes(all_stock_data or {}, days_back=1)

        # 5. 패턴 업데이트
        patterns = _update_patterns(outcomes)

        # 6. 조정값 생성
        adjustments = build_learning_adjustments(patterns)

        # 7. 조정값 저장
        adj_path = _adjustments_path()
        with open(adj_path, "w", encoding="utf-8") as f:
            json.dump({
                "date": _today_str(),
                "adjustments": adjustments,
            }, f, ensure_ascii=False, indent=2)

        # 8. outcome 저장
        out_path = _outcomes_path()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "date": _today_str(),
                "outcomes": outcomes,
            }, f, ensure_ascii=False, indent=2)

        # 9. consistency check
        all_today = holding_dec + candidate_dec
        consistency_issues = run_consistency_check(
            outcomes, prev_decisions, all_today, price_trigger_results
        )

        # 10. 보고서용 요약 텍스트
        summary_text = get_learning_summary_for_report(
            outcomes, adjustments, prev_decisions
        )

        return {
            "outcomes": outcomes,
            "adjustments": adjustments,
            "patterns": patterns,
            "consistency_issues": consistency_issues,
            "summary_text": summary_text,
            "decision_log_path": dec_path,
            "outcomes_path": out_path,
            "adjustments_path": adj_path,
            "error": None,
        }

    except Exception as e:
        return {
            "outcomes": [],
            "adjustments": [],
            "patterns": {},
            "consistency_issues": [],
            "summary_text": "\n🧠 자가학습 반영 요약\n\n자가학습 데이터 미반영 — 오류 발생\n",
            "decision_log_path": None,
            "outcomes_path": None,
            "adjustments_path": None,
            "error": str(e),
        }


# ── Mock Tests ────────────────────────────────────────────────────────────────

def _run_mock_tests() -> bool:
    results = []

    def check(name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    print("\n=== learning_engine.py mock tests ===\n")

    # 공통 기반 판단
    def _make_holding(action, cp, stop=None, target=None, risk_tags=None,
                      action_reason=None, price_triggered=False):
        return {
            "holding_status": True,
            "ticker": "TEST_H",
            "name": "테스트보유",
            "action": action,
            "current_price": cp,
            "stop_loss": stop,
            "target_1": target,
            "risk_tag": risk_tags or [],
            "action_reason": action_reason or [],
            "price_condition": {"triggered": price_triggered, "trigger_price": None, "trigger_type": None},
            "forbidden_action": [],
        }

    def _make_candidate(action, cp, grade="C", news_missing=True, single_source=True,
                        model_low=True, stop=None):
        reasons = []
        if model_low: reasons.append("model_accuracy_below_30")
        if news_missing: reasons.append("news_missing")
        if single_source: reasons.append("single_source")
        return {
            "holding_status": False,
            "ticker": "TEST_C",
            "name": "테스트후보",
            "action": action,
            "current_price": cp,
            "grade": grade,
            "stop_loss": stop,
            "reason": reasons,
        }

    # T1: reduce_review + 하락 → correct_reduce
    try:
        dec = _make_holding("reduce_review", 100.0, stop=90.0)
        r = classify_decision_result(dec, 96.0)
        check("T1: reduce_review + 하락 → correct_reduce",
              r["tag"] == "correct_reduce", f"tag={r['tag']}")
    except Exception as e:
        check("T1", False, str(e))

    # T2: reduce_review + +7% 반등 → over_reactive_reduce 또는 risk_control_valid_but_timing_early
    try:
        dec = _make_holding("reduce_review", 100.0, stop=90.0)
        r = classify_decision_result(dec, 107.0)
        check("T2: reduce_review + +7% 반등 → over_reactive 또는 timing_early",
              r["tag"] in ("over_reactive_reduce", "risk_control_valid_but_timing_early"),
              f"tag={r['tag']}")
    except Exception as e:
        check("T2", False, str(e))

    # T3: hold + 손절가 이탈 → bad_hold 또는 late_reduce
    try:
        dec = _make_holding("hold", 100.0, stop=90.0)
        r = classify_decision_result(dec, 88.0)
        check("T3: hold + 손절가 이탈 → bad_hold 또는 late_reduce",
              r["tag"] in ("bad_hold", "late_reduce"), f"tag={r['tag']}")
    except Exception as e:
        check("T3", False, str(e))

    # T4: take_profit_review + 하락 → correct_take_profit
    try:
        dec = _make_holding("take_profit_review", 100.0)
        r = classify_decision_result(dec, 96.0)
        check("T4: take_profit_review + 하락 → correct_take_profit",
              r["tag"] == "correct_take_profit", f"tag={r['tag']}")
    except Exception as e:
        check("T4", False, str(e))

    # T5: take_profit_review + +10% → early_profit_take
    try:
        dec = _make_holding("take_profit_review", 100.0)
        r = classify_decision_result(dec, 111.0)
        check("T5: take_profit_review + +10% → early_profit_take",
              r["tag"] == "early_profit_take", f"tag={r['tag']}")
    except Exception as e:
        check("T5", False, str(e))

    # T6: no_add + 하락 → correct_no_add
    try:
        dec = _make_holding("no_add", 100.0)
        r = classify_decision_result(dec, 96.0)
        check("T6: no_add + 하락 → correct_no_add",
              r["tag"] == "correct_no_add", f"tag={r['tag']}")
    except Exception as e:
        check("T6", False, str(e))

    # T7: no_add + 상승 + 모델정확도 낮음 → blocked_correctly_due_to_risk
    try:
        dec = _make_holding("no_add", 100.0, action_reason=["model_accuracy_low"])
        r = classify_decision_result(dec, 106.0)
        check("T7: no_add + 상승 + 모델정확도 낮음 → blocked_correctly_due_to_risk",
              r["tag"] == "blocked_correctly_due_to_risk", f"tag={r['tag']}")
    except Exception as e:
        check("T7", False, str(e))

    # T8: observe_only + 상승 + 위험 차단 → blocked_correctly_due_to_risk
    try:
        dec = _make_candidate("observe_only", 100.0, model_low=True, news_missing=True)
        r = classify_decision_result(dec, 105.0)
        check("T8: observe_only + 상승 + 위험차단 → blocked_correctly_due_to_risk",
              r["tag"] == "blocked_correctly_due_to_risk", f"tag={r['tag']}")
    except Exception as e:
        check("T8", False, str(e))

    # T9: observe_only + 하락 → correct_no_buy
    try:
        dec = _make_candidate("observe_only", 100.0)
        r = classify_decision_result(dec, 96.0)
        check("T9: observe_only + 하락 → correct_no_buy",
              r["tag"] == "correct_no_buy", f"tag={r['tag']}")
    except Exception as e:
        check("T9", False, str(e))

    # T10: consistency — 가격조건 발동 + 오늘 hold → ignored_trigger
    try:
        today_dec = [{
            "ticker": "068760.KS", "holding_status": True, "action": "hold",
            "action_change_reason": [],
        }]
        price_triggers = [{"symbol": "068760.KS", "status": "TRIGGERED"}]
        issues = run_consistency_check([], [], today_dec, price_triggers)
        found = any(i.get("type") == "ignored_trigger" for i in issues)
        check("T10: 가격조건 발동 + hold → ignored_trigger",
              found, f"issues={[i['type'] for i in issues]}")
    except Exception as e:
        check("T10", False, str(e))

    # T11: consistency — 어제 축소→오늘 보유, 이유 없음 → inconsistent_action
    try:
        prev_dec = [{"ticker": "RGTI", "holding_status": True, "action": "strong_reduce"}]
        today_dec = [{"ticker": "RGTI", "holding_status": True, "action": "hold",
                      "action_change_reason": []}]
        issues = run_consistency_check([], prev_dec, today_dec, [])
        found = any(i.get("type") == "inconsistent_action" for i in issues)
        check("T11: 어제 축소→오늘 보유 이유 없음 → inconsistent_action",
              found, f"issues={[i['type'] for i in issues]}")
    except Exception as e:
        check("T11", False, str(e))

    # T12: learning adjustment 생성
    try:
        patterns = {
            "global_patterns": {"over_reactive_reduce": 4, "late_reduce": 3},
            "ticker_patterns": {},
            "tag_history": [],
        }
        adjs = build_learning_adjustments(patterns)
        has_adj = len(adjs) >= 2
        check("T12: adjustment 생성 확인", has_adj,
              f"adjustments={[a['pattern'] for a in adjs]}")
    except Exception as e:
        check("T12", False, str(e))

    # T13: logs 저장 확인
    try:
        _ensure_logs_dir()
        path = save_today_decisions(
            [{"ticker": "TEST_HOLD", "holding_status": True, "action": "hold", "date": _today_str()}],
            [{"ticker": "TEST_CAND", "holding_status": False, "action": "observe_only", "date": _today_str()}]
        )
        exists = os.path.exists(path)
        check("T13: decision log 저장 확인", exists, f"path={path}")
    except Exception as e:
        check("T13", False, str(e))

    # T14: learning_engine 오류 시 보고서 생성 중단 없음
    try:
        result = run_learning_cycle(
            portfolio=None, all_stock_data=None,
            tde_results=None, price_trigger_results=None,
            screener_data=None, model_accuracy=18.0,
        )
        no_crash = True
        check("T14: learning_engine 오류 시 중단 없음",
              no_crash, f"error={result.get('error')}")
    except Exception as e:
        check("T14", False, f"crash: {e}")

    total = len(results)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = [(n, d) for n, s, d in results if s == "FAIL"]
    print(f"\n=== 결과: {passed}/{total} PASS ===")
    if failed:
        print("실패한 테스트:")
        for n, d in failed:
            print(f"  FAIL: {n} — {d}")
    return passed == total


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--mock-test" in sys.argv:
        ok = _run_mock_tests()
        sys.exit(0 if ok else 1)
    print("Usage: python3 learning_engine.py --mock-test")
