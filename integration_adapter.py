"""Core-only bridge between sport-module presentation payloads and member UI.

The adapter never imports, calls, or alters MLB/Football domain services.  It
accepts only their already-produced payloads/rows and converts them into the
stable Core presentation contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

try:  # Supports both a consolidated app folder and package-style imports.
    from core_shared_ui import DisplayStatus, ReportColumn, ReportRow, SharedReport
except ModuleNotFoundError:  # pragma: no cover - used when imported as a package
    from .core_shared_ui import DisplayStatus, ReportColumn, ReportRow, SharedReport


TZ_TW = ZoneInfo("Asia/Taipei")
MEMBER_OPEN_TIME = time(19, 30)

# This is the Core-owned member-table contract.  Sport modules may not reorder
# or replace these columns; they only provide display-ready cell values.
MEMBER_REPORT_COLUMNS: tuple[ReportColumn, ...] = (
    ReportColumn("event_meta", "賽事時間／聯盟或運動類別", "primary", "14%"),
    ReportColumn("matchup", "對戰", "primary", "14%"),
    ReportColumn("market", "市場盤口", "normal", "16%"),
    ReportColumn("recommendation", "最終推薦", "primary", "18%"),
    ReportColumn("model_ev", "模型機率／EV", "normal", "13%"),
    ReportColumn("risk_warning", "資料風險／警語", "detail", "12%"),
    ReportColumn("source_timing", "資料來源／查詢或校正時間", "detail", "13%"),
    ReportColumn("settlement", "結算狀態", "normal", "auto"),
)


class ReleaseStatus(str, Enum):
    FETCHING = "fetching"
    AWAITING_MANUAL_CALIBRATION = "awaiting_manual_calibration"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHED = "published"
    AUTOMATIC_AVAILABLE = "automatic_available"
    FAILED = "failed"


@dataclass(frozen=True)
class MemberReleaseGate:
    """The Core result for a member request; no sport operation occurs here."""

    allowed: bool
    status: ReleaseStatus
    message: str
    report: Optional[SharedReport] = None


def mlb_payloads_to_shared_report(payloads: Iterable[Any]) -> SharedReport:
    """Adapt ``PublishedGame.as_member_payload()`` output without MLB semantics."""

    rows = []
    for payload in payloads:
        sport = _value(payload, "sport", "MLB")
        teams = _value(payload, "teams", {}) or {}
        away, home = _teams(teams, payload)
        recommendations = _value(payload, "recommendations", ()) or ()
        risk = _text(_value(payload, "risk", ""))
        warning = _text(_value(payload, "warning", ""))
        model = _value(payload, "model", "資料未提供")
        sources = _value(payload, "sources", "資料來源未提供")
        cells = {
            "event_meta": _event_meta(_value(payload, "kickoff", "未提供"), sport),
            "matchup": _matchup(away, home),
            "market": _market_text(payload),
            "recommendation": _recommendation_text(recommendations),
            "model_ev": _model_ev_text(model, recommendations),
            "risk_warning": _join_text(risk, warning) or "無額外警語",
            "source_timing": _source_timing(sources, payload),
            "settlement": _settlement_label(_value(payload, "settlement_status", "pending")),
        }
        rows.append(ReportRow(str(_value(payload, "event_id", "unknown")), cells, _display_status(payload)))
    return SharedReport("MLB｜正式推薦", "mlb", MEMBER_REPORT_COLUMNS, tuple(rows))


def football_rows_to_shared_report(rows: Iterable[Any], run_metadata: Mapping[str, Any]) -> SharedReport:
    """Adapt Football presentation rows plus Core-run metadata, without AH parsing."""

    report_rows = []
    for row in rows:
        recommendations = _value(row, "recommendations", ()) or ()
        warning = _join_text(_text(_value(row, "risk", "")), _text(_value(row, "warning", "")))
        cells = {
            "event_meta": _event_meta(_value(row, "kickoff", "未提供"), _value(row, "sport", "Football")),
            "matchup": _matchup(_value(row, "away", "客隊未提供"), _value(row, "home", "主隊未提供")),
            "market": _market_text(row),
            "recommendation": _recommendation_text(recommendations),
            "model_ev": _model_ev_text(_value(row, "model", "資料未提供"), recommendations),
            "risk_warning": warning or "無額外警語",
            "source_timing": _run_metadata_text(run_metadata),
            "settlement": _settlement_label(_value(row, "settlement_status", "pending")),
        }
        report_rows.append(ReportRow(str(_value(row, "event_id", "unknown")), cells, _display_status(row)))
    return SharedReport("Football｜正式推薦", "football", MEMBER_REPORT_COLUMNS, tuple(report_rows))


def member_release_gate(
    status: ReleaseStatus | str,
    stored_report: Optional[SharedReport],
    *,
    now: Optional[datetime] = None,
) -> MemberReleaseGate:
    """Return only a saved report after the Taiwan 19:30 publication gate.

    ``stored_report`` must be a report read from persisted, published data.
    The gate neither accepts a refresh callback nor invokes a sport module.
    """

    release_status = ReleaseStatus(status)
    now_tw = (now or datetime.now(TZ_TW)).astimezone(TZ_TW)
    if release_status is ReleaseStatus.FAILED:
        return MemberReleaseGate(False, release_status, "今日資料取得或盤口校正失敗，暫不提供推薦。")
    if now_tw.time() < MEMBER_OPEN_TIME:
        return MemberReleaseGate(False, release_status, "今日推薦將於台灣時間 19:30 後開放。")
    if release_status is ReleaseStatus.FETCHING:
        return MemberReleaseGate(False, release_status, "今日賽事數據分析中。")
    if release_status is ReleaseStatus.AWAITING_MANUAL_CALIBRATION:
        return MemberReleaseGate(False, release_status, "正在校正最新盤口與賽事數據。")
    if release_status is ReleaseStatus.READY_TO_PUBLISH:
        return MemberReleaseGate(False, release_status, "今日推薦已完成校正，等待發布。")
    if stored_report is None:
        return MemberReleaseGate(False, release_status, "正式推薦尚未儲存，暫不提供推薦。")
    if release_status is ReleaseStatus.AUTOMATIC_AVAILABLE:
        return MemberReleaseGate(True, release_status, "自動更新推薦｜尚未人工校正。", stored_report)
    return MemberReleaseGate(True, release_status, "已發布：顯示已儲存的正式推薦。", stored_report)


def decorate_snapshot_report(
    report: SharedReport,
    *,
    snapshot_kind: str | None,
    calibration_state: str | None,
    updated_at: Any = None,
    stale_warning: Any = None,
) -> SharedReport:
    """Add Core-owned snapshot provenance without changing sport data or columns."""

    is_automatic = snapshot_kind in {"automatic", "automatic_baseline"}
    marker = "自動更新推薦｜尚未人工校正" if is_automatic else "人工校正正式推薦"
    title = f"{report.title}｜{marker}"
    provenance = _join_text(
        marker,
        "校正狀態：" + _text(calibration_state) if calibration_state else "",
        "更新：" + _text(updated_at) if updated_at else "",
    )
    rows = []
    for row in report.rows:
        cells = dict(row.cells)
        cells["source_timing"] = _join_text(cells.get("source_timing", ""), provenance)
        if stale_warning:
            cells["risk_warning"] = _join_text(cells.get("risk_warning", ""), "時效警語：" + _text(stale_warning))
        rows.append(ReportRow(row.event_id, cells, row.status, row.note))
    return SharedReport(title, report.sport, report.columns, tuple(rows), report.empty_message)


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _teams(teams: Any, payload: Any) -> tuple[str, str]:
    if isinstance(teams, Mapping):
        return str(teams.get("away", teams.get("away_team", "客隊未提供"))), str(teams.get("home", teams.get("home_team", "主隊未提供")))
    return str(_value(payload, "away", "客隊未提供")), str(_value(payload, "home", "主隊未提供"))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "；".join(str(item) for item in value if item is not None)
    return str(value).strip()


def _join_text(*values: str) -> str:
    return "；".join(value for value in values if value)


def _event_meta(kickoff: Any, sport: Any) -> str:
    return f"{kickoff}\n{sport}"


def _matchup(away: Any, home: Any) -> str:
    return f"{away} vs {home}"


def _market_text(source: Any) -> str:
    # Presentation fields only: no raw-line interpretation or format conversion.
    parts = []
    for key, label in (("first_market", "首次發布盤"), ("latest_market", "最新校正盤"), ("market_change", "盤口變化")):
        value = _value(source, key)
        if value not in (None, "", [], {}):
            parts.append(f"{label}：{_text(value)}")
    return "\n".join(parts) if parts else "盤口資料未提供"


def _recommendation_text(recommendations: Any) -> str:
    picks = _eligible_recommendations(recommendations)
    if not picks:
        return "PASS｜無 +EV 且 playable 的推薦"
    return "\n".join(_text(_value(pick, "display", _value(pick, "selection", _value(pick, "label", "推薦")))) for pick in picks)


def _model_ev_text(model: Any, recommendations: Any) -> str:
    parts = [_text(model)] if model not in (None, "", {}, []) else []
    for pick in _eligible_recommendations(recommendations):
        probability = _value(pick, "model_probability", _value(pick, "probability"))
        ev = _value(pick, "ev")
        value = _join_text("模型機率：" + _text(probability) if probability is not None else "", "EV：" + _text(ev) if ev is not None else "")
        if value:
            parts.append(value)
    return "\n".join(parts) if parts else "模型／EV 資料未提供"


def _eligible_recommendations(recommendations: Any) -> list[Any]:
    if isinstance(recommendations, Mapping):
        recommendations = recommendations.get("items", ())
    picks = []
    for pick in recommendations:
        playable = _value(pick, "playable", False)
        ev = _value(pick, "ev")
        try:
            positive_ev = float(ev) > 0
        except (TypeError, ValueError):
            positive_ev = False
        if playable is True and positive_ev:
            picks.append(pick)
    return picks


def _source_timing(sources: Any, payload: Any) -> str:
    updated = _value(payload, "updated_at", _value(payload, "queried_at", _value(payload, "calibrated_at", None)))
    return _join_text("來源：" + _text(sources), "更新／校正：" + _text(updated) if updated else "")


def _run_metadata_text(metadata: Mapping[str, Any]) -> str:
    return _join_text(
        "來源：" + _text(metadata.get("sources", "資料來源未提供")),
        "校正來源：" + _text(metadata.get("calibration_source", "未提供")),
        "更新：" + _text(metadata.get("updated_at", "未提供")),
        "發布狀態：" + _text(metadata.get("release_status", "未提供")),
    )


def _display_status(source: Any) -> DisplayStatus:
    raw = str(_value(source, "settlement_status", "pending")).lower()
    return DisplayStatus(raw) if raw in {item.value for item in DisplayStatus} else DisplayStatus.PENDING


def _settlement_label(value: Any) -> str:
    return {"win": "過關", "push": "卡盤", "loss": "未過關", "pending": "待結算"}.get(str(value).lower(), "待結算")
