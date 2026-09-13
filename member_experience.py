"""Read-only presentation for saved member snapshots.

This module deliberately turns the shared report into a compact, original-app
style match table. It never calls a provider, model, calculation service or
database writer; it receives only Core's already-built ``MemberReleaseGate``.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from core_shared_ui import ReportColumn, ReportRow, SharedReport, render_shared_report


def show_report(st: Any, gate: Any, *, admin: bool = False) -> None:
    """Display one stored report without exposing payloads or source internals."""

    if not gate.allowed or gate.report is None:
        st.info(gate.message)
        return

    report = gate.report
    if not report.rows:
        st.info("目前沒有可顯示的已保存賽事。")
        return

    automatic = "自動更新推薦" in report.title
    st.caption(
        ("自動分析版本｜尚未人工校正" if automatic else "人工校正正式版本")
        + f"｜{len(report.rows)} 場｜會員僅讀取此已保存版本"
    )
    st.caption(_source_caption(report.summary))

    st.caption("賽果預估＝比賽結果；推薦欄機率＝該投注項目的模型估計。EV＝按所示賠率與結算規則估計的報酬率，不保證獲利。")
    compact = _compact_report(report)
    st.markdown(render_shared_report(compact), unsafe_allow_html=True)
    _render_single_game_detail(st, report, admin=admin)


def _compact_report(report: SharedReport) -> SharedReport:
    """Keep the table to the information a member needs to compare matches."""

    football = report.sport == "football"
    if football:
        columns = (
            ReportColumn("event_meta", "開賽（台灣）", "primary", "10%"),
            ReportColumn("matchup", "對戰", "primary", "16%"),
            ReportColumn("xg", "預估 xG", "normal", "11%"),
            ReportColumn("score", "預估比分", "normal", "9%"),
            ReportColumn("model_ev", "主勝／和局／客勝", "normal"),
            ReportColumn("moneyline", "獨贏推薦／EV", "primary", "18%"),
            ReportColumn("spread", "讓分推薦／EV", "primary", "18%"),
            ReportColumn("total", "大小推薦／EV", "primary", "18%"),
        )
        title = "歐洲五大聯賽・歐冠｜當日賽事分析"
    else:
        columns = (
            ReportColumn("event_meta", "開賽（台灣）", "primary", "12%"),
            ReportColumn("matchup", "對戰", "primary", "18%"),
            ReportColumn("pitchers", "先發投手", "normal", "20%"),
            ReportColumn("model_ev", "賽果預估（非投注機率）", "normal", "20%"),
            ReportColumn("recommendation", "推薦盤口／EV", "primary", "30%"),
        )
        title = "MLB｜當日賽事分析"

    rows = []
    for row in report.rows:
        cells = dict(row.cells)
        # Old snapshots can lack display-only additions. Their absence must
        # never stop the rest of the daily schedule from rendering.
        for column in columns:
            cells.setdefault(column.key, "尚未保存")
        rows.append(ReportRow(row.event_id, cells, row.status, row.note, row.group))
    return replace(report, title=title, columns=columns, rows=tuple(rows), summary="")


def _render_single_game_detail(st: Any, report: SharedReport, *, admin: bool) -> None:
    """Put explainability below the table rather than inside every table cell."""

    st.markdown("#### 單場盤口與水位變化")
    ids = list(range(len(report.rows)))
    index = st.selectbox(
        "選擇比賽",
        ids,
        format_func=lambda i: str(report.rows[i].cells.get("matchup", "賽事")).replace("\n", " "),
        key=f"detail:{report.sport}:{'admin' if admin else 'member'}",
    )
    row = report.rows[index]
    change = str(row.cells.get("market_change", "尚無比較紀錄"))
    st.write("盤口與水位未變動" if change == "無變化" else change)
    st.caption("比較首次保存與最新保存的盤口；不是莊家完整開盤歷史。查詢只讀快照，不會抓取即時水位。")

    with st.expander("資料限制、風險與適用條件", expanded=False):
        st.write(row.cells.get("risk_warning", "無額外紀錄"))
        st.caption("只適用於上方保存的盤口與價格；先發、陣容或價格改變後，須由後台重新建立快照。")
        st.caption("正 EV 是模型與該價格下的期望值，不是單場獲利保證。")


def _source_caption(summary: Any) -> str:
    """Return a concise public summary, never an operational diagnostic dump."""

    text = str(summary or "")
    sources = [
        name
        for name in (
            "MLB 官方", "MLB Stats API", "Baseball Savant", "Open-Meteo",
            "ESPN", "API-Football", "ClubElo", "The Odds API",
        )
        if name in text
    ]
    # Core already formats snapshot provenance into Taiwan time. The regex is
    # intentionally restrictive so raw URLs, JSON or provider errors cannot be
    # accidentally surfaced from an old snapshot.
    timestamps = list(dict.fromkeys(re.findall(r"\d{2}/\d{2}\s+\d{2}:\d{2}", text)))
    source_label = "、".join(sources) if sources else "已保存快照"
    suffix = f"｜更新／校正：{'、'.join(timestamps)} 台灣時間" if timestamps else ""
    return f"資料來源：{source_label}{suffix}"
