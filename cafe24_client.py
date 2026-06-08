"""
cafe24_client.py — 카페24 API 연동 클라이언트
현재 상태: 연동 준비 구조 (실제 API 호출은 토큰 발급 후 진행)

원칙:
- 토큰 값은 절대 코드에 직접 넣지 않음
- 환경변수 또는 .env에서만 읽음
- 실제 API 호출은 CAFE24_ACCESS_TOKEN이 설정된 후에만 실행
"""

from dotenv import load_dotenv
load_dotenv()

import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ── 환경변수 (값은 .env 또는 GitHub Secrets에서 주입) ────────────────────────
def _get_cafe24_config() -> dict:
    return {
        "mall_id":       os.environ.get("CAFE24_MALL_ID", ""),
        "client_id":     os.environ.get("CAFE24_CLIENT_ID", ""),
        "client_secret": os.environ.get("CAFE24_CLIENT_SECRET", ""),
        "redirect_uri":  os.environ.get("CAFE24_REDIRECT_URI", ""),
        "access_token":  os.environ.get("CAFE24_ACCESS_TOKEN", ""),
        "refresh_token": os.environ.get("CAFE24_REFRESH_TOKEN", ""),
    }

def _now_kst_str() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%d %H:%M:%S KST")

# ── 연동 상태 확인 ────────────────────────────────────────────────────────────
def check_connection_status() -> dict:
    """
    카페24 연동에 필요한 환경변수 설정 여부 확인.
    실제 API 호출 없이 준비 상태만 점검.
    """
    cfg = _get_cafe24_config()
    required = ["mall_id", "client_id", "client_secret", "access_token"]
    missing  = [k for k in required if not cfg[k]]

    if not missing:
        status = "ready"
        message = "카페24 연동 준비 완료 — API 호출 가능"
    elif "access_token" in missing and len(missing) == 1:
        status = "token_needed"
        message = "CAFE24_ACCESS_TOKEN 미설정 — OAuth 인증 필요"
    elif missing:
        status = "not_configured"
        message = f"미설정 항목: {', '.join(missing)}"
    else:
        status = "unknown"
        message = "상태 확인 불가"

    return {
        "status":       status,
        "message":      message,
        "mall_id_set":  bool(cfg["mall_id"]),
        "client_set":   bool(cfg["client_id"]) and bool(cfg["client_secret"]),
        "token_set":    bool(cfg["access_token"]),
        "refresh_set":  bool(cfg["refresh_token"]),
        "checked_at":   _now_kst_str(),
    }

# ── OAuth 인증 URL 생성 ───────────────────────────────────────────────────────
def get_auth_url() -> str:
    """
    카페24 OAuth 인증 URL 생성.
    브라우저에서 열어 인증 코드 받기.
    """
    cfg = _get_cafe24_config()
    if not cfg["mall_id"] or not cfg["client_id"]:
        return "CAFE24_MALL_ID 또는 CAFE24_CLIENT_ID 미설정"

    return (
        f"https://{cfg['mall_id']}.cafe24api.com/api/v2/oauth/authorize"
        f"?response_type=code"
        f"&client_id={cfg['client_id']}"
        f"&redirect_uri={cfg['redirect_uri']}"
        f"&scope=mall.read_order,mall.read_product,mall.read_analytics"
    )

# ── 토큰 갱신 ─────────────────────────────────────────────────────────────────
def refresh_access_token() -> dict:
    """
    refresh_token으로 access_token 갱신.
    실제 토큰이 설정된 후에만 동작.
    """
    cfg = _get_cafe24_config()
    if not cfg["refresh_token"]:
        return {"success": False, "error": "CAFE24_REFRESH_TOKEN 미설정"}
    if not cfg["client_id"] or not cfg["client_secret"]:
        return {"success": False, "error": "CAFE24_CLIENT_ID/SECRET 미설정"}

    try:
        url = f"https://{cfg['mall_id']}.cafe24api.com/api/v2/oauth/token"
        resp = requests.post(url, data={
            "grant_type":    "refresh_token",
            "refresh_token": cfg["refresh_token"],
        }, auth=(cfg["client_id"], cfg["client_secret"]), timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            return {
                "success":       True,
                "access_token":  data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "expires_in":    data.get("expires_in", 0),
            }
        else:
            return {"success": False, "error": f"HTTP {resp.status_code}", "detail": resp.text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── API 요청 기본 헬퍼 ────────────────────────────────────────────────────────
def _api_get(endpoint: str, params: dict = None) -> dict:
    """
    카페24 API GET 요청 헬퍼.
    access_token이 없으면 실행하지 않음.
    """
    cfg = _get_cafe24_config()
    if not cfg["access_token"]:
        return {"success": False, "error": "ACCESS_TOKEN 미설정 — API 호출 불가"}
    if not cfg["mall_id"]:
        return {"success": False, "error": "CAFE24_MALL_ID 미설정"}

    try:
        url = f"https://{cfg['mall_id']}.cafe24api.com/api/v2{endpoint}"
        headers = {
            "Authorization":  f"Bearer {cfg['access_token']}",
            "Content-Type":   "application/json",
            "X-Cafe24-Api-Version": "2024-03-01",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return {"success": True, "data": resp.json()}
        else:
            return {"success": False, "error": f"HTTP {resp.status_code}", "detail": resp.text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── 데이터 수집 함수들 (토큰 준비 후 실제 호출) ──────────────────────────────

def get_today_orders() -> dict:
    """오늘 주문 데이터 조회"""
    from datetime import date
    today = date.today().isoformat()
    return _api_get("/orders", params={
        "start_date": today,
        "end_date":   today,
        "limit":      100,
    })

def get_products(limit: int = 50) -> dict:
    """상품 목록 조회"""
    return _api_get("/products", params={"limit": limit})

def get_sales_summary(start_date: str, end_date: str) -> dict:
    """기간별 매출 요약"""
    return _api_get("/reports/salesvolume", params={
        "start_date": start_date,
        "end_date":   end_date,
    })

def get_customers(limit: int = 50) -> dict:
    """고객 데이터 조회"""
    return _api_get("/customers", params={"limit": limit})

# ── 단독 실행 테스트 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("[cafe24_client] 연동 상태 확인")
    print("=" * 50)

    status = check_connection_status()
    print(f"\n상태: {status['status']}")
    print(f"메시지: {status['message']}")
    print(f"Mall ID 설정: {status['mall_id_set']}")
    print(f"Client 설정: {status['client_set']}")
    print(f"Access Token 설정: {status['token_set']}")
    print(f"Refresh Token 설정: {status['refresh_set']}")

    if status["status"] == "not_configured":
        print("\n[다음 단계]")
        print("1. 카페24 개발자센터(https://developers.cafe24.com)에서 앱 생성")
        print("2. .env에 아래 값 설정:")
        print("   CAFE24_MALL_ID=쇼핑몰ID")
        print("   CAFE24_CLIENT_ID=앱클라이언트ID")
        print("   CAFE24_CLIENT_SECRET=앱시크릿")
        print("   CAFE24_REDIRECT_URI=리다이렉트URI")
        print("3. OAuth 인증 후 토큰 발급")
        print(f"\n인증 URL (설정 후): {get_auth_url()}")
