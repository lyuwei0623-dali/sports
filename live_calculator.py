"""Extracted legacy live calculations, isolated from all pre-match modules.
Archived assumptions are not validated live recommendations. No HTTP or database.
"""

import numpy as np
import math,hashlib,json

def rng_for(*parts):
    seed = int(hashlib.sha256(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()[:16], 16)
    return np.random.default_rng(seed)

def total_probabilities(totals, line):
    return float(np.mean(totals > line)), float(np.mean(totals < line)), float(np.mean(totals == line))

def handicap_outcomes(diff, handicap):
    # Asian handicap: quarter lines split into their two adjacent half-lines.
    h = float(handicap)
    if not np.isclose(h * 4, round(h * 4)):
        raise ValueError("讓球必須為 0.25 的倍數")
    lines = [math.floor(h * 2) / 2, math.ceil(h * 2) / 2] if round(h*4) % 2 else [h]
    settlements = np.mean([np.sign(np.asarray(diff) + line) for line in lines], axis=0)
    return {str(value): float(np.mean(settlements == value)) for value in [-1., -.5, 0., .5, 1.]}

def nb_runs(rng, mean, size):
    if mean < 0 or not math.isfinite(mean):
        raise ValueError("得分期望必須是非負有限值")
    if mean == 0:
        return np.zeros(size, dtype=int)
    return rng.negative_binomial(mean / .35, 1 / 1.35, size)

def simulate_mlb_finish(rng, size, home_mean, away_mean, inning=1, is_top=True,
                        current_home=0, current_away=0, current_half_mean=None):
    """Half-inning approximation, including skipped home ninth and consistent extra-inning scores.
    Walk-off increments stop at one-run lead (walk-off HR not modeled); extra-inning mean is an assumption.
    """
    if not 1 <= inning <= 9:
        raise ValueError("目前支援正規九局內的試算；延長賽請等待模型升級")
    h = np.full(size, current_home, dtype=int)
    a = np.full(size, current_away, dtype=int)
    active = np.ones(size, dtype=bool)
    for inn in range(inning, 10):
        if inn != inning or is_top:
            mean = current_half_mean if inn == inning and current_half_mean is not None else away_mean / 9
            a[active] += nb_runs(rng, mean, int(active.sum()))
        if inn >= 9:
            active &= h <= a
        mean = current_half_mean if inn == inning and not is_top and current_half_mean is not None else home_mean / 9
        h[active] += nb_runs(rng, mean, int(active.sum()))
        if inn >= 9:
            h[active] = np.minimum(h[active], a[active] + 1)
    tied = h == a
    for _ in range(100):
        if not tied.any(): break
        n = int(tied.sum())
        extra_a = nb_runs(rng, max(.1, away_mean / 9 + .35), n)
        extra_h = nb_runs(rng, max(.1, home_mean / 9 + .35), n)
        a[tied] += extra_a
        h[tied] += np.minimum(extra_h, extra_a + 1)
        tied = h == a
    if tied.any():
        raise RuntimeError("延長賽模擬未收斂")
    return h, a

BASE_SOCCER_ELO = {
    "Real Madrid": 2010.0, "Barcelona": 1935.0, "Atletico Madrid": 1865.0, "Girona": 1785.0,
    "Athletic Club": 1805.0, "Athletic": 1805.0, "Real Sociedad": 1775.0, "Villarreal": 1775.0,
    "Real Betis": 1745.0, "Sevilla": 1710.0, "Celta Vigo": 1685.0, "Celta": 1685.0,
    "Osasuna": 1675.0, "Mallorca": 1680.0, "Valencia": 1690.0, "Rayo Vallecano": 1680.0,
    "Las Palmas": 1650.0, "Getafe": 1660.0, "Alaves": 1660.0, "Leganes": 1635.0,
    "Espanyol": 1655.0, "Valladolid": 1625.0, "Manchester City": 2020.0, "Arsenal": 1985.0,
    "Liverpool": 1970.0, "Chelsea": 1835.0, "Tottenham": 1815.0, "Tottenham Hotspur": 1815.0,
    "Newcastle": 1815.0, "Newcastle United": 1815.0, "Aston Villa": 1835.0, "Manchester United": 1785.0,
    "Brighton": 1775.0, "Brighton & Hove Albion": 1775.0, "West Ham": 1725.0, "West Ham United": 1725.0,
    "Fulham": 1715.0, "Bournemouth": 1705.0, "Brentford": 1705.0, "Crystal Palace": 1715.0,
    "Wolves": 1685.0, "Wolverhampton Wanderers": 1685.0, "Everton": 1690.0, "Nottingham Forest": 1685.0,
    "Leicester": 1675.0, "Southampton": 1635.0, "Ipswich": 1615.0, "Ipswich Town": 1615.0,
    "Hull City": 1650.0, "Hull": 1650.0, "Sunderland": 1660.0, "Coventry City": 1640.0,
    "Coventry": 1640.0, "Leeds United": 1670.0, "Leeds": 1670.0, "Bayern Munich": 1955.0,
    "Bayer Leverkusen": 1945.0, "Borussia Dortmund": 1875.0, "RB Leipzig": 1875.0, "Stuttgart": 1835.0,
    "Eintracht Frankfurt": 1785.0, "Freiburg": 1745.0, "Wolfsburg": 1725.0, "Mainz": 1705.0,
    "Augsburg": 1705.0, "Werder Bremen": 1715.0, "Inter": 1975.0, "Internazionale": 1975.0,
    "Atalanta": 1885.0, "Juventus": 1875.0, "Milan": 1865.0, "AC Milan": 1865.0,
    "Roma": 1805.0, "Lazio": 1805.0, "Napoli": 1825.0, "Bologna": 1815.0,
    "Fiorentina": 1775.0, "Torino": 1755.0, "Paris Saint-Germain": 1925.0, "Monaco": 1835.0,
    "Lille": 1815.0, "Marseille": 1785.0, "Lyon": 1775.0, "Nice": 1775.0,
    "Lens": 1765.0, "Brest": 1765.0, "Rennes": 1755.0
}

SOCCER_GOALS = {
    "eng.1": {"home": 1.55, "away": 1.25}, "esp.1": {"home": 1.40, "away": 1.10},
    "ger.1": {"home": 1.65, "away": 1.35}, "ita.1": {"home": 1.45, "away": 1.15},
    "fra.1": {"home": 1.42, "away": 1.12}, "uefa.champions": {"home": 1.58, "away": 1.28}
}

SOCCER_LEAGUE_TEAMS = {
    "eng.1": [
        "Manchester City", "Arsenal", "Liverpool", "Chelsea", "Tottenham Hotspur", 
        "Manchester United", "Newcastle United", "Aston Villa", "Brighton & Hove Albion", 
        "West Ham United", "Fulham", "Wolverhampton Wanderers", "Everton", "Brentford", 
        "Crystal Palace", "Bournemouth", "Nottingham Forest", "Leicester City", "Ipswich Town", "Southampton"
    ],
    "esp.1": [
        "Real Madrid", "Barcelona", "Atletico Madrid", "Girona", "Athletic Club", 
        "Real Sociedad", "Real Betis", "Villarreal", "Sevilla", "Valencia", 
        "Osasuna", "Celta Vigo", "Mallorca", "Rayo Vallecano", "Las Palmas", 
        "Getafe", "Alaves", "Espanyol", "Leganes", "Valladolid"
    ],
    "ger.1": [
        "Bayern Munich", "Bayer Leverkusen", "Borussia Dortmund", "RB Leipzig", "Stuttgart", 
        "Eintracht Frankfurt", "Freiburg", "Wolfsburg", "Mainz", "Augsburg", "Werder Bremen"
    ],
    "ita.1": [
        "Inter", "Atalanta", "Juventus", "Milan", "Roma", "Lazio", "Napoli", "Bologna", "Fiorentina", "Torino"
    ],
    "fra.1": [
        "Paris Saint-Germain", "Monaco", "Lille", "Marseille", "Lyon", "Nice", "Lens", "Brest", "Rennes"
    ],
    "uefa.champions": [
        "Real Madrid", "Manchester City", "Bayern Munich", "Arsenal", "Barcelona", 
        "Paris Saint-Germain", "Liverpool", "Inter", "Bayer Leverkusen", "Atletico Madrid", 
        "Borussia Dortmund", "Juventus", "Milan", "Atalanta", "Aston Villa", "RB Leipzig"
    ]
}

RE24 = {
            0: {"Empty": 0.48, "1B": 0.86, "2B": 1.10, "3B": 1.35, "12B": 1.44, "13B": 1.70, "23B": 1.96, "Loaded": 2.28},
            1: {"Empty": 0.25, "1B": 0.51, "2B": 0.67, "3B": 0.95, "12B": 0.93, "13B": 1.14, "23B": 1.38, "Loaded": 1.54},
            2: {"Empty": 0.10, "1B": 0.22, "2B": 0.32, "3B": 0.36, "12B": 0.44, "13B": 0.48, "23B": 0.58, "Loaded": 0.75}
        }

def soccer_samples(home_team, away_team, league_slug, curr_home_score, curr_away_score, minute,
                   home_red=0, away_red=0, home_yellow=0, away_yellow=0):
    if home_team == away_team or home_team not in BASE_SOCCER_ELO or away_team not in BASE_SOCCER_ELO:
        raise ValueError("缺少原版球隊基準，無法試算")
    if not 0 <= minute < 90:
        raise ValueError("目前僅支援正規時間第0至89分鐘；補時與延長賽不套用本試算")
    if min(curr_home_score,curr_away_score,home_red,away_red,home_yellow,away_yellow) < 0:
        raise ValueError("比分與牌數不可為負數")
    rng = rng_for("soccer_live",home_team,away_team,curr_home_score,curr_away_score,minute,
                  home_red,away_red,home_yellow,away_yellow)
    # 1. 戰力基礎差距
    diff = BASE_SOCCER_ELO.get(home_team, 1650.0) - BASE_SOCCER_ELO.get(away_team, 1650.0)
    base_goals = SOCCER_GOALS.get(league_slug, {"home": 1.50, "away": 1.20})

    # 2. 時間衰減因子 (補時考量)
    time_factor = max(0.02, (90.0 - minute) / 90.0)

    # 3. 比分激勵/落後追分 (Trailing urgency)
    h_trail_boost = 1.20 if (curr_home_score < curr_away_score and minute >= 60) else 1.0
    a_trail_boost = 1.20 if (curr_away_score < curr_home_score and minute >= 60) else 1.0

    # 4. 紅黃牌動態壓制模型 (黃牌累積導致防守偏保守，每張黃牌微幅提升失球風險 2.5%)
    h_card_mult = max(0.25, 1.0 - home_red * 0.35 - home_yellow * 0.025) * (1.0 + away_red * 0.25 + away_yellow * 0.02)
    a_card_mult = max(0.25, 1.0 - away_red * 0.35 - away_yellow * 0.025) * (1.0 + home_red * 0.25 + home_yellow * 0.02)

    rem_lh = max(0.05, base_goals["home"] * (1.0 + diff / 550.0) * 1.15 * time_factor * h_trail_boost * h_card_mult)
    rem_la = max(0.04, base_goals["away"] * (1.0 - diff / 550.0) * time_factor * a_trail_boost * a_card_mult)

    # 5. 蒙特卡羅模擬
    sim_h_rem = rng.poisson(rem_lh, 10000)
    sim_a_rem = rng.poisson(rem_la, 10000)

    final_h = curr_home_score + sim_h_rem
    final_a = curr_away_score + sim_a_rem
    final_tot = final_h + final_a

    return final_h, final_a


def settlement_summary(values, line, decimal_price):
    if not math.isfinite(decimal_price) or decimal_price <= 1:
        raise ValueError("請輸入大於1的十進位賠率")
    outcomes = handicap_outcomes(values, line)
    win_weight = outcomes["1.0"] + outcomes["0.5"] / 2
    loss_weight = outcomes["-1.0"] + outcomes["-0.5"] / 2
    ev = win_weight * (decimal_price - 1) - loss_weight
    return {"全贏":outcomes["1.0"],"半贏":outcomes["0.5"],"走盤":outcomes["0.0"],
            "半輸":outcomes["-0.5"],"全輸":outcomes["-1.0"],"EV":ev,
            "公允賠率":1+loss_weight/win_weight if win_weight else None}
