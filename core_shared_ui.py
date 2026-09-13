"""Core-owned presentation contract for all sports modules.

This module deliberately contains no model calculation, market parsing,
recommendation selection, or outcome-settlement logic.  MLB and Football own
their own data and pass presentation-ready values to this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from html import escape
from typing import Iterable, Mapping, Optional, Sequence


class DisplayStatus(str, Enum):
    """Presentation-only statuses.  Sports modules decide which one applies."""

    WIN = "win"
    PUSH = "push"
    LOSS = "loss"
    PENDING = "pending"


@dataclass(frozen=True)
class ReportColumn:
    """A column supplied by a sport module; Core controls its rendering only."""

    key: str
    label: str
    priority: str = "normal"  # primary | normal | detail
    width: Optional[str] = None


@dataclass(frozen=True)
class ReportRow:
    """One event row. Values may contain deliberate, trusted HTML only."""

    event_id: str
    cells: Mapping[str, str]
    status: DisplayStatus | str = DisplayStatus.PENDING
    note: Optional[str] = None
    group: str = ""


@dataclass(frozen=True)
class SharedReport:
    """Portable report payload produced by MLB or Football and rendered by Core."""

    title: str
    sport: str
    columns: Sequence[ReportColumn]
    rows: Sequence[ReportRow] = field(default_factory=tuple)
    empty_message: str = "目前沒有可顯示的推薦紀錄。"
    summary: str = ""


_STATUS_META = {
    DisplayStatus.WIN.value: ("status-win", "過關"),
    DisplayStatus.PUSH.value: ("status-push", "卡盤"),
    DisplayStatus.LOSS.value: ("status-loss", "未過關"),
    DisplayStatus.PENDING.value: ("status-pending", "待結算"),
}


def shared_report_css() -> str:
    """Return the one visual contract used by desktop tables and mobile cards."""

    return """
    <style>
      .core-report { margin: 0 0 20px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      .core-report__title { margin: 0; padding: 10px 14px; color: #fff; background: linear-gradient(90deg, #0f172a, #334155); border-radius: 8px 8px 0 0; font-size: 15px; }
      .core-report__scroll { overflow-x: auto; border: 1px solid #cbd5e1; border-top: 0; border-radius: 0 0 8px 8px; }
      .core-report table { width: 100%; min-width: 760px; border-collapse: collapse; background: #fff; color: #172033; }
      .core-report[data-sport="football"] table { min-width: 1180px; }
      .core-report[data-sport="football"] td { vertical-align: top; }
      .core-report[data-sport="football"] td[data-label="市場盤口"] { min-width: 210px; }
      .core-report[data-sport="football"] td[data-label="資料風險／警語"] { min-width: 220px; }
      .core-report__summary { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
      .core-report th { padding: 9px 10px; background: #eaf0f7; border: 1px solid #cbd5e1; text-align: center; font-size: 13px; }
      .core-report__group th { background: linear-gradient(90deg, #0f172a, #334155); color: #fff; text-align: left; letter-spacing: .2px; }
      .core-report td { padding: 10px; border: 1px solid #e2e8f0; text-align: center; vertical-align: middle; font-size: 13px; overflow-wrap: anywhere; }
      .core-report tbody tr:nth-child(even) td { background: #f8fafc; }
      .core-report .status-win td { background: #dcfce7 !important; }
      .core-report .status-push td { background: #fef3c7 !important; }
      .core-report .status-loss td { background: #fee2e2 !important; }
      .core-report .status-pending td { background: #f8fafc !important; }
      .core-report__badge { display: inline-block; padding: 2px 7px; border-radius: 99px; font-weight: 700; font-size: 12px; }
      .status-win .core-report__badge { background: #15803d; color: #fff; }
      .status-push .core-report__badge { background: #b45309; color: #fff; }
      .status-loss .core-report__badge { background: #dc2626; color: #fff; }
      .status-pending .core-report__badge { background: #64748b; color: #fff; }
      .core-report__note { display: block; margin-top: 4px; color: #475569; font-size: 11px; }
      .core-report__empty { padding: 16px; border: 1px solid #cbd5e1; border-top: 0; color: #64748b; background: #fff; border-radius: 0 0 8px 8px; }
      @media (max-width: 700px) {
        .core-report__scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
        .core-report table { min-width: 980px; }
        .core-report th, .core-report td { padding: 8px 7px; font-size: 12px; }
        .core-report__title { position: sticky; left: 0; }
      }
    </style>
    """


def render_shared_report(report: SharedReport) -> str:
    """Render a report without interpreting sport-specific content or outcomes."""

    _validate_report(report)
    headers = "".join(_render_header(column) for column in report.columns)
    parts = []
    current_group = None
    for row in sorted(report.rows, key=lambda item: item.group) if report.sport == "football" else report.rows:
        if row.group and row.group != current_group:
            parts.append(f'<tr class="core-report__group"><th colspan="{len(report.columns)}">{escape(row.group)}</th></tr>')
            current_group = row.group
        parts.append(_render_row(row, report.columns))
    body = "".join(parts)
    content = (
        f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>"
        if report.rows
        else f'<div class="core-report__empty">{escape(report.empty_message)}</div>'
    )
    return (
        shared_report_css()
        + (f'<p class="core-report__summary">{escape(report.summary)}</p>' if report.summary else "")
        + '<section class="core-report"'
        + f' data-sport="{escape(report.sport, quote=True)}">'
        + f'<h3 class="core-report__title">{escape(report.title)}</h3>'
        + f'<div class="core-report__scroll">{content}</div></section>'
    )


def _validate_report(report: SharedReport) -> None:
    keys = [column.key for column in report.columns]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("Report columns must contain unique keys.")
    for row in report.rows:
        missing = set(keys) - set(row.cells)
        if missing:
            raise ValueError(f"Row {row.event_id!r} is missing cells: {sorted(missing)}")


def _render_header(column: ReportColumn) -> str:
    width = f' style="width:{escape(column.width, quote=True)}"' if column.width else ""
    return f'<th{width}>{escape(column.label)}</th>'


def _render_row(row: ReportRow, columns: Iterable[ReportColumn]) -> str:
    columns = tuple(columns)
    status = _normalise_status(row.status)
    class_name, label = _STATUS_META[status]
    cells = []
    for column in columns:
        # Cell values are escaped because Core must not trust cross-module input.
        value = escape(str(row.cells[column.key])).replace("\n", "<br>")
        cells.append(f'<td data-label="{escape(column.label, quote=True)}">{value}</td>')
    if row.note:
        cells[-1] = cells[-1].replace("</td>", f'<span class="core-report__note">{escape(row.note)}</span></td>')
    if columns[-1].key == "settlement":
        cells[-1] = cells[-1].replace("</td>", f'<br><span class="core-report__badge">{label}</span></td>')
    return f'<tr class="{class_name}" data-event-id="{escape(row.event_id, quote=True)}">{"".join(cells)}</tr>'


def _normalise_status(value: DisplayStatus | str) -> str:
    raw = value.value if isinstance(value, DisplayStatus) else str(value).strip().lower()
    if raw not in _STATUS_META:
        raise ValueError(f"Unknown display status: {value!r}")
    return raw
