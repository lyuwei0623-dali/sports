"""Administrator-only connectivity checks. No secrets or provider payloads escape."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import requests

def _check(name, url, *, key="", headers=None, params=None, needs_key=False):
    base = {"來源": name, "狀態": "無法連線", "處理方式": "稍後重試；不代表當日沒有賽事。"}
    if needs_key and not key.strip():
        return {**base, "狀態": "未設定金鑰", "處理方式": "到 Streamlit Secrets 設定此來源的金鑰。"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=12)
        try:
            body = response.json()
        except ValueError:
            body = None
        error_code = body.get("error_code") if isinstance(body, dict) else None
        if error_code == "OUT_OF_USAGE_CREDITS" or response.status_code == 429:
            return {**base, "狀態": "額度或速率受限", "處理方式": "到該來源帳戶查看剩餘額度／重設時間。"}
        if error_code == "INVALID_KEY" or response.status_code in (401,403):
            return {**base, "狀態": "認證失敗", "處理方式": "從該來源帳戶重新複製有效金鑰至 Secrets；不要填登入密碼。"}
        if not response.ok:
            return {**base, "狀態": f"來源回應 HTTP {response.status_code}"}
        if isinstance(body,dict) and body.get("errors"):
            return {**base, "狀態": "API 拒絕查詢", "處理方式": "檢查方案、額度與可用賽季；連線成功不等於資料可用。"}
        if name == "ClubElo" and ("Club,Elo" not in response.text and not all(x in response.text.splitlines()[0] for x in ("Club","Elo"))):
            return {**base, "狀態": "評分格式不符"}
        return {**base, "狀態": "連線通過", "處理方式": "僅驗證此端點；實際比賽與盤口仍需建立快照確認。"}
    except Exception:
        return base

def check_sources(odds_key, football_key, day):
    """Called solely by protected admin UI, never on ordinary app/member load."""
    jobs = [
        ("MLB 官方", "https://statsapi.mlb.com/api/v1/schedule", {"params":{"sportId":1,"date":day}}),
        ("The Odds API", "https://api.the-odds-api.com/v4/sports", {"key":odds_key,"needs_key":True,"params":{"apiKey":odds_key}}),
        ("API-Football", "https://v3.football.api-sports.io/status", {"key":football_key,"needs_key":True,"headers":{"x-apisports-key":football_key}}),
        ("ClubElo", "https://api.clubelo.com/"+datetime.now(timezone.utc).date().isoformat(), {}),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_check,name,url,**kw) for name,url,kw in jobs]
        return [f.result() for f in futures]
