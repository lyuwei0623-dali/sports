"""Core-only safe admin presentation for snapshot and publish operations.

This module intentionally renders only a fixed allow-list of operational
metadata. It never serialises the result object or displays raw error text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SAFE_FAILURE_MESSAGE = "快照建立或發布失敗，請確認資料連線與設定後重試。"
SAFE_FAILURE_BY_CODE = {
    "no_games": "所選台灣日期沒有 MLB 賽事。",
    "schedule_connection_failed": "MLB 官方賽程連線或逾時失敗，請稍後再試。",
    "schedule_auth_failed": "MLB 官方賽程存取被拒絕。",
    "schedule_quota_failed": "MLB 官方賽程請求受到速率限制。",
    "schedule_format_failed": "MLB 官方賽程回應格式不符。",
    "no_events": "當天沒有從 API-Football 或 ESPN 取得可用賽程。",
    "external_api_authentication_error": "Football 資料來源金鑰未設定、無效或額度受限；系統已嘗試 ESPN 備援。",
    "league_season_configuration_error": "Football 聯賽賽季設定不正確。",
    "no_usable_markets": "已取得賽程，但沒有取得可用盤口；完整賽程仍會保留並標示 PASS。",
    "external_api_error": "Football 外部資料來源暫時無法連線。",
    "api_failed": "MLB 官方賽程暫時無法取得。",
    "processing_failed": "已取得資料，但推薦運算未能完成。",
    "storage_failed": "推薦運算完成，但快照未能儲存。",
}


@dataclass(frozen=True)
class SnapshotSummary:
    sport: str
    success: bool
    updated_at: str
    event_count: str
    snapshot_kind: str
    member_available: str
    message: str


def summarise_snapshot_result(result: Mapping[str, Any] | None, sport: str) -> SnapshotSummary:
    """Create a safe, fixed-shape summary from an admin operation result."""

    result = result or {}
    status = str(result.get("status", result.get("release_status", ""))).lower()
    success = bool(result.get("success", result.get("ok", False))) or status in {
        "automatic_available", "valid", "published", "ready_to_publish", "stale",
    }
    sport_label = "MLB" if sport.lower() == "mlb" else "Football" if sport.lower() in {"football", "soccer"} else "未知運動"
    if not success:
        reason_code = str(result.get("reason_code", status))
        safe_message = SAFE_FAILURE_BY_CODE.get(reason_code, SAFE_FAILURE_MESSAGE)
        return SnapshotSummary(sport_label, False, "—", "—", "—", "否", safe_message)
    available = result.get("member_available", result.get("member_visible"))
    is_available = bool(available) if available is not None else status in {"published", "automatic_available", "valid", "stale"}
    event_count = result.get("event_count", result.get("game_count", result.get("match_count", result.get("events", "未提供"))))
    kind = result.get("snapshot_kind", "未提供")
    return SnapshotSummary(
        sport_label,
        True,
        _safe_scalar(result.get("updated_at", result.get("published_at", "未提供"))),
        _safe_scalar(event_count),
        _safe_scalar(kind),
        "是" if is_available else "否",
        "快照已保存。" if is_available else "快照已保存，尚不可供會員查詢。",
    )


def render_snapshot_result(st: Any, result: Mapping[str, Any] | None, sport: str) -> SnapshotSummary:
    """Render the same safe admin summary for MLB, Football, and manual publish."""

    summary = summarise_snapshot_result(result, sport)
    if not summary.success:
        st.error(summary.message)
        return summary
    st.success(summary.message)
    columns = st.columns(5)
    labels_values = (
        ("運動種類", summary.sport),
        ("更新時間", summary.updated_at),
        ("賽事數", summary.event_count),
        ("快照種類", summary.snapshot_kind),
        ("會員可查詢", summary.member_available),
    )
    for column, (label, value) in zip(columns, labels_values):
        column.metric(label, value)
    return summary


def _safe_scalar(value: Any) -> str:
    """Only scalar operation metadata can leave the Core admin boundary."""

    return str(value) if isinstance(value, (str, int, float, bool)) else "未提供"
