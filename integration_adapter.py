"""Core-only bridge between sport-module presentation payloads and member UI.

The adapter never imports, calls, or alters MLB/Football domain services.  It
accepts only their already-produced payloads/rows and converts them into the
stable Core presentation contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo
from football_display import LEAGUES, team_name, translate_teams

try:  # Supports both a consolidated app folder and package-style imports.
    from core_shared_ui import DisplayStatus, ReportColumn, ReportRow, SharedReport
except ModuleNotFoundError:  # pragma: no cover - used when imported as a package
    from .core_shared_ui import DisplayStatus, ReportColumn, ReportRow, SharedReport


TZ_TW = ZoneInfo("Asia/Taipei")
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
            "event_meta": _format_taipei_kickoff(_value(payload, "kickoff", "未提供")),
            "matchup": f"{team_name(away)}（客）\n{team_name(home)}（主）",
            "pitchers": _text(_value(payload, "pitchers_display", "舊快照未保存先發資訊")),
            "market": translate_teams(_decimal_display(_market_text(payload)), home, away),
            "recommendation": translate_teams(_decimal_display(_recommendation_text(recommendations)), home, away),
            "model_ev": _text(model),
            "market_change": translate_teams(_decimal_display(_text(_value(payload, "market_change", "尚無比較紀錄"))), home, away),
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
        home, away = str(_value(row, "home", "")), str(_value(row, "away", ""))
        forecast = _value(row, "forecast", {}) or {}
        if not isinstance(forecast, Mapping):
            forecast = {}
        evaluations = _value(row, "market_evaluations", ()) or ()
        recommendations = _value(row, "recommendations", ()) or ()
        warning = _join_text(_text(_value(row, "risk", "")), _text(_value(row, "warning", "")))
        cells = {
            "event_meta": _format_taipei_kickoff(_value(row, "kickoff", "未提供")),
            "matchup": f"主：{team_name(home)}\n客：{team_name(away)}",
            "market": translate_teams(_market_text(row), home, away),
            "recommendation": _recommendation_text(recommendations),
            "model_ev": _forecast_probabilities(forecast),
            "market_change": translate_teams(_text(_value(row, "market_change", "尚無比較紀錄")), home, away),
            "xg": f"主 {_number(forecast.get('home_xg'))}／客 {_number(forecast.get('away_xg'))}",
            "score": str(forecast.get("score") or "尚未儲存，請重新建立快照"),
            "moneyline": _football_pick(recommendations, "moneyline", home, away, evaluations),
            "spread": _football_pick(recommendations, "spread", home, away, evaluations),
            "total": _football_pick(recommendations, "total", home, away, evaluations),
            "risk_warning": warning or "無額外警語",
            "source_timing": _run_metadata_text(run_metadata),
            "settlement": _settlement_label(_value(row, "settlement_status", "pending")),
        }
        report_rows.append(ReportRow(str(_value(row, "event_id", "unknown")), cells, _display_status(row),
                                    group=LEAGUES.get(_value(row, "league_key"), "聯賽未記錄（請重新建立快照）")))
    columns = tuple(ReportColumn(k, label) for k, label in (
        ("event_meta", "比賽時間（台灣）"), ("matchup", "對戰"),
        ("market", "市場盤口"), ("model_ev", "主勝／和局／客勝機率"),
        ("xg", "預估 xG（模型進球）"), ("score", "預估比分（主：客）"),
        ("moneyline", "獨贏推薦／EV"), ("spread", "讓分推薦／EV"),
        ("total", "大小分推薦／EV"), ("risk_warning", "資料風險／警語")))
    return SharedReport("足球賽事分析", "football", columns, tuple(report_rows),
                        summary=_run_metadata_text(run_metadata))


def _decimal_display(text: str) -> str:
    """Convert explicitly labelled HK display values only; never touch stored prices."""
    return re.sub(r"香港盤\s*([0-9]+(?:\.[0-9]+)?)",
                  lambda match: f"歐洲賠率 {float(match.group(1)) + 1:.3f}".rstrip("0").rstrip("."),
                  text)


def _number(value):
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _as_float(value: Any) -> Optional[float]:
    """Best-effort display conversion; malformed old snapshots stay readable."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent(value: Any, *, signed: bool = False, missing: str = "尚未儲存") -> str:
    number = _as_float(value)
    if number is None:
        return missing
    return f"{number:+.1%}" if signed else f"{number:.1%}"


def _forecast_probabilities(forecast):
    values = []
    for key, label in (("home_probability", "主勝"), ("draw_probability", "和局"), ("away_probability", "客勝")):
        value = forecast.get(key)
        values.append(f"{label} {_percent(value)}")
    return "\n".join(values)


def _football_pick(recommendations, market_type, home, away, evaluations=()):
    picks = [p for p in _eligible_recommendations(recommendations) if _value(p, "market_type") == market_type]
    if not picks:
        values = [_as_float(_value(p, "ev")) for p in evaluations
                  if _value(p, "market_type") == market_type]
        values = [value for value in values if value is not None]
        if values:
            return f"暫不推薦｜最高 EV {max(values):+.1%}"
        return "無法評估｜缺少有效盤口或運算結果"
    return "\n".join(
        translate_teams(str(_value(p, "display", _value(p, "selection", "推薦"))), home, away)
        + f"｜EV {_percent(_value(p, 'ev'), signed=True, missing='未提供')}"
        for p in picks
    )


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

    try:
        release_status = ReleaseStatus(status)
    except ValueError:
        release_status = ReleaseStatus.FETCHING
    now_tw = (now or datetime.now(TZ_TW)).astimezone(TZ_TW)
    if release_status is ReleaseStatus.FAILED:
        return MemberReleaseGate(False, release_status, "今日資料取得或盤口校正失敗，暫不提供推薦。")
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
        "更新：" + _format_taipei_kickoff(updated_at) if updated_at else "",
    )
    rows = []
    for row in report.rows:
        cells = dict(row.cells)
        cells["source_timing"] = _join_text(cells.get("source_timing", ""), provenance)
        if stale_warning:
            cells["risk_warning"] = _join_text(cells.get("risk_warning", ""), "時效警語：" + _text(stale_warning))
        rows.append(ReportRow(row.event_id, cells, row.status, row.note, row.group))
    columns = tuple(c for c in report.columns if c.key != "source_timing")
    summaries = list(dict.fromkeys(row.cells.get("source_timing", "") for row in rows))
    return SharedReport(title, report.sport, columns, tuple(rows), report.empty_message,
                        _join_text(report.summary, provenance) if report.summary else _join_text(*summaries))


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


def _format_taipei_kickoff(value: Any) -> str:
    """Format a stored ISO kickoff for display only; never alter event data."""

    raw = _text(value)
    if not raw or raw == "未提供":
        return "時間未提供"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ_TW)
        return parsed.astimezone(TZ_TW).strftime("%m/%d %H:%M（台灣時間）")
    except (TypeError, ValueError):
        return raw


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
        return "暫不推薦｜未達門檻" if recommendations else "無法評估｜缺少有效盤口或運算結果"
    values = []
    for pick in picks:
        selection = _text(_value(pick, "display", _value(pick, "selection", _value(pick, "label", "推薦"))))
        probability = _value(pick, "model_probability", _value(pick, "probability"))
        values.append(
            selection
            + f"\n模型 {_percent(probability)}｜EV {_percent(_value(pick, 'ev'), signed=True, missing='未提供')}"
        )
    return "\n".join(values)


def _model_ev_text(model: Any, recommendations: Any) -> str:
    parts = [_text(model)] if model not in (None, "", {}, []) else []
    for pick in _eligible_recommendations(recommendations):
        probability = _value(pick, "model_probability", _value(pick, "probability"))
        ev = _value(pick, "ev")
        value = _join_text(
            "模型機率：" + _percent(probability) if probability is not None else "",
            "EV：" + _percent(ev, signed=True, missing="未提供") if ev is not None else "",
        )
        if value:
            parts.append(value)
    return "\n".join(parts) if parts else "模型／EV 資料未提供"


def _eligible_recommendations(recommendations: Any) -> list[Any]:
    if isinstance(recommendations, Mapping):
        recommendations = recommendations.get("items", ())
    if not isinstance(recommendations, (list, tuple, set)):
        return []
    picks = []
    for pick in recommendations:
        playable = _value(pick, "playable", False)
        ev = _value(pick, "ev")
        try:
            positive_ev = float(ev) > 0
        except (TypeError, ValueError):
            positive_ev = False
        if (playable is True or type(playable) is int and playable == 1) and positive_ev:
            picks.append(pick)
    return picks


def _source_timing(sources: Any, payload: Any) -> str:
    updated = _value(payload, "updated_at", _value(payload, "queried_at", _value(payload, "calibrated_at", None)))
    return _join_text("來源：" + _text(sources), "更新／校正：" + _text(updated) if updated else "")


def _run_metadata_text(metadata: Mapping[str, Any]) -> str:
    return _join_text(
        "來源：" + _text(metadata.get("sources", "資料來源未提供")),
        "校正來源：" + _text(metadata.get("calibration_source", "未提供")),
        "更新：" + _format_taipei_kickoff(metadata.get("updated_at", "未提供")),
        "發布狀態：" + {"automatic_available": "自動快照可查詢", "published": "人工正式發布",
                      "awaiting_manual_calibration": "等待人工校正"}.get(metadata.get("release_status"), "已讀取儲存快照"),
    )


def _display_status(source: Any) -> DisplayStatus:
    raw = str(_value(source, "settlement_status", "pending")).lower()
    return DisplayStatus(raw) if raw in {item.value for item in DisplayStatus} else DisplayStatus.PENDING


def _settlement_label(value: Any) -> str:
    return {"win": "過關", "push": "卡盤", "loss": "未過關", "pending": "待結算"}.get(str(value).lower(), "待結算")
