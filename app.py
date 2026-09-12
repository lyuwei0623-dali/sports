"""維大力體育APP Streamlit entry point.

Member routes read stored snapshots only.  Admin-only buttons may fetch,
calculate and save snapshots.  Sport calculation rules remain in their modules.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from admin_snapshot_ui import render_snapshot_result
from app_services import AppServices, compose_services
from member_release_service import render_member_view
from mlb_pre_release_module import (
    MLBAutoSnapshotRunner, MLBPreReleaseService, SuperQuote, parse_super_line,
)

TZ_TAIPEI = ZoneInfo("Asia/Taipei")
APP_NAME = "維大力體育APP"

st.set_page_config(page_title=APP_NAME, page_icon="⚽", layout="wide")


@st.cache_resource(show_spinner=False)
def services() -> AppServices:
    return compose_services()


def _taipei_now() -> datetime:
    return datetime.now(TZ_TAIPEI)


def _render_brand() -> None:
    """Render the supplied anti-counterfeit logo when it is deployed."""
    logo = next((Path(name) for name in ("logo.png", "logo.jpg", "logo.jpeg") if Path(name).is_file()), None)
    logo_column, title_column = st.columns([1, 5])
    with logo_column:
        if logo is not None:
            st.image(str(logo), width=150)
    with title_column:
        st.title(APP_NAME)
        st.caption("MLB／Football 賽事數據分析與推薦")


def _render_sidebar_brand() -> None:
    logo = next((Path(name) for name in ("logo.png", "logo.jpg", "logo.jpeg") if Path(name).is_file()), None)
    with st.sidebar:
        if logo is not None:
            st.image(str(logo), use_container_width=True)
        st.markdown("### 維大力體育APP")
        st.caption("官方防偽識別｜請認明大力體育 Logo")


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
    st.caption("賽前推薦｜MLB／Football｜會員端只讀取已保存快照")
    selected = st.date_input("查詢日期", value=_taipei_now().date(), key="member_date")
    date_str = selected.isoformat()
    mlb_tab, football_tab = st.tabs(["⚾ MLB", "⚽ Football"])
    with mlb_tab:
        gate = app.members.get_mlb_member_view(date_str, now=_taipei_now())
        st.markdown(render_member_view(gate), unsafe_allow_html=True)
    with football_tab:
        gate = app.members.get_football_member_view(date_str, now=_taipei_now())
        st.markdown(render_member_view(gate), unsafe_allow_html=True)
    st.caption("機率高不等於值得下注。")


def _matchup_label(row: dict[str, object], sport: str) -> str:
    if sport == "mlb":
        teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
        return f"{teams.get('away', '客隊')} @ {teams.get('home', '主隊')}"
    return f"{row.get('away', '客隊')} @ {row.get('home', '主隊')}"


def _signed_mlb_line(raw: str, expected_sign: str) -> str:
    value = str(raw).strip().replace(" ", "")
    if not value:
        raise ValueError("讓分盤口不可空白")
    if value[0] in "+-":
        if value[0] != expected_sign:
            raise ValueError("讓分／受讓方向與盤口正負號不一致")
    else:
        value = expected_sign + value
    parse_super_line(value)
    return value


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
    options = {f"{_matchup_label(row, 'mlb')}｜{row.get('event_id')}": row for row in rows}
    chosen = st.selectbox("選擇要校正的比賽", tuple(options), key=f"mlb_event:{date_str}")
    row = options[chosen]
    event_id = str(row.get("event_id"))
    with st.form(f"mlb_manual:{date_str}:{event_id}"):
        direction = st.radio("主隊方向", ("主隊讓分", "主隊受讓"), horizontal=True)
        spread_1, spread_2 = st.columns(2)
        home_line = spread_1.text_input("主隊讓分盤口", value="-1.5" if direction == "主隊讓分" else "+1.5",
                                        help="支援 -1.5、-1+65、+1-50 等完整 SUPER 寫法")
        away_line = spread_2.text_input("客隊讓分盤口", value="+1.5" if direction == "主隊讓分" else "-1.5",
                                        help="請輸入與主隊相反方向的完整盤口")
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
            home_sign = "-" if direction == "主隊讓分" else "+"
            away_sign = "+" if direction == "主隊讓分" else "-"
            parse_super_line(total_line)
            quotes = [
                SuperQuote("spread", "home", _signed_mlb_line(home_line, home_sign), _positive(home_spread_price, "主隊讓分賠率")),
                SuperQuote("spread", "away", _signed_mlb_line(away_line, away_sign), _positive(away_spread_price, "客隊讓分賠率")),
                SuperQuote("total", "over", total_line.strip(), _positive(over_price, "大分賠率")),
                SuperQuote("total", "under", total_line.strip(), _positive(under_price, "小分賠率")),
                SuperQuote("moneyline", "home", None, _positive(home_moneyline, "主隊獨贏賠率")),
                SuperQuote("moneyline", "away", None, _positive(away_moneyline, "客隊獨贏賠率")),
            ]
            saved = st.session_state.setdefault(f"mlb_manual_quotes:{date_str}", {})
            saved[event_id] = quotes
            st.success(f"已暫存人工盤口：{_matchup_label(row, 'mlb')}")
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
            st.markdown(render_member_view(app.members.get_mlb_admin_preview(date_str)), unsafe_allow_html=True)
        except Exception:
            st.error("MLB 人工校正發布失敗，請確認所有盤口格式後重試。")


def _football_manual_section(app: AppServices, date_str: str) -> None:
    snapshot = app.football.get_member_snapshot(date_str)
    rows = list(snapshot.get("rows") or [])
    st.subheader("Football 人工校正與發布")
    st.caption("逐場輸入標準亞洲盤與十進位賠率；儲存完成後再發布。")
    if not rows:
        st.info("請先執行 Football 自動快照，取得當天完整賽程後再人工校正。")
        return
    options = {f"{_matchup_label(row, 'football')}｜{row.get('event_id')}": row for row in rows}
    chosen = st.selectbox("選擇要校正的比賽", tuple(options), key=f"football_event:{date_str}")
    row = options[chosen]
    event_id = str(row.get("event_id"))
    with st.form(f"football_manual:{date_str}:{event_id}"):
        spread_1, spread_2, spread_3 = st.columns(3)
        home_line = spread_1.number_input("主隊亞洲讓分（受讓填正數）", value=-0.5, step=0.25)
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
            st.success(f"已儲存人工盤口：{_matchup_label(row, 'football')}")
        except Exception:
            st.error("Football 人工盤口儲存失敗，請確認盤口與賠率格式。")
    if st.button("全部校正完成，發布 Football"):
        try:
            app.football.confirm_daily_release(date_str, note="管理員人工亞洲盤校正")
            render_snapshot_result(st, {"status": "published", "updated_at": _taipei_now().isoformat(),
                                        "snapshot_kind": "manual", "member_available": True}, "football")
            st.markdown(render_member_view(app.members.get_football_admin_preview(date_str)), unsafe_allow_html=True)
        except Exception:
            st.error("尚有比賽未完成人工校正，請逐場儲存後再發布。")


def _admin_page(app: AppServices) -> None:
    _render_brand()
    st.header("後台｜資料更新、人工校正與發布")
    st.warning("僅此頁按鈕會抓取、運算或寫入資料；會員查詢頁不會執行這些操作。")
    selected = st.date_input("作業日期", value=_taipei_now().date(), key="admin_date")
    date_str = selected.isoformat()
    mlb_tab, football_tab = st.tabs(["MLB 後台", "Football 後台"])

    with mlb_tab:
        st.subheader("MLB 自動存取快照")
        if st.button("立即執行 MLB 自動快照", type="primary"):
            key = os.environ.get("THE_ODDS_API_KEY", "")
            if not key:
                st.error("尚未設定 MLB 所需資料來源，請確認後台設定。")
            else:
                try:
                    with st.spinner("正在取得 MLB 資料、運算並儲存快照…"):
                        result = MLBAutoSnapshotRunner(app.mlb_store).run(date_str, key, now=_taipei_now())
                    summary = render_snapshot_result(st, result, "mlb")
                    if summary.success and summary.member_available == "是":
                        st.success("本次運算已保存，完整賽表顯示於下方。")
                except Exception:
                    st.error("MLB 自動快照執行失敗，APP 已保護會員頁不受影響。請確認部署檔案版本與資料來源設定。")
        preview = app.members.get_mlb_admin_preview(date_str)
        if preview.allowed and preview.report is not None:
            st.markdown("#### 當天完整 MLB 賽表與推薦")
            st.markdown(render_member_view(preview), unsafe_allow_html=True)
        _mlb_manual_section(app, selected, date_str)

    with football_tab:
        st.subheader("Football 自動存取快照")
        if st.button("立即執行 Football 自動快照", type="primary"):
            try:
                with st.spinner("正在取得足球資料、運算並儲存快照…"):
                    result = app.football.run_football_auto_snapshot(date_str, app.seasons, now=_taipei_now())
                summary = render_snapshot_result(st, result, "football")
                if summary.success and summary.member_available == "是":
                    st.success("本次運算已保存，完整賽表顯示於下方。")
            except Exception:
                st.error("Football 自動快照執行失敗，APP 已保護會員頁不受影響。請確認聯賽賽季與資料來源設定。")
        preview = app.members.get_football_admin_preview(date_str)
        if preview.allowed and preview.report is not None:
            st.markdown("#### 當天完整 Football 賽表與推薦")
            st.markdown(render_member_view(preview), unsafe_allow_html=True)
        _football_manual_section(app, date_str)


def main() -> None:
    _render_sidebar_brand()
    role = st.session_state.get("user_role")
    if role not in {"member", "admin"}:
        _login()
    app = services()
    if role == "admin":
        _admin_page(app)
    else:
        _member_page(app)
    with st.sidebar:
        st.divider()
        st.caption("維大力體育APP｜會員端唯讀快照")
        if st.button("登出"):
            st.session_state.clear()
            st.rerun()


if __name__ == "__main__":
    main()
