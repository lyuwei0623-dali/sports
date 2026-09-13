"""維大力體育APP Streamlit entry point.

Member routes read stored snapshots only.  Admin-only buttons may fetch,
calculate and save snapshots.  Sport calculation rules remain in their modules.
"""
from __future__ import annotations

import os
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from admin_snapshot_ui import render_snapshot_result
from app_services import AppServices, compose_services
from member_experience import show_report
from live_ui import render_live
from source_health import check_sources
from football_display import team_name
from mlb_pre_release_module import (
    MLBAutoSnapshotRunner, MLBPreReleaseService, SuperQuote, parse_super_line,
)

TZ_TAIPEI = ZoneInfo("Asia/Taipei")
APP_NAME = "維大力體育APP"
st.set_page_config(page_title=APP_NAME, page_icon="⚽", layout="wide", initial_sidebar_state="collapsed")


def services() -> AppServices:
    # Avoid retaining old module/service instances across a source update.
    return compose_services()


def _taipei_now() -> datetime:
    return datetime.now(TZ_TAIPEI)


def _render_brand() -> None:
    """Render the owner brand with Streamlit widgets, never embedded HTML.

    Some hosted Streamlit builds deliberately render data-URI HTML as text.
    Keeping the logo as a normal local image avoids exposing Base64 source code
    while retaining the owner's anti-counterfeit mark on every entry page.
    """

    logo = Path(__file__).with_name("logo.png")
    logo_column, title_column = st.columns((1, 7), vertical_alignment="center")
    with logo_column:
        if logo.is_file():
            st.image(str(logo), width=88)
        else:
            st.caption("大力體育")
    with title_column:
        st.title(APP_NAME)
        st.caption("有依據的賽事分析・盤口價值・單場風險")
        st.caption("大力體育防偽識別｜官方會員分析")
    st.divider()


def _login() -> None:
    _render_brand()
    st.caption("輸入會員密碼進入賽事查詢；輸入管理員密碼會直接進入後台。")
    member_password = os.environ.get("APP_MEMBER_PASSWORD", "")
    admin_password = os.environ.get("APP_ADMIN_PASSWORD", "")
    if not member_password or not admin_password:
        st.error("尚未完整設定 APP_MEMBER_PASSWORD 與 APP_ADMIN_PASSWORD。")
        st.stop()
    password = st.text_input("登入密碼", type="password", key="login_password")
    if st.button("登入", type="primary"):
        if password == admin_password:
            st.session_state.user_role = "admin"
            st.rerun()
        elif password == member_password:
            st.session_state.user_role = "member"
            st.rerun()
        else:
            st.error("密碼不正確")
    st.stop()


def _member_page(app: AppServices) -> None:
    _render_brand()
    section = st.radio("查詢功能", ["MLB", "歐洲足球", "走地計算機"], horizontal=True, label_visibility="collapsed")
    if section == "走地計算機":
        render_live(st)
        return
    selected = st.date_input("賽事日期（台灣）", value=_taipei_now().date(), key="member_date")
    sport = "mlb" if section == "MLB" else "football"
    if st.button("讀取已保存的賽事分析", type="primary", key=f"query_saved:{sport}"):
        st.session_state[f"member_query:{sport}:{selected.isoformat()}"] = _taipei_now().strftime("%m/%d %H:%M")
    gate = app.members.get_member_view(sport, selected.isoformat(), now=_taipei_now())
    show_report(st, gate)
    st.caption("機率高不等於值得下注。資料更新由管理員執行；會員查詢不抓取即時盤口。")


def _matchup_label(row: dict[str, object], sport: str) -> str:
    if sport == "mlb":
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        away, home = teams.get("away", "客隊"), teams.get("home", "主隊")
    else:
        away, home = row.get("away", "客隊"), row.get("home", "主隊")
    return f"{team_name(away)}（客） vs {team_name(home)}（主）"


def _normalise_mlb_home_line(raw: str) -> str:
    """Validate the one home-team SUPER line entered by the administrator.

    The domain parser remains the authoritative implementation for SUPER
    settlement.  This UI helper deliberately requires the leading sign, so the
    selected side can never be silently inverted: ``-`` means the home team
    gives points and ``+`` means it receives points.
    """

    value = str(raw).strip().replace(" ", "")
    if not value:
        raise ValueError("主隊讓分盤口不可空白")
    if value[0] not in "+-":
        raise ValueError("請以 + 或 - 開頭：- 代表主隊讓分，+ 代表主隊受讓")
    parse_super_line(value)
    return value


def _opponent_mlb_line(home_line: str) -> str:
    """Return the opposite-team line without changing SUPER's ratio syntax."""

    return ("+" if home_line.startswith("-") else "-") + home_line[1:]


def _parse_home_asian_handicap(raw: str) -> float:
    """Read one signed home-team Asian handicap without changing AH rules.

    The Football module still validates and settles the resulting number.  The
    slash form is a convenient administrator input for quarter lines, e.g.
    ``-0/0.5`` or ``+0.5/1``.
    """

    text = str(raw).strip().replace(" ", "")
    if text.upper() in {"PK", "平"}:
        return 0.0
    if not text or text[0] not in "+-":
        raise ValueError("請以 + 或 - 開頭：- 代表主隊讓分，+ 代表主隊受讓")
    sign, body = (-1.0 if text[0] == "-" else 1.0), text[1:]
    if not body:
        raise ValueError("請輸入主隊亞洲讓分數值，例如 -0.5、+0.5、-0/0.5")
    try:
        if "/" in body:
            values = [float(part) for part in body.split("/")]
            if len(values) != 2 or any(value < 0 for value in values):
                raise ValueError
            if not math.isclose(abs(values[1] - values[0]), 0.5):
                raise ValueError
            line = sign * sum(values) / 2
        else:
            line = sign * float(body)
    except ValueError as exc:
        raise ValueError("亞洲盤格式請填 -0.5、+0.5、-0/0.5 或 +0.5/1") from exc
    if not math.isclose(line * 4, round(line * 4)):
        raise ValueError("亞洲盤只接受 0.25 的倍數")
    return line


def _positive(value: float, label: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{label}必須大於 0")
    return number


def _mlb_manual_section(app: AppServices, selected, date_str: str) -> None:
    snapshot = app.mlb_store.get_member_snapshot(date_str)
    rows = list(snapshot.get("payloads") or [])
    st.subheader("MLB 人工 SUPER 校正與發布")
    st.caption("先選擇比賽，再逐格輸入實際 SUPER 盤口；不需要準備或上傳 JSON。")
    if not rows:
        st.info("請先執行 MLB 自動快照，取得當天完整賽程後再人工校正。")
        return
    event_indexes = tuple(range(len(rows)))
    row = rows[st.selectbox(
        "選擇要校正的比賽", event_indexes,
        format_func=lambda index: _matchup_label(rows[index], "mlb"),
        key=f"mlb_event:{date_str}",
    )]
    event_id = str(row.get("event_id"))
    with st.form(f"mlb_manual:{date_str}:{event_id}"):
        home_line = st.text_input(
            "主隊讓分盤（- 主隊讓分；+ 主隊受讓）", value="-1.5",
            help="只需填主隊一格。支援 -1.5、+1.5、-1+85、+1-60 等完整 SUPER 寫法；客隊盤會自動反向。",
        )
        st.caption("特殊盤請完整輸入基準與比例，例如「-1+85」或「+1-60」；+85 單獨只代表比例，沒有讓分基準，無法正確結算。")
        spread_price_1, spread_price_2 = st.columns(2)
        home_spread_price = spread_price_1.number_input("主隊讓分盤香港賠率", min_value=0.01, value=0.94, step=0.01)
        away_spread_price = spread_price_2.number_input("客隊讓分盤香港賠率", min_value=0.01, value=0.94, step=0.01)
        total_1, total_2, total_3 = st.columns(3)
        total_line = total_1.text_input("大小分盤", value="8.5", help="支援 8.5、8+65、9-30 等寫法")
        over_price = total_2.number_input("大分香港賠率", min_value=0.01, value=0.94, step=0.01)
        under_price = total_3.number_input("小分香港賠率", min_value=0.01, value=0.94, step=0.01)
        money_1, money_2 = st.columns(2)
        home_moneyline = money_1.number_input("主隊獨贏香港賠率", min_value=0.01, value=0.94, step=0.01)
        away_moneyline = money_2.number_input("客隊獨贏香港賠率", min_value=0.01, value=0.94, step=0.01)
        save_event = st.form_submit_button("儲存此場人工校正")
    if save_event:
        try:
            home_line = _normalise_mlb_home_line(home_line)
            away_line = _opponent_mlb_line(home_line)
            parse_super_line(total_line)
            quotes = [
                SuperQuote("spread", "home", home_line, _positive(home_spread_price, "主隊讓分賠率")),
                SuperQuote("spread", "away", away_line, _positive(away_spread_price, "客隊讓分賠率")),
                SuperQuote("total", "over", total_line.strip(), _positive(over_price, "大分賠率")),
                SuperQuote("total", "under", total_line.strip(), _positive(under_price, "小分賠率")),
                SuperQuote("moneyline", "home", None, _positive(home_moneyline, "主隊獨贏賠率")),
                SuperQuote("moneyline", "away", None, _positive(away_moneyline, "客隊獨贏賠率")),
            ]
            saved = st.session_state.setdefault(f"mlb_manual_quotes:{date_str}", {})
            saved[event_id] = quotes
            home_direction = "主隊讓分" if home_line.startswith("-") else "主隊受讓"
            st.success(f"已暫存人工盤口：{_matchup_label(row, 'mlb')}｜{home_direction} {home_line}；客隊自動對應 {away_line}")
        except (TypeError, ValueError) as exc:
            st.error(str(exc))
    pending = st.session_state.get(f"mlb_manual_quotes:{date_str}", {})
    st.caption(f"本次已輸入 {len(pending)}／{len(rows)} 場；未人工修改的比賽會保留自動快照推薦。")
    if st.button("套用已輸入盤口並發布 MLB", disabled=not pending):
        try:
            with st.spinner("正在依人工盤口重新運算並發布…"):
                calculated = MLBPreReleaseService().run(selected, pending, calibrated_at=_taipei_now().isoformat())
                replacements = {game.game.event_id: game.as_member_payload() for game in calculated
                                if game.game.event_id in pending}
                merged = [replacements.get(str(item.get("event_id")), dict(item)) for item in rows]
                app.mlb_store.save_calibrated_snapshot(date_str, merged, note="管理員人工 SUPER 校正")
                result = app.mlb_store.confirm_daily_release(date_str, note="管理員人工 SUPER 校正")
            st.session_state.pop(f"mlb_manual_quotes:{date_str}", None)
            render_snapshot_result(st, result, "mlb")
            show_report(st, app.members.get_mlb_admin_preview(date_str), admin=True)
        except Exception:
            st.error("MLB 人工校正發布失敗，請確認所有盤口格式後重試。")


def _football_manual_section(app: AppServices, date_str: str) -> None:
    snapshot = app.football.get_member_snapshot(date_str)
    rows = list(snapshot.get("rows") or [])
    st.subheader("足球人工校正與發布")
    st.caption("逐場輸入標準亞洲盤與十進位賠率；儲存完成後再發布。")
    if not rows:
        st.info("請先執行足球自動快照，取得當天完整賽程後再人工校正。")
        return
    event_indexes = tuple(range(len(rows)))
    row = rows[st.selectbox(
        "選擇要校正的比賽", event_indexes,
        format_func=lambda index: _matchup_label(rows[index], "football"),
        key=f"football_event:{date_str}",
    )]
    event_id = str(row.get("event_id"))
    with st.form(f"football_manual:{date_str}:{event_id}"):
        spread_1, spread_2, spread_3 = st.columns(3)
        home_line_text = spread_1.text_input(
            "主隊亞洲讓分（- 主讓；+ 主受讓）", value="-0.5",
            help="只需填主隊一格。支援 -0.5、+0.5、-0/0.5、+0.5/1；客隊盤會自動反向。",
        )
        home_spread = spread_2.number_input("主隊讓分盤十進位賠率", min_value=1.01, value=1.94, step=0.01)
        away_spread = spread_3.number_input("客隊讓分盤十進位賠率", min_value=1.01, value=1.94, step=0.01)
        total_1, total_2, total_3 = st.columns(3)
        total_line = total_1.number_input("大小分盤", min_value=0.0, value=2.5, step=0.25)
        over_price = total_2.number_input("大分十進位賠率", min_value=1.01, value=1.94, step=0.01)
        under_price = total_3.number_input("小分十進位賠率", min_value=1.01, value=1.94, step=0.01)
        money_1, money_2, money_3 = st.columns(3)
        home_ml = money_1.number_input("主勝十進位賠率", min_value=1.01, value=2.00, step=0.01)
        draw_ml = money_2.number_input("和局十進位賠率", min_value=1.01, value=3.20, step=0.01)
        away_ml = money_3.number_input("客勝十進位賠率", min_value=1.01, value=3.00, step=0.01)
        save_event = st.form_submit_button("儲存此場人工校正")
    if save_event:
        try:
            home_line = _parse_home_asian_handicap(home_line_text)
            markets = [
                {"market_type": "spread", "side": "home", "line": home_line, "decimal_price": home_spread},
                {"market_type": "spread", "side": "away", "line": -home_line, "decimal_price": away_spread},
                {"market_type": "total", "side": "over", "line": total_line, "decimal_price": over_price},
                {"market_type": "total", "side": "under", "line": total_line, "decimal_price": under_price},
                {"market_type": "moneyline", "side": "home", "line": None, "decimal_price": home_ml},
                {"market_type": "moneyline", "side": "draw", "line": None, "decimal_price": draw_ml},
                {"market_type": "moneyline", "side": "away", "line": None, "decimal_price": away_ml},
            ]
            app.football.apply_manual_calibration(date_str, event_id, markets)
            home_direction = "主隊讓分" if home_line < 0 else "主隊受讓" if home_line > 0 else "平手盤"
            st.success(f"已儲存人工盤口：{_matchup_label(row, 'football')}｜{home_direction} {home_line:+g}；客隊自動對應 {-home_line:+g}")
        except (TypeError, ValueError) as exc:
            st.error(str(exc))
    if st.button("全部校正完成，發布足球"):
        try:
            app.football.confirm_daily_release(date_str, note="管理員人工亞洲盤校正")
            render_snapshot_result(st, {"status": "published", "updated_at": _taipei_now().isoformat(),
                                        "snapshot_kind": "manual", "member_available": True}, "football")
            show_report(st, app.members.get_football_admin_preview(date_str), admin=True)
        except Exception:
            st.error("尚有比賽未完成人工校正，請逐場儲存後再發布。")


def _admin_page(app: AppServices) -> None:
    _render_brand()
    st.header("後台｜資料更新、人工校正與發布")
    st.warning("僅此頁按鈕會抓取、運算或寫入資料；會員查詢頁不會執行這些操作。")
    selected = st.date_input("作業日期", value=_taipei_now().date(), key="admin_date")
    date_str = selected.isoformat()
    with st.expander("資料來源檢查與設定", expanded=False):
        st.caption("金鑰請放在 Streamlit Secrets，這裡不會顯示金鑰，也不會要求會員輸入。")
        if st.button("檢查資料連線", key="check_sources"):
            with st.spinner("正在檢查資料來源…"):
                st.session_state["source_health"] = check_sources(os.environ.get("THE_ODDS_API_KEY", "").strip(), os.environ.get("API_FOOTBALL_KEY", "").strip(), date_str)
        if "source_health" in st.session_state:
            st.dataframe(st.session_state["source_health"], hide_index=True, use_container_width=True)
        st.caption("The Odds API 金鑰 → THE_ODDS_API_KEY；API-Football 金鑰 → API_FOOTBALL_KEY。兩者不同，也不是帳戶登入密碼。")
    mlb_tab, football_tab = st.tabs(["MLB 後台", "足球後台"])

    with mlb_tab:
        st.subheader("MLB 自動存取快照")
        if st.button("立即執行 MLB 自動快照", type="primary"):
            key = os.environ.get("THE_ODDS_API_KEY", "")
            try:
                with st.spinner("正在取得 MLB 賽程、運算並儲存快照…"):
                    result = MLBAutoSnapshotRunner(app.mlb_store).run(date_str, key, now=_taipei_now())
                summary = render_snapshot_result(st, result, "mlb")
                if summary.success and summary.member_available == "是":
                    st.success("本次運算已保存，完整賽表顯示於下方。")
                    if not key:
                        st.info("未設定 The Odds API 金鑰：已保存 MLB 官方完整賽表；缺少可驗證市場盤的場次會標示 PASS。")
            except Exception:
                st.error("MLB 自動快照執行失敗，APP 已保護會員頁不受影響。請在「資料來源檢查」確認 MLB 官方與 The Odds API。")
        preview = app.members.get_mlb_admin_preview(date_str)
        if preview.allowed and preview.report is not None:
            st.markdown("#### 當天完整 MLB 賽表與推薦")
            show_report(st, preview, admin=True)
        _mlb_manual_section(app, selected, date_str)

    with football_tab:
        st.subheader("足球自動存取快照")
        if st.button("立即執行足球自動快照", type="primary"):
            try:
                with st.spinner("正在取得足球資料、運算並儲存快照…"):
                    result = app.football.run_football_auto_snapshot(date_str, app.seasons, now=_taipei_now())
                summary = render_snapshot_result(st, result, "football")
                if summary.success and summary.member_available == "是":
                    st.success("本次運算已保存，完整賽表顯示於下方。")
                    if not app.seasons:
                        st.info("未設定 API-Football 賽季：本次已使用 ESPN 賽程備援；未取得的補強資料會如實標示風險。")
            except Exception:
                st.error("足球自動快照執行失敗，APP 已保護會員頁不受影響。請在「資料來源檢查」確認 ESPN、API-Football、ClubElo 與 The Odds API。")
        preview = app.members.get_football_admin_preview(date_str)
        if preview.allowed and preview.report is not None:
            st.markdown("#### 當天完整足球賽表與推薦")
            show_report(st, preview, admin=True)
        _football_manual_section(app, date_str)


def main() -> None:
    role = st.session_state.get("user_role")
    if role not in {"member", "admin"}:
        _login()
    app = services()
    if role == "admin":
        page = st.radio("管理員功能", ["管理後台", "會員畫面預覽"], horizontal=True)
        if page == "管理後台": _admin_page(app)
        else: _member_page(app)
    else:
        _member_page(app)
    st.divider()
    if st.button("登出", key="logout"):
        st.session_state.clear()
        st.rerun()


if __name__ == "__main__":
    main()
