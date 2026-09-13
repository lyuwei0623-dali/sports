"""Local display vocabulary only; canonical provider names remain unchanged."""
import re
import unicodedata

LEAGUES = {"eng.1": "英超", "fra.1": "法甲", "ger.1": "德甲",
           "esp.1": "西甲", "ita.1": "義甲", "uefa.champions": "歐冠"}

def name_key(value):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower())

_NAMES = """
Arsenal|兵工廠
Aston Villa|阿斯頓維拉
Bournemouth|伯恩茅斯
Brentford|布倫特福德
Brighton|布萊頓
Brighton & Hove Albion|布萊頓
Burnley|伯恩利
Chelsea|切爾西
Crystal Palace|水晶宮
Everton|艾佛頓
Fulham|富勒姆
Leeds United|里茲聯
Leicester City|萊斯特城
Liverpool|利物浦
Manchester City|曼城
Manchester United|曼聯
Newcastle United|紐卡索聯
Nottingham Forest|諾丁漢森林
Sunderland|桑德蘭
Southampton|南安普敦
Tottenham Hotspur|托特納姆熱刺
West Ham United|西漢姆聯
Wolverhampton Wanderers|狼隊
Ipswich Town|伊普斯維奇
Real Madrid|皇家馬德里
Barcelona|巴塞隆納
Atletico Madrid|馬德里競技
Atlético Madrid|馬德里競技
Athletic Club|畢爾包競技
Athletic Bilbao|畢爾包競技
Real Sociedad|皇家社會
Real Betis|皇家貝提斯
Villarreal|比利亞雷亞爾
Valencia|瓦倫西亞
Sevilla|塞維利亞
Celta Vigo|塞爾塔
Malaga|馬拉加
Girona|赫羅納
Espanyol|西班牙人
Getafe|赫塔費
Osasuna|奧薩蘇納
Rayo Vallecano|巴列卡諾
Mallorca|馬略卡
Alaves|阿拉維斯
Levante|萊萬特
Elche|埃爾切
Real Oviedo|皇家奧維耶多
Valladolid|巴拉多利德
Las Palmas|拉斯帕爾馬斯
Leganes|萊加內斯
Paris Saint-Germain|巴黎聖日耳曼
Paris Saint Germain|巴黎聖日耳曼
Marseille|馬賽
Olympique Marseille|馬賽
Lyon|里昂
Olympique Lyonnais|里昂
Monaco|摩納哥
Lille|里爾
Nice|尼斯
Lens|朗斯
Rennes|雷恩
Strasbourg|史特拉斯堡
Brest|布雷斯特
Toulouse|土魯斯
Nantes|南特
Auxerre|歐塞爾
Angers|昂熱
Le Havre|勒阿弗爾
Metz|梅斯
Lorient|洛里昂
Paris FC|巴黎FC
Reims|蘭斯
Montpellier|蒙彼利埃
Saint-Etienne|聖伊天
Bayern Munich|拜仁慕尼黑
Bayern München|拜仁慕尼黑
Bayer Leverkusen|勒沃庫森
Borussia Dortmund|多特蒙德
RB Leipzig|RB萊比錫
Eintracht Frankfurt|法蘭克福
VfB Stuttgart|斯圖加特
Stuttgart|斯圖加特
SC Freiburg|弗萊堡
Freiburg|弗萊堡
Mainz|美因茲
Mainz 05|美因茲
Werder Bremen|文達不來梅
Borussia Monchengladbach|門興格拉德巴赫
Borussia Mönchengladbach|門興格拉德巴赫
Wolfsburg|沃爾夫斯堡
VfL Wolfsburg|沃爾夫斯堡
Augsburg|奧格斯堡
Union Berlin|柏林聯
Hoffenheim|霍芬海姆
TSG Hoffenheim|霍芬海姆
St. Pauli|聖保利
Heidenheim|海登海姆
Hamburg|漢堡
Hamburger SV|漢堡
Cologne|科隆
FC Cologne|科隆
1. FC Köln|科隆
Inter|國際米蘭
Internazionale|國際米蘭
Inter Milan|國際米蘭
AC Milan|AC米蘭
Milan|AC米蘭
Juventus|尤文圖斯
Napoli|拿坡里
Atalanta|亞特蘭大
Roma|羅馬
AS Roma|羅馬
Lazio|拉齊奧
Bologna|波隆那
Fiorentina|佛羅倫斯
Torino|都靈
Udinese|烏迪內斯
Genoa|熱那亞
Cagliari|卡利亞里
Parma|帕爾馬
Como|科莫
Lecce|萊切
Hellas Verona|維羅納
Verona|維羅納
Sassuolo|薩索洛
Pisa|比薩
Cremonese|克雷莫納
Empoli|恩波利
Monza|蒙扎
Benfica|本菲卡
FC Porto|波爾圖
Sporting CP|葡萄牙體育
Ajax|阿賈克斯
PSV Eindhoven|PSV恩荷芬
Feyenoord|飛燕諾
Celtic|塞爾提克
Rangers|格拉斯哥流浪者
Club Brugge|布魯日
Galatasaray|加拉塔薩雷
Fenerbahce|費內巴切
Red Bull Salzburg|薩爾斯堡紅牛
Shakhtar Donetsk|頓內次克礦工
"""
TEAM_NAMES = {name_key(a): b for a, b in (line.split("|") for line in _NAMES.strip().splitlines())}

def team_name(value):
    return TEAM_NAMES.get(name_key(value), str(value))

def translate_teams(text, home, away):
    value = str(text)
    for team in sorted((home, away), key=len, reverse=True):
        if team:
            value = value.replace(team, team_name(team))
    return value

# Original APP baseball display names, never used for provider identity.
TEAM_NAMES.update({name_key(k): v for k, v in {'New York Yankees': '洋基', 'Baltimore Orioles': '金鶯', 'Boston Red Sox': '紅襪', 'Tampa Bay Rays': '光芒', 'Toronto Blue Jays': '藍鳥', 'Chicago White Sox': '白襪', 'Cleveland Guardians': '守護者', 'Detroit Tigers': '老虎', 'Kansas City Royals': '皇家', 'Minnesota Twins': '雙城', 'Houston Astros': '太空人', 'Los Angeles Angels': '天使', 'Oakland Athletics': '運動家', 'Athletics': '運動家', 'Seattle Mariners': '水手', 'Texas Rangers': '遊騎兵', 'Atlanta Braves': '勇士', 'Miami Marlins': '馬林魚', 'New York Mets': '大都會', 'Philadelphia Phillies': '費城人', 'Washington Nationals': '國民', 'Chicago Cubs': '小熊', 'Cincinnati Reds': '紅人', 'Milwaukee Brewers': '釀酒人', 'Pittsburgh Pirates': '海盜', 'St. Louis Cardinals': '紅雀', 'Arizona Diamondbacks': '響尾蛇', 'Colorado Rockies': '落磯', 'Los Angeles Dodgers': '道奇', 'San Diego Padres': '教士', 'San Francisco Giants': '巨人'}.items()})
