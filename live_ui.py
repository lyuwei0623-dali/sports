"""Independent local-only scenario UI. Does not access pre-match snapshots."""
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from live_calculator import BASE_SOCCER_ELO, SOCCER_GOALS, RE24, rng_for, soccer_samples, simulate_mlb_finish, settlement_summary
from football_display import team_name, LEAGUES

def render_live(st):
    st.subheader("走地計算機")
    st.warning("原版情境試算｜使用原版固定基準，尚未完成實盤回測；結果不列為正式下注推薦。")
    st.caption("不會自動抓取賽況。請核對比分與盤口；任何比賽事件發生後，舊試算即不適用。")
    sport = st.radio("運動",["MLB","足球"],horizontal=True,key="live_sport")
    with st.form("live_inputs"):
        if sport == "足球":
            league = st.selectbox("聯賽",list(SOCCER_GOALS),format_func=lambda x:LEAGUES.get(x,x))
            clubs = sorted(BASE_SOCCER_ELO)
            home = st.selectbox("主隊",clubs,format_func=team_name)
            away = st.selectbox("客隊",clubs,index=1,format_func=team_name)
            minute = st.number_input("比賽分鐘（正規時間）",0,89,60)
        else:
            home = st.text_input("主隊",value="")
            away = st.text_input("客隊",value="")
            inning = st.number_input("局數",1,9,7)
            top = st.radio("半局",["上半局","下半局"],horizontal=True)
            outs = st.selectbox("出局數",[0,1,2])
            bases = st.selectbox("壘包",list(RE24[0]),format_func=lambda x:{"Empty":"無人在壘","1B":"一壘","2B":"二壘","3B":"三壘","12B":"一二壘","13B":"一三壘","23B":"二三壘","Loaded":"滿壘"}[x])
        a,b = st.columns(2)
        hs = a.number_input("主隊目前得分",0,30,0)
        aws = b.number_input("客隊目前得分",0,30,0)
        if sport == "足球":
            hr = a.number_input("主隊紅牌",0,3,0)
            ar = b.number_input("客隊紅牌",0,3,0)
            hy = a.number_input("主隊黃牌",0,8,0)
            ay = b.number_input("客隊黃牌",0,8,0)
        market = st.selectbox("試算盤種",["主隊獨贏","客隊獨贏","主隊讓分","客隊讓分","大分","小分"]+ (["和局"] if sport=="足球" else []))
        line = st.number_input("所選隊伍讓分／大小分數字（獨贏不使用）",value=0.0,step=0.25)
        price = st.number_input("目前十進位賠率（香港0.95請填1.95）",min_value=1.01,max_value=1000.0,value=1.95,step=0.01)
        confirmed = st.checkbox("已確認目前比分與報價；足球盤按全場比分結算")
        submit = st.form_submit_button("計算本次情境",type="primary")
    if not submit:
        return
    if not confirmed or not home.strip() or not away.strip() or home.strip()==away.strip():
        st.error("請確認賽況，並選擇或填入不同的主客隊。");return
    try:
        if sport == "足球":
            h,a = soccer_samples(home,away,league,hs,aws,minute,hr,ar,hy,ay)
        else:
            if inning == 9 and top == "下半局" and hs > aws:
                raise ValueError("此狀態下比賽通常已結束，請核對")
            if market in ("主隊讓分","客隊讓分","大分","小分") and not float(line*2).is_integer():
                raise ValueError("MLB 走地僅支援標準整數或半分盤；SUPER特殊盤不套用足球拆盤")
            h,a = simulate_mlb_finish(rng_for(home,away,hs,aws,inning,top,outs,bases),10000,4.05,4.05,
                                     inning,top=="上半局",hs,aws,RE24[outs][bases])
        if market in ("大分","小分") and line <= 0:
            raise ValueError("大小分盤必須大於0")
        if market in ("主隊獨贏","客隊獨贏","和局"):
            win = h>a if market=="主隊獨贏" else a>h if market=="客隊獨贏" else h==a
            result = settlement_summary(np.where(win,1,-1),0,price)
        else:
            values, handicap = ((h-a,line) if market=="主隊讓分" else (a-h,line) if market=="客隊讓分"
                                else (h+a,-line) if market=="大分" else (-h-a,line))
            result = settlement_summary(values,handicap,price)
        st.caption("試算時間："+datetime.now(ZoneInfo("Asia/Taipei")).strftime("%m/%d %H:%M:%S")+"（台灣）")
        st.write(f"本次條件：{home} {hs}：{aws} {away} · {market} {line:g} · 賠率 {price:g}")
        st.dataframe([{"項目":k,"估計值":f"{v:+.2%}" if k=="EV" else f"{v:.2%}" if k!="公允賠率" else (f"{v:.3f}" if v else "無法估計")} for k,v in result.items()],hide_index=True)
        st.info("以上為假設條件下的機率與 EV，並非已驗證投注優勢。")
    except (ValueError,RuntimeError) as exc:
        st.error(str(exc))
