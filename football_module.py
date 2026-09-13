"""Independent pre-match football module.

Public entry points:
  * FootballModule.refresh_daily_snapshot(...)  # one backend/admin operation
  * FootballModule.apply_manual_calibration(...) # admin only
  * FootballModule.confirm_daily_release(...)    # admin only
  * FootballModule.get_member_snapshot(...)      # sole member/integration entry

This module deliberately has no Streamlit, Core, MLB, in-play or SUPER imports.
The member/integration entry never fetches an external API: it only reads SQLite
rows saved by the daily backend refresh and administrator calibration step.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
import re
import sqlite3
import csv
import unicodedata
from io import StringIO
from typing import Any, Iterable, Mapping, Optional

import numpy as np
try:  # requests is preferred in production, but the module remains portable.
    import requests
except ModuleNotFoundError:  # pragma: no cover - used only in minimal runtimes
    from types import SimpleNamespace
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    class _Response:
        def __init__(self, raw: bytes, status_code: int):
            self.status_code, self.text = status_code, raw.decode("utf-8")
            self.ok = 200 <= status_code < 300
        def json(self): return json.loads(self.text)
        def raise_for_status(self):
            if not self.ok: raise URLError(f"HTTP {self.status_code}")

    def _get(url, headers=None, params=None, timeout=10):
        if params: url += ("&" if "?" in url else "?") + urlencode(params)
        try:
            with urlopen(Request(url, headers=dict(headers or {})), timeout=timeout) as r:
                return _Response(r.read(), r.status)
        except (URLError, HTTPError) as exc:
            raise exc
    requests = SimpleNamespace(get=_get, RequestException=(URLError, HTTPError))


TZ_UTC = timezone.utc
TZ_TAIPEI = timezone(timedelta(hours=8))
DEFAULT_LEAGUES = {
    "eng.1": 39, "esp.1": 140, "ger.1": 78,
    "ita.1": 135, "fra.1": 61, "uefa.champions": 2,
}


@dataclass(frozen=True)
class FootballConfig:
    db_path: str
    api_football_key: str
    odds_api_key: str = ""
    api_football_base: str = "https://v3.football.api-sports.io"
    odds_api_base: str = "https://api.the-odds-api.com/v4"
    odds_regions: str = "eu"
    min_ev: float = 0.03
    simulations: int = 30000
    max_market_age_seconds: int = 900


class AutoSnapshotDiagnosticError(RuntimeError):
    """Expected backend snapshot outcome with a safe, displayable diagnosis."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FootballModule:
    """Persistence and calculation boundary for the Football module only."""

    def __init__(self, config: FootballConfig) -> None:
        self.config = config
        self._init_db()

    @classmethod
    def from_environment(cls, db_path: str) -> "FootballModule":
        return cls(FootballConfig(
            db_path=db_path,
            api_football_key=os.environ.get("API_FOOTBALL_KEY", ""),
            odds_api_key=os.environ.get("THE_ODDS_API_KEY", ""),
            odds_regions=os.environ.get("FOOTBALL_ODDS_REGIONS", "eu").strip() or "eu",
        ))

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS football_daily_runs (
              date_str TEXT PRIMARY KEY, fetched_at TEXT NOT NULL,
              status TEXT NOT NULL, source_summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS football_events (
              date_str TEXT NOT NULL, event_id TEXT NOT NULL,
              league_key TEXT NOT NULL, kickoff TEXT NOT NULL,
              home_team TEXT NOT NULL, away_team TEXT NOT NULL,
              raw_json TEXT NOT NULL, model_json TEXT NOT NULL,
              PRIMARY KEY(date_str,event_id)
            );
            CREATE TABLE IF NOT EXISTS football_market_reference (
              date_str TEXT NOT NULL, event_id TEXT NOT NULL, market_type TEXT NOT NULL,
              side TEXT NOT NULL, line REAL, price REAL, provider TEXT NOT NULL,
              observed_at TEXT NOT NULL, source_count INTEGER NOT NULL, raw_json TEXT NOT NULL,
              PRIMARY KEY(date_str,event_id,market_type,side,provider)
            );
            CREATE TABLE IF NOT EXISTS football_calibrated_markets (
              date_str TEXT NOT NULL, event_id TEXT NOT NULL, market_type TEXT NOT NULL,
              side TEXT NOT NULL, line REAL, decimal_price REAL, source TEXT NOT NULL,
              updated_at TEXT NOT NULL, PRIMARY KEY(date_str,event_id,market_type,side)
            );
            CREATE TABLE IF NOT EXISTS football_recommendations (
              date_str TEXT NOT NULL, event_id TEXT NOT NULL, market_type TEXT NOT NULL,
              side TEXT NOT NULL, line REAL, decimal_price REAL, model_probability REAL,
              implied_probability REAL, ev REAL, playable INTEGER NOT NULL, label TEXT NOT NULL,
              generated_at TEXT NOT NULL, PRIMARY KEY(date_str,event_id,market_type,side)
            );
            CREATE TABLE IF NOT EXISTS football_release_log (
              date_str TEXT PRIMARY KEY, status TEXT NOT NULL, confirmed_at TEXT,
              published_at TEXT, note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS football_manual_snapshots (
              date_str TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
              published_at TEXT NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS football_automatic_snapshots (
              date_str TEXT PRIMARY KEY, status TEXT NOT NULL,
              payload_json TEXT NOT NULL, updated_at TEXT, market_observed_at TEXT,
              last_attempt_at TEXT NOT NULL, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS football_automatic_snapshot_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT, date_str TEXT NOT NULL,
              status TEXT NOT NULL, payload_json TEXT NOT NULL,
              updated_at TEXT, market_observed_at TEXT, attempted_at TEXT NOT NULL,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS football_automatic_snapshot_history_lookup
              ON football_automatic_snapshot_history(date_str, id DESC);
            -- Presentation audit metadata only.  This trigger records an
            -- immutable copy of a manual market after it has been validated
            -- and saved; it does not participate in pricing or +EV logic.
            CREATE TABLE IF NOT EXISTS football_calibration_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date_str TEXT NOT NULL, event_id TEXT NOT NULL, market_type TEXT NOT NULL,
              side TEXT NOT NULL, line REAL, decimal_price REAL NOT NULL,
              source TEXT NOT NULL, recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS football_calibration_history_lookup
              ON football_calibration_history(date_str, event_id, market_type, side, id);
            CREATE TRIGGER IF NOT EXISTS football_capture_calibration_history
            AFTER INSERT ON football_calibrated_markets
            BEGIN
              INSERT INTO football_calibration_history
                (date_str,event_id,market_type,side,line,decimal_price,source,recorded_at)
              VALUES
                (NEW.date_str,NEW.event_id,NEW.market_type,NEW.side,NEW.line,
                 NEW.decimal_price,NEW.source,NEW.updated_at);
            END;
            """)

    # --------------------------- backend fetch layer -------------------------
    def refresh_daily_snapshot(self, date_str: str, season_by_league: Mapping[str, int]) -> dict[str, Any]:
        """Run once from a protected backend job or admin action.

        It is intentionally the only method that contacts API-Football, ESPN,
        ClubElo or The Odds API.  It may safely be retried; event rows are
        replaced atomically per date.  Member UI must never call this method.
        """
        self._source_diagnostics = {}
        self._api_football_odds_stats = {"attempted": 0, "events": 0, "quotes": 0}
        target = date.fromisoformat(date_str)
        espn_index = self._fetch_espn_fixtures(target)
        api_events: list[dict[str, Any]] = []
        api_football_status = "not_configured"
        valid_seasons = {
            league: season for league, season in season_by_league.items()
            if league in DEFAULT_LEAGUES and isinstance(season, int) and season >= 1900
        }
        if not self.config.api_football_key:
            self._source_diagnostics["API-Football"] = "未設定金鑰，使用 ESPN 賽程"
        elif not valid_seasons:
            # ESPN remains a valid schedule source. A missing optional
            # enrichment configuration must not turn a member-visible daily
            # schedule into an application-wide failure.
            self._source_diagnostics["API-Football"] = "未設定可用賽季，使用 ESPN 賽程"
            api_football_status = "season_not_configured"
        else:
            try:
                api_events = self._fetch_api_football_fixtures(target, valid_seasons)
                api_football_status = "ok" if api_events else "empty"
            except Exception as exc:
                self._source_diagnostics["API-Football"] = _provider_failure(exc)
                # API-Football is enrichment.  A provider/key/quota problem must
                # not erase an otherwise valid ESPN schedule.
                api_football_status = "unavailable"
        primary_events = list(api_events)
        seen = {(e["league_key"], _team_key(e["home"]), _team_key(e["away"])) for e in api_events}
        for event in self._espn_events_as_primary(espn_index):
            identity = (event["league_key"], _team_key(event["home"]), _team_key(event["away"]))
            if identity not in seen:
                primary_events.append(event)
                seen.add(identity)
        elo = self._fetch_clubelo(target)
        odds = self._fetch_odds_consensus(target)
        # The Odds API is used first.  If it has no internally valid market for
        # an API-Football fixture, use that already configured provider's
        # pre-match odds endpoint as a narrowly-scoped fallback.  This avoids
        # replacing the Football model or inventing a price, and it avoids a
        # second provider call for events that already have usable odds.
        fallback_by_event: dict[str, list[dict[str, Any]]] = {}
        for event in primary_events:
            if _has_usable_market_records(_match_odds(odds, event)):
                continue
            fallback = self._fetch_api_football_prematch_odds(event)
            if _has_usable_market_records(fallback):
                fallback_by_event[str(event["event_id"])] = fallback
        stored = 0
        with self._db() as conn:
            conn.execute("DELETE FROM football_events WHERE date_str=?", (date_str,))
            conn.execute("DELETE FROM football_market_reference WHERE date_str=?", (date_str,))
            for event in primary_events:
                event = self._merge_espn_backup(event, espn_index)
                event["elo_snapshot"] = {"home": elo.get(_team_key(event["home"]), 1650.0),
                                         "away": elo.get(_team_key(event["away"]), 1650.0)}
                model = self._build_base_model(event, elo)
                conn.execute("""INSERT INTO football_events
                  VALUES(?,?,?,?,?,?,?,?)""", (
                    date_str, event["event_id"], event["league_key"], event["kickoff"],
                    event["home"], event["away"], json.dumps(event, ensure_ascii=False),
                    json.dumps(model, ensure_ascii=False),
                ))
                matched = _match_odds(odds, event)
                if not _has_usable_market_records(matched):
                    matched = fallback_by_event.get(str(event["event_id"]), matched)
                for q in matched:
                    conn.execute("""INSERT INTO football_market_reference VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                        date_str, event["event_id"], q["market_type"], q["side"], q["line"],
                        q["price"], q["provider"], q["observed_at"], q["source_count"],
                        json.dumps(q, ensure_ascii=False),
                    ))
                stored += 1
            matched_count = conn.execute("SELECT COUNT(*) FROM football_market_reference WHERE date_str=?", (date_str,)).fetchone()[0]
            priced_events = conn.execute("SELECT COUNT(DISTINCT event_id) FROM football_market_reference WHERE date_str=?", (date_str,)).fetchone()[0]
            fallback_stats = dict(self._api_football_odds_stats)
            self._source_diagnostics["盤口匹配"] = (
                f"The Odds API {len(odds)} 筆；API-Football 賽前盤補強 {fallback_stats['events']} 場；"
                f"可用盤口 {priced_events}/{len(primary_events)} 場"
            )
            unmatched = sorted({name for e in primary_events for name in (e["home"], e["away"]) if _team_key(name) not in elo})
            if not elo:
                self._source_diagnostics["ClubElo 匹配"] = "來源未取得評分，尚無法進行匹配"
            elif not unmatched:
                self._source_diagnostics["ClubElo 匹配"] = "當日賽程隊伍已完整匹配"
            else:
                self._source_diagnostics["ClubElo 匹配"] = (
                    f"未對應 {len(unmatched)} 隊：" + "、".join(unmatched)
                )
            summary = {"api_football_events": len(api_events), "api_football_status": api_football_status,
                       "espn_events": len(espn_index), "primary_events": len(primary_events),
                       "clubelo_teams": len(elo), "odds_quotes": len(odds),
                       "api_football_prematch_events": fallback_stats["events"],
                       "api_football_prematch_quotes": fallback_stats["quotes"],
                       "market_reference_quotes": int(matched_count), "market_reference_events": int(priced_events),
                       "diagnostics": dict(getattr(self, "_source_diagnostics", {}))}
            conn.execute("""INSERT INTO football_daily_runs VALUES(?,?,?,?)
              ON CONFLICT(date_str) DO UPDATE SET fetched_at=excluded.fetched_at,
              status=excluded.status,source_summary=excluded.source_summary""",
              (date_str, _now(), "awaiting_manual_calibration", json.dumps(summary)))
        return {"date": date_str, "events": stored, "sources": summary}

    # ----------------------- backend automatic snapshot ----------------------
    def run_football_auto_snapshot(self, date_str: str, season_by_league: Mapping[str, int],
                                   now: Optional[datetime] = None) -> dict[str, Any]:
        """Administrator-only operation that creates one saved automatic snapshot.

        It reuses the existing Football fetch and recommendation machinery, then
        persists a separate automatic display snapshot.  It never changes a
        published manual snapshot.  This method must not be exposed to member
        routes; ``get_member_snapshot`` remains read-only.  It is intentionally
        invoked by an administrator action, not a background scheduler.
        """
        attempted_at = _timestamp(now)
        try:
            refresh_result = self.refresh_daily_snapshot(date_str, season_by_league)
            if not refresh_result.get("events"):
                raise AutoSnapshotDiagnosticError("no_events", "當日查無可用足球賽事")
            rows, run_metadata, market_observed_at = self._build_automatic_snapshot_payload(date_str, attempted_at)
            payload = json.dumps({"rows": rows, "run_metadata": run_metadata}, ensure_ascii=False)
            with self._db() as conn:
                conn.execute("""INSERT INTO football_automatic_snapshots
                  (date_str,status,payload_json,updated_at,market_observed_at,last_attempt_at,last_error)
                  VALUES(?,?,?,?,?,?,NULL)
                  ON CONFLICT(date_str) DO UPDATE SET status='valid',payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at,market_observed_at=excluded.market_observed_at,
                    last_attempt_at=excluded.last_attempt_at,last_error=NULL""",
                    (date_str, "valid", payload, attempted_at, market_observed_at, attempted_at))
                conn.execute("""INSERT INTO football_automatic_snapshot_history
                  (date_str,status,payload_json,updated_at,market_observed_at,attempted_at,error)
                  VALUES(?,?,?,?,?,?,NULL)""",
                  (date_str, "valid", payload, attempted_at, market_observed_at, attempted_at))
            return {"date": date_str, "status": "valid", "snapshot_kind": "automatic",
                    "updated_at": attempted_at, "events": len(rows), "member_visible": True,
                    "market_status": run_metadata.get("market_status", "")}
        except Exception as exc:
            code, message = _auto_snapshot_diagnostic(exc)
            # Keep the prior valid payload intact.  A failed replacement is only
            # an attempt record, so the member page can disclose staleness.
            with self._db() as conn:
                existing = conn.execute("SELECT status FROM football_automatic_snapshots WHERE date_str=?", (date_str,)).fetchone()
                if existing and existing["status"] == "valid":
                    conn.execute("UPDATE football_automatic_snapshots SET last_attempt_at=?,last_error=? WHERE date_str=?",
                                 (attempted_at, f"{code}: {message}", date_str))
                    conn.execute("""INSERT INTO football_automatic_snapshot_history
                      (date_str,status,payload_json,updated_at,market_observed_at,attempted_at,error)
                      SELECT date_str,'failed',payload_json,updated_at,market_observed_at,?,?
                      FROM football_automatic_snapshots WHERE date_str=?""", (attempted_at, f"{code}: {message}", date_str))
                    return {"date": date_str, "status": "stale", "snapshot_kind": "automatic",
                            "reason_code": code, "message": message, "member_visible": True}
                conn.execute("""INSERT INTO football_automatic_snapshots
                  (date_str,status,payload_json,updated_at,market_observed_at,last_attempt_at,last_error)
                  VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(date_str) DO UPDATE SET status='failed',last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error""", (date_str, "failed", "{}", None, None, attempted_at, f"{code}: {message}"))
                conn.execute("""INSERT INTO football_automatic_snapshot_history
                  (date_str,status,payload_json,updated_at,market_observed_at,attempted_at,error)
                  VALUES(?,?,?,?,?,?,?)""", (date_str, "failed", "{}", None, None, attempted_at, f"{code}: {message}"))
            return {"date": date_str, "status": "failed", "snapshot_kind": "automatic",
                    "reason_code": code, "message": message, "member_visible": False}

    def run_football_auto_snapshot_job(self, date_str: str, season_by_league: Mapping[str, int],
                                       now: Optional[datetime] = None) -> dict[str, Any]:
        """Deprecated: Streamlit deployment has no reliable background scheduler."""
        del season_by_league, now
        return {"date": date_str, "status": "disabled",
                "message": "背景排程已停用；請由管理員後台手動執行自動存取快照。"}

    def _validate_auto_snapshot_seasons(self, seasons: Mapping[str, int]) -> None:
        configured = [(league, seasons.get(league)) for league in DEFAULT_LEAGUES]
        if not any(isinstance(value, int) and value >= 1900 for _, value in configured):
            raise AutoSnapshotDiagnosticError("league_season_configuration_error", "聯賽或賽季設定不正確，未提供可用賽季")

    def _build_automatic_snapshot_payload(self, date_str: str, updated_at: str) -> tuple[list[dict[str, Any]], dict[str, Any], Optional[str]]:
        """Build a display payload from saved backend data and standard football markets."""
        with self._db() as conn:
            events = conn.execute("SELECT event_id,league_key,kickoff,home_team,away_team,model_json FROM football_events WHERE date_str=? ORDER BY kickoff,event_id", (date_str,)).fetchall()
            references = conn.execute("""SELECT event_id,market_type,side,line,price,observed_at
              FROM football_market_reference WHERE date_str=? ORDER BY event_id,observed_at""", (date_str,)).fetchall()
            run = conn.execute("SELECT source_summary FROM football_daily_runs WHERE date_str=?", (date_str,)).fetchone()
        if not events:
            raise AutoSnapshotDiagnosticError("no_events", "當日查無可用足球賽事")
        reference_by_event: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        market_observed_at: Optional[str] = None
        for item in references:
            entry = {"market_type": item["market_type"], "side": item["side"], "line": item["line"], "decimal_price": item["price"]}
            # The refresh layer already stores The Odds API's standard consensus.
            reference_by_event.setdefault(item["event_id"], {})[(item["market_type"], item["side"])] = entry
            market_observed_at = max(market_observed_at or "", item["observed_at"] or "") or market_observed_at

        rows: list[dict[str, Any]] = []
        for event in events:
            markets = list(reference_by_event.get(event["event_id"], {}).values())
            normalised: list[dict[str, Any]] = []
            market_warning = ""
            if markets:
                try:
                    normalised = [_validate_market(market) for market in markets]
                    _validate_market_set(normalised)
                except (TypeError, ValueError):
                    normalised = []
                    market_warning = "自動盤口格式不完整，本場先保留賽程並標示 PASS"
            else:
                market_warning = "尚未取得可用盤口，本場先保留賽程並標示 PASS"
            model = json.loads(event["model_json"])
            recommendations = self._calculate_recommendations(model, normalised) if normalised else []
            risk = model.get("risk", {})
            row = {
                "event_id": event["event_id"], "league_key": event["league_key"], "forecast": _saved_forecast(model), "sport": "football", "kickoff": event["kickoff"],
                "home": event["home_team"], "away": event["away_team"], "model": model,
                "risk": _risk_display(risk), "risk_display": _risk_display(risk),
                "warning": _join_warning("自動更新／尚未人工校正", market_warning, _risk_warning(risk)),
                "settlement_status": "pending",
                "first_market": _markets_display(normalised, event["home_team"], event["away_team"])
                                if normalised else "自動盤口未取得",
                "latest_market": _markets_display(normalised, event["home_team"], event["away_team"])
                                 if normalised else "自動盤口未取得",
                "market_change": "自動快照盤口（尚無人工校正歷程）",
                "market_evaluations": recommendations,
                "recommendations": [],
            }
            for recommendation in recommendations:
                if not recommendation["playable"]:
                    continue
                display = _market_selection_display(recommendation, event["home_team"], event["away_team"])
                row["recommendations"].append({**recommendation, "display": display, "selection": display})
            rows.append(row)
        if not rows:
            raise AutoSnapshotDiagnosticError("no_events", "當日查無可用足球賽事")
        metadata = self.get_run_metadata(date_str)
        source_summary = _decode_source_summary(run["source_summary"] if run else None)
        metadata.update({
            "release_status": "automatic_available",
            "calibration_source": "自動更新／尚未人工校正",
            "updated_at": updated_at,
            "market_observed_at": market_observed_at,
            "market_status": _automatic_market_status(source_summary, rows),
        })
        return rows, metadata, market_observed_at

    def _fetch_api_football_fixtures(self, target: date, seasons: Mapping[str, int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        headers = {"x-apisports-key": self.config.api_football_key}
        for league_key, league_id in DEFAULT_LEAGUES.items():
            season = seasons.get(league_key)
            if not season:
                continue
            response = requests.get(f"{self.config.api_football_base}/fixtures", headers=headers,
                params={"league": league_id, "season": season, "date": target.isoformat(), "timezone": "Asia/Taipei"}, timeout=12)
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                errors = body["errors"]
                codes = set(errors) if isinstance(errors, dict) else set()
                reason = ("API-Football 額度或請求速率限制" if codes & {"rateLimit", "requests"} else
                          "API-Football 方案、認證或賽季參數遭拒")
                if not hasattr(self, "_source_diagnostics"):
                    self._source_diagnostics = {}
                self._source_diagnostics["API-Football " + league_key] = reason
                continue
            for item in body.get("response", []):
                fixture = item.get("fixture", {})
                teams = item.get("teams", {})
                if not fixture.get("id") or not teams.get("home", {}).get("name"):
                    continue
                home_id, away_id = teams["home"].get("id"), teams["away"].get("id")
                team_stats = {
                    "home": self._fetch_team_statistics(headers, league_id, season, home_id, target),
                    "away": self._fetch_team_statistics(headers, league_id, season, away_id, target),
                }
                injuries = self._fetch_fixture_injuries(headers, fixture["id"])
                lineups = self._fetch_fixture_lineups(headers, fixture["id"])
                out.append({"event_id": str(fixture["id"]), "league_key": league_key,
                    "kickoff": fixture.get("date"), "home": teams["home"]["name"],
                    "away": teams["away"]["name"], "home_id": home_id, "away_id": away_id,
                    "status": fixture.get("status", {}).get("short", "NS"),
                    "team_statistics": team_stats, "injuries": injuries, "lineups": lineups,
                    "api_source": "API-Football"})
        return out

    def _espn_events_as_primary(
        self, espn_index: Mapping[tuple[str, str], Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Turn the existing ESPN verification feed into a schedule fallback."""
        out: list[dict[str, Any]] = []
        for event in espn_index.values():
            competition = (event.get("competitions") or [{}])[0]
            competitors = competition.get("competitors") or []
            by_side = {item.get("homeAway"): item for item in competitors}
            home = by_side.get("home", {}).get("team", {})
            away = by_side.get("away", {}).get("team", {})
            if not event.get("id") or not home.get("name") or not away.get("name"):
                continue
            league_key = str(event.get("_league_key") or "football")
            out.append({
                "event_id": f"espn:{league_key}:{event['id']}",
                "league_key": league_key,
                "kickoff": event.get("date") or competition.get("date"),
                "home": home["name"], "away": away["name"],
                "home_id": home.get("id"), "away_id": away.get("id"),
                "status": ((event.get("status") or {}).get("type") or {}).get("state", "pre"),
                "team_statistics": {"home": {}, "away": {}},
                "injuries": [], "lineups": [], "api_source": "ESPN fallback",
                "espn_verified": True,
            })
        return out

    def _fetch_team_statistics(self, headers: Mapping[str, str], league: int, season: int,
                               team: Optional[int], target: date) -> dict[str, Any]:
        if not team:
            return {}
        r = requests.get(f"{self.config.api_football_base}/teams/statistics", headers=headers,
            params={"league": league, "season": season, "team": team, "date": target.isoformat()}, timeout=12)
        if not r.ok:
            return {}
        return r.json().get("response") or {}

    def _fetch_fixture_injuries(self, headers: Mapping[str, str], fixture_id: int) -> list[dict[str, Any]]:
        r = requests.get(f"{self.config.api_football_base}/injuries", headers=headers,
            params={"fixture": fixture_id}, timeout=12)
        return r.json().get("response", []) if r.ok else []

    def _fetch_fixture_lineups(self, headers: Mapping[str, str], fixture_id: int) -> list[dict[str, Any]]:
        """Returns confirmed lineups when the provider has them; [] means unconfirmed."""
        r = requests.get(f"{self.config.api_football_base}/fixtures/lineups", headers=headers,
            params={"fixture": fixture_id}, timeout=12)
        return r.json().get("response", []) if r.ok else []

    def _merge_espn_backup(self, event: dict[str, Any], espn_index: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, Any]:
        backup = espn_index.get((_team_key(event["home"]), _team_key(event["away"])))
        event["espn_verified"] = bool(backup)
        if backup:
            event["espn_event_id"] = str(backup.get("id", ""))
        return event

    def _fetch_espn_fixtures(self, target: date) -> dict[tuple[str, str], dict[str, Any]]:
        """ESPN is a verification/fallback feed only, never a model feature."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for league_key in DEFAULT_LEAGUES:
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_key}/scoreboard"
                r = requests.get(url, params={"dates": (target-timedelta(days=1)).strftime("%Y%m%d") + "-" + target.strftime("%Y%m%d"), "limit": 100}, timeout=8)
                r.raise_for_status()
                for event in r.json().get("events", []) if r.ok else []:
                    if _local_date(event.get("date")) != target.isoformat():
                        continue
                    comp = (event.get("competitions") or [{}])[0]
                    names = {c.get("homeAway"): c.get("team", {}).get("name", "") for c in comp.get("competitors", [])}
                    if names.get("home") and names.get("away"):
                        saved = dict(event)
                        saved["_league_key"] = league_key
                        out[(_team_key(names["home"]), _team_key(names["away"]))] = saved
            except Exception as exc:
                if not hasattr(self, "_source_diagnostics"):
                    self._source_diagnostics = {}
                self._source_diagnostics["ESPN " + league_key] = _provider_failure(exc)
                continue
        return out

    def _fetch_clubelo(self, target: date) -> dict[str, float]:
        # Do not request a future rating snapshot. Never label fallback ratings current.
        rating_day = min(target, datetime.now(TZ_TAIPEI).date())
        if not hasattr(self, "_source_diagnostics"):
            self._source_diagnostics = {}
        try:
            r = requests.get(f"https://api.clubelo.com/{rating_day.isoformat()}", timeout=15)
            r.raise_for_status()
            records = list(csv.DictReader(StringIO(r.text.lstrip("\ufeff"))))
            ratings = {_team_key(row["Club"]): float(row["Elo"]) for row in records}
            self._source_diagnostics["ClubElo"] = f"評分日期 {rating_day}，取得 {len(ratings)} 隊"
            return ratings
        except Exception as exc:
            self._source_diagnostics["ClubElo"] = _provider_failure(exc)
            return {}

    def _fetch_odds_consensus(self, target: date) -> list[dict[str, Any]]:
        if not hasattr(self, "_source_diagnostics"):
            self._source_diagnostics = {}
        if not self.config.odds_api_key:
            self._source_diagnostics["The Odds API"] = "未設定金鑰"
            return []
        keys = {"eng.1": "soccer_epl", "esp.1": "soccer_spain_la_liga", "ger.1": "soccer_germany_bundesliga",
                "ita.1": "soccer_italy_serie_a", "fra.1": "soccer_france_ligue_one", "uefa.champions": "soccer_uefa_champs_league"}
        out = []
        for league, sport_key in keys.items():
            try:
                r = requests.get(f"{self.config.odds_api_base}/sports/{sport_key}/odds", params={
                    "apiKey": self.config.odds_api_key, "regions": self.config.odds_regions,
                    "markets": "h2h,spreads,totals", "oddsFormat": "decimal", "dateFormat": "iso"}, timeout=12)
                r.raise_for_status()
                events = r.json()
                if not isinstance(events, list):
                    raise ValueError("invalid response")
                before = len(out)
                for event in events:
                    if _local_date(event.get("commence_time")) != target.isoformat():
                        continue
                    out.extend(_consensus_event(league, event))
                self._source_diagnostics["盤口 " + league] = f"API 成功 {len(events)} 場；目標台灣日期 {len(out)-before} 筆盤口"
            except Exception as exc:
                self._source_diagnostics["盤口 " + league] = _provider_failure(exc)
        return out

    def _fetch_api_football_prematch_odds(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Fetch saved pre-match odds only for an unmatched API-Football event.

        This remains strictly inside the administrator refresh path.  ESPN
        fallback rows have no API-Football fixture id, so they are never guessed
        into another event.  Returned values go through the existing standard
        market validation and the unchanged Asian/+EV calculation.
        """

        stats = getattr(self, "_api_football_odds_stats", None)
        if not isinstance(stats, dict):
            stats = self._api_football_odds_stats = {"attempted": 0, "events": 0, "quotes": 0}
        if not self.config.api_football_key:
            self._source_diagnostics.setdefault("API-Football 賽前盤", "未設定金鑰，無法補強未配對盤口")
            return []
        fixture_id = str(event.get("event_id") or "")
        if str(event.get("api_source") or "") != "API-Football" or not fixture_id.isdigit():
            return []
        stats["attempted"] += 1
        try:
            response = requests.get(
                f"{self.config.api_football_base}/odds",
                headers={"x-apisports-key": self.config.api_football_key},
                params={"fixture": fixture_id}, timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("invalid response")
            if payload.get("errors"):
                self._source_diagnostics["API-Football 賽前盤"] = "來源回應無可用賽前盤口或方案未開放"
                return []
            records = _api_football_prematch_records(event, payload)
            if records:
                stats["events"] += 1
                stats["quotes"] += len(records)
                self._source_diagnostics["API-Football 賽前盤"] = (
                    f"未配對賽事補強成功 {stats['events']} 場／{stats['quotes']} 筆"
                )
            elif "API-Football 賽前盤" not in self._source_diagnostics:
                self._source_diagnostics["API-Football 賽前盤"] = "未配對賽事尚未提供可驗證賽前盤口"
            return records
        except Exception as exc:
            self._source_diagnostics["API-Football 賽前盤"] = _provider_failure(exc)
            return []

    # -------------------------- Base Model: football only ---------------------
    def _build_base_model(self, event: Mapping[str, Any], elo: Mapping[str, float]) -> dict[str, Any]:
        home_stats = event.get("team_statistics", {}).get("home", {})
        away_stats = event.get("team_statistics", {}).get("away", {})
        home_attack, home_defence = _team_rates(home_stats, "home")
        away_attack, away_defence = _team_rates(away_stats, "away")
        league_home, league_away = _league_goal_prior(event["league_key"])
        home_elo = elo.get(_team_key(event["home"]), 1650.0)
        away_elo = elo.get(_team_key(event["away"]), 1650.0)
        elo_adjust = max(.80, min(1.20, 1.0 + (home_elo - away_elo) / 2200.0))
        home_injuries = _injury_count(event.get("injuries", []), event.get("home"))
        away_injuries = _injury_count(event.get("injuries", []), event.get("away"))
        # xG rates are preferred; goals are a bounded fallback.  Injuries are a
        # small risk adjustment until confirmed lineups can replace them.
        lh = max(.25, math.sqrt(home_attack * away_defence) * league_home * elo_adjust * (1 - .035 * home_injuries))
        la = max(.20, math.sqrt(away_attack * home_defence) * league_away / elo_adjust * (1 - .035 * away_injuries))
        quality = {"home_stats": bool(home_stats), "away_stats": bool(away_stats),
                   "clubelo": _team_key(event["home"]) in elo and _team_key(event["away"]) in elo,
                   "injury_count": {"home": home_injuries, "away": away_injuries}}
        return {"lambda_home": round(lh, 4), "lambda_away": round(la, 4),
                "projected_total": round(lh + la, 4), "quality": quality,
                "risk": _assess_risk(event, quality),
                "notes": "API-Football form/statistics + ClubElo; market data excluded from Base Model"}

    def refresh_fixture_readiness(self, date_str: str, event_id: str) -> dict[str, Any]:
        """Protected pre-release check for confirmation of lineups/injuries.

        This is never called by member traffic. It refreshes only one event;
        when confirmed information changes, the saved Base Model and any already
        calibrated +EV rows are recalculated automatically.
        """
        if not self.config.api_football_key:
            raise RuntimeError("API_FOOTBALL_KEY is required")
        with self._db() as conn:
            row = conn.execute("SELECT raw_json FROM football_events WHERE date_str=? AND event_id=?", (date_str, str(event_id))).fetchone()
        if not row:
            raise KeyError("event is not in the saved daily snapshot")
        event = json.loads(row["raw_json"])
        headers = {"x-apisports-key": self.config.api_football_key}
        event["injuries"] = self._fetch_fixture_injuries(headers, int(event_id))
        event["lineups"] = self._fetch_fixture_lineups(headers, int(event_id))
        # Elo and team form remain frozen from the daily run; this call updates
        # volatile availability only, preserving the daily model audit trail.
        old_model = self._load_model(date_str, str(event_id))
        saved_elo = event.get("elo_snapshot", {})
        model = self._build_base_model(event, {
            _team_key(event["home"]): float(saved_elo.get("home", 1650.0)),
            _team_key(event["away"]): float(saved_elo.get("away", 1650.0)),
        })
        model["quality"]["clubelo"] = old_model.get("quality", {}).get("clubelo", False)
        with self._db() as conn:
            conn.execute("UPDATE football_events SET raw_json=?,model_json=? WHERE date_str=? AND event_id=?",
                (json.dumps(event, ensure_ascii=False), json.dumps(model, ensure_ascii=False), date_str, str(event_id)))
            calibrated = [dict(x) for x in conn.execute("SELECT market_type,side,line,decimal_price FROM football_calibrated_markets WHERE date_str=? AND event_id=?", (date_str,str(event_id))).fetchall()]
        if calibrated:
            self.apply_manual_calibration(date_str, str(event_id), calibrated, source="會員平台人工校準（確認先發後重算）")
        return {"event_id": str(event_id), "risk": model["risk"], "recalculated": bool(calibrated)}

    def _load_model(self, date_str: str, event_id: str) -> dict[str, Any]:
        with self._db() as conn:
            row=conn.execute("SELECT model_json FROM football_events WHERE date_str=? AND event_id=?",(date_str,event_id)).fetchone()
        if not row: raise KeyError("event is not in the saved daily snapshot")
        return json.loads(row["model_json"])

    # -------------------------- manual calibration layer ----------------------
    def apply_manual_calibration(self, date_str: str, event_id: str,
                                 markets: Iterable[Mapping[str, Any]],
                                 source: str = "會員平台人工校準") -> list[dict[str, Any]]:
        """Save confirmed member-platform odds and recompute football +EV.

        markets use decimal odds and conventional Asian lines only.  Valid rows:
          {market_type: moneyline, side: home|away|draw, decimal_price: 2.10}
          {market_type: spread, side: home|away, line: -0.75|+0.75, decimal_price: 1.94}
          {market_type: total, side: over|under, line: 2.5|2.75, decimal_price: 1.94}
        """
        normalised = [_validate_market(m) for m in markets]
        _validate_market_set(normalised)
        with self._db() as conn:
            row = conn.execute("SELECT model_json FROM football_events WHERE date_str=? AND event_id=?", (date_str, str(event_id))).fetchone()
            if not row:
                raise KeyError("event must be included in the saved daily Football snapshot")
            conn.execute("DELETE FROM football_calibrated_markets WHERE date_str=? AND event_id=?", (date_str, str(event_id)))
            for m in normalised:
                conn.execute("INSERT INTO football_calibrated_markets VALUES(?,?,?,?,?,?,?,?)", (
                    date_str, str(event_id), m["market_type"], m["side"], m.get("line"),
                    m["decimal_price"], source, _now()))
            model = json.loads(row["model_json"])
        results = self._calculate_recommendations(model, normalised)
        with self._db() as conn:
            conn.execute("DELETE FROM football_recommendations WHERE date_str=? AND event_id=?", (date_str, str(event_id)))
            for r in results:
                conn.execute("INSERT INTO football_recommendations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    date_str, str(event_id), r["market_type"], r["side"], r.get("line"), r["decimal_price"],
                    r["model_probability"], r["implied_probability"], r["ev"], int(r["playable"]),
                    r["label"], _now()))
        return results

    def confirm_daily_release(self, date_str: str, note: str = "") -> None:
        """Admin gate: only calibrated events with a saved recommendation run can publish."""
        with self._db() as conn:
            events = [r[0] for r in conn.execute("SELECT event_id FROM football_events WHERE date_str=?", (date_str,))]
            complete = {r[0] for r in conn.execute("SELECT DISTINCT event_id FROM football_recommendations WHERE date_str=?", (date_str,))}
            if not events or set(events) - complete:
                raise ValueError("all events must be manually calibrated and recalculated before release")
            now = _now()
            conn.execute("INSERT INTO football_release_log VALUES(?,?,?,?,?) ON CONFLICT(date_str) DO UPDATE SET status=excluded.status,confirmed_at=excluded.confirmed_at,published_at=excluded.published_at,note=excluded.note",
                (date_str, "published", now, now, note))
            conn.execute("UPDATE football_daily_runs SET status='published' WHERE date_str=?", (date_str,))
        # A published manual payload is immutable from the auto-snapshot path.
        # It is display persistence only; no model or market logic is changed.
        published_rows = self.get_published_rows(date_str)
        for row in published_rows:
            row["forecast"] = _saved_forecast(row["model"])
        payload = json.dumps({"rows": published_rows, "run_metadata": self.get_run_metadata(date_str)}, ensure_ascii=False)
        with self._db() as conn:
            conn.execute("""INSERT INTO football_manual_snapshots VALUES(?,?,?,?,?)
              ON CONFLICT(date_str) DO UPDATE SET payload_json=excluded.payload_json,
                published_at=excluded.published_at,note=excluded.note,updated_at=excluded.updated_at""",
                (date_str, payload, now, note, now))

    def _calculate_recommendations(self, model: Mapping[str, Any], markets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        seed = abs(hash(json.dumps(model, sort_keys=True))) % (2**32)
        rng = np.random.default_rng(seed)
        home = rng.poisson(float(model["lambda_home"]), self.config.simulations)
        away = rng.poisson(float(model["lambda_away"]), self.config.simulations)
        total, results = home + away, []
        for m in markets:
            p, ev = None, None
            if m["market_type"] == "moneyline":
                p = float(np.mean(home > away) if m["side"] == "home" else np.mean(away > home) if m["side"] == "away" else np.mean(home == away))
                ev = p * m["decimal_price"] - 1
            elif m["market_type"] == "spread":
                values = home - away if m["side"] == "home" else away - home
                ev, p = _asian_ev(values, float(m["line"]), m["decimal_price"])
            elif m["market_type"] == "total":
                values = total if m["side"] == "over" else -total
                handicap = -float(m["line"]) if m["side"] == "over" else float(m["line"])
                ev, p = _asian_ev(values, handicap, m["decimal_price"])
            playable = bool(ev is not None and ev >= self.config.min_ev)
            results.append({**m, "model_probability": p, "implied_probability": 1 / m["decimal_price"],
                            "ev": ev, "playable": playable,
                            "label": "可推薦" if playable else "PASS｜未達 +EV 門檻"})
        return results

    # ---------------------- internal display read layer -----------------------
    def get_published_rows(self, date_str: str) -> list[dict[str, Any]]:
        """Internal SQLite display reader; member routes must use get_member_snapshot."""
        with self._db() as conn:
            rows = conn.execute("""SELECT e.event_id,e.league_key,e.kickoff,e.home_team,e.away_team,e.model_json,
              r.market_type,r.side,r.line,r.decimal_price,r.model_probability,r.ev,r.playable,r.label
              FROM football_events e LEFT JOIN football_recommendations r
              ON r.date_str=e.date_str AND r.event_id=e.event_id
              WHERE e.date_str=? ORDER BY e.kickoff,e.event_id,r.ev DESC""", (date_str,)).fetchall()
            saved_markets = conn.execute("""SELECT event_id,market_type,side,line,decimal_price,source,updated_at
              FROM football_calibrated_markets WHERE date_str=?
              ORDER BY event_id,market_type,side""", (date_str,)).fetchall()
            history = conn.execute("""SELECT id,event_id,market_type,side,line,decimal_price,source,recorded_at
              FROM football_calibration_history WHERE date_str=?
              ORDER BY event_id,market_type,side,id""", (date_str,)).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        market_by_event: dict[str, list[Mapping[str, Any]]] = {}
        for market in saved_markets:
            market_by_event.setdefault(market["event_id"], []).append(dict(market))
        history_by_event: dict[str, list[Mapping[str, Any]]] = {}
        for market in history:
            history_by_event.setdefault(market["event_id"], []).append(dict(market))
        for row in rows:
            key = row["event_id"]
            model = json.loads(row["model_json"])
            if key not in grouped:
                current = market_by_event.get(key, [])
                first, latest = _first_and_latest_calibrations(history_by_event.get(key, []), current)
                risk = model.get("risk", {})
                record = grouped[key] = {
                    "event_id": key, "league_key": row["league_key"], "forecast": model.get("display_forecast", {}), "sport": "football", "kickoff": row["kickoff"],
                    "home": row["home_team"], "away": row["away_team"], "model": model,
                    "risk": _risk_display(risk), "risk_display": _risk_display(risk),
                    "warning": _risk_warning(risk), "settlement_status": "pending",
                    "first_market": _markets_display(first, row["home_team"], row["away_team"]),
                    "latest_market": _markets_display(latest, row["home_team"], row["away_team"]),
                    "market_change": _market_change_display(first, latest, row["home_team"], row["away_team"]),
                    "market_evaluations": [],
                    "recommendations": [],
                }
            if row["market_type"]:
                record["market_evaluations"].append({k: row[k] for k in ("market_type", "side", "line", "decimal_price", "model_probability", "ev", "playable", "label")})
            if row["market_type"] and row["playable"]:
                recommendation = {k: row[k] for k in ("market_type", "side", "line", "decimal_price", "model_probability", "ev", "playable", "label")}
                recommendation["display"] = _market_selection_display(recommendation, row["home_team"], row["away_team"])
                recommendation["selection"] = recommendation["display"]
                record["recommendations"].append(recommendation)
        return list(grouped.values())

    def get_run_metadata(self, date_str: str) -> dict[str, Any]:
        """Internal SQLite metadata reader; member routes must use get_member_snapshot."""
        with self._db() as conn:
            run = conn.execute("SELECT fetched_at,status,source_summary FROM football_daily_runs WHERE date_str=?", (date_str,)).fetchone()
            release = conn.execute("SELECT status,confirmed_at,published_at FROM football_release_log WHERE date_str=?", (date_str,)).fetchone()
            calibration = conn.execute("""SELECT source,MAX(updated_at) AS updated_at
              FROM football_calibrated_markets WHERE date_str=? GROUP BY source ORDER BY updated_at DESC""", (date_str,)).fetchall()
            has_events = conn.execute("SELECT EXISTS(SELECT 1 FROM football_events WHERE date_str=?)", (date_str,)).fetchone()[0]
            has_all_recommendations = conn.execute("""SELECT NOT EXISTS(
              SELECT 1 FROM football_events e WHERE e.date_str=? AND NOT EXISTS(
                SELECT 1 FROM football_recommendations r WHERE r.date_str=e.date_str AND r.event_id=e.event_id))
              """, (date_str,)).fetchone()[0]

        source_summary = _decode_source_summary(run["source_summary"] if run else None)
        timestamps = [run["fetched_at"]] if run else []
        timestamps.extend(item["updated_at"] for item in calibration if item["updated_at"])
        if release:
            timestamps.extend(value for value in (release["confirmed_at"], release["published_at"]) if value)
        stored_status = release["status"] if release else (run["status"] if run else "failed")
        if stored_status == "awaiting_manual_calibration" and has_events and has_all_recommendations:
            stored_status = "ready_to_publish"
        release_status = stored_status if stored_status in {
            "fetching", "awaiting_manual_calibration", "ready_to_publish", "published", "failed"
        } else "failed"
        return {
            "sources": _sources_display(source_summary),
            "calibration_source": "、".join(item["source"] for item in calibration) or "尚未人工校正",
            "updated_at": max(timestamps) if timestamps else None,
            "release_status": release_status,
        }

    def get_member_snapshot(self, date_str: str) -> dict[str, Any]:
        """Return the sole Football member/integration snapshot from SQLite.

        ``get_published_rows`` and ``get_run_metadata`` are internal data-read
        layers.  Member routes and integration code must call this method only.
        It does not publish, recalculate, refresh, contact a provider, or write.
        Core's ``member_release_gate`` remains the sole owner of release-status
        enforcement and the Taiwan 19:30 publication-time decision.
        """
        # Read priority is fixed: published manual > valid automatic > no data.
        # Do not add refresh, calibration, model or publication calls here.
        with self._db() as conn:
            manual = conn.execute("SELECT payload_json,published_at FROM football_manual_snapshots WHERE date_str=?", (date_str,)).fetchone()
            release = conn.execute("SELECT status,published_at FROM football_release_log WHERE date_str=?", (date_str,)).fetchone()
            automatic = conn.execute("""SELECT status,payload_json,updated_at,market_observed_at,last_error
              FROM football_automatic_snapshots WHERE date_str=?""", (date_str,)).fetchone()
        if manual:
            payload = _decode_snapshot_payload(manual["payload_json"])
            metadata = dict(payload["run_metadata"])
            metadata["release_status"] = "published"
            metadata["updated_at"] = manual["published_at"]
            return {
                "release_status": "published", "rows": payload["rows"], "run_metadata": metadata,
                "snapshot_kind": "manual", "calibration_state": "manual_published",
                "stale_warning": None, "updated_at": manual["published_at"],
            }
        if release and release["status"] == "published":
            # Backward-compatible read for a release made before the manual
            # snapshot table was introduced.  New releases persist above.
            metadata = self.get_run_metadata(date_str)
            return {
                "release_status": "published", "rows": self.get_published_rows(date_str), "run_metadata": metadata,
                "snapshot_kind": "manual", "calibration_state": "manual_published",
                "stale_warning": None, "updated_at": release["published_at"] or metadata.get("updated_at"),
            }
        if automatic and automatic["status"] == "valid":
            payload = _decode_snapshot_payload(automatic["payload_json"])
            metadata = dict(payload["run_metadata"])
            stale_warning = None
            if automatic["last_error"]:
                stale_warning = f"自動更新失敗，正顯示最後有效快照（更新於 {automatic['updated_at']}）"
            metadata.update({"release_status": "automatic_available", "updated_at": automatic["updated_at"],
                             "market_observed_at": automatic["market_observed_at"],
                             "calibration_source": "自動更新／尚未人工校正"})
            return {
                "release_status": "automatic_available", "rows": payload["rows"], "run_metadata": metadata,
                "snapshot_kind": "automatic", "calibration_state": "尚未人工校正",
                "stale_warning": stale_warning, "updated_at": automatic["updated_at"],
            }
        # No valid snapshot: show only the saved lifecycle state, never active
        # un-snapshotted rows that could be mistaken for a published recommendation.
        metadata = self.get_run_metadata(date_str)
        if automatic and automatic["status"] == "failed":
            metadata["release_status"] = "failed"
        elif not automatic and metadata["release_status"] != "failed":
            metadata["release_status"] = "fetching"
        return {
            "release_status": metadata["release_status"], "rows": [], "run_metadata": metadata,
            "snapshot_kind": None, "calibration_state": "no_valid_snapshot",
            "stale_warning": "尚無有效自動或人工發布快照" if metadata["release_status"] != "fetching" else None,
            "updated_at": metadata.get("updated_at"),
        }


def _now() -> str:
    return datetime.now(TZ_UTC).isoformat()

def _timestamp(value: Optional[datetime]) -> str:
    if value is None:
        return _now()
    return value.astimezone(TZ_UTC).isoformat() if value.tzinfo else value.replace(tzinfo=TZ_UTC).isoformat()

def _parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def _auto_snapshot_diagnostic(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AutoSnapshotDiagnosticError):
        return exc.code, str(exc)
    text = str(exc).casefold()
    if "api_football_key" in text or "401" in text or "403" in text or "auth" in text:
        return "external_api_authentication_error", "外部資料來源認證或設定失敗"
    if "season" in text or "league" in text:
        return "league_season_configuration_error", "聯賽或賽季設定不正確"
    if "market" in text or "odds" in text or "line" in text:
        return "no_usable_markets", "資料取得成功，但沒有可用的標準足球盤口"
    if "request" in text or "http" in text or "timeout" in text or "url" in text:
        return "external_api_error", "外部足球資料來源暫時無法取得"
    return "snapshot_build_error", "建立足球自動快照失敗，請查看後台資料來源與設定"

def _decode_snapshot_payload(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    metadata = payload.get("run_metadata") if isinstance(payload.get("run_metadata"), Mapping) else {}
    return {"rows": rows, "run_metadata": dict(metadata)}

def _join_warning(*parts: Any) -> str:
    return "；".join(str(part) for part in parts if part)

def _number_display(value: Any) -> str:
    """Compact decimal display without changing the stored numerical values."""
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")

def _market_selection_display(market: Mapping[str, Any], home: str, away: str) -> str:
    """Football-only presentation of an already validated saved market."""
    market_type, side = market.get("market_type"), market.get("side")
    price = _number_display(market.get("decimal_price"))
    suffix = f"｜Decimal {price}" if price else ""
    if market_type == "moneyline":
        selection = {"home": f"{home} 勝", "away": f"{away} 勝", "draw": "和局"}.get(str(side), str(side))
    elif market_type == "spread":
        team = home if side == "home" else away
        line = float(market.get("line") or 0)
        selection = f"{team}{'讓' if line < 0 else '受讓' if line > 0 else '平手'} {line:+g}"
    elif market_type == "total":
        selection = f"{'大' if side == 'over' else '小'} {_number_display(market.get('line'))}"
    else:
        selection = str(side or market_type or "盤口")
    return selection + suffix

def _markets_display(markets: Iterable[Mapping[str, Any]], home: str, away: str) -> str:
    displays = [_market_selection_display(market, home, away) for market in markets]
    return "；".join(displays) if displays else "尚無人工校正盤"

def _first_and_latest_calibrations(history: list[Mapping[str, Any]],
                                   current: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Select market versions from saved audit metadata, never from a live feed."""
    if not history:
        # Historical runs created before this display metadata existed retain an
        # honest baseline: the currently saved manual market is all that exists.
        return list(current), list(current)
    first: dict[tuple[str, str], Mapping[str, Any]] = {}
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in history:
        key = (str(item["market_type"]), str(item["side"]))
        first.setdefault(key, item)
        latest[key] = item
    return list(first.values()), list(latest.values())

def _market_change_display(first: Iterable[Mapping[str, Any]], latest: Iterable[Mapping[str, Any]],
                           home: str, away: str) -> str:
    first_by_key = {(m["market_type"], m["side"]): m for m in first}
    latest_by_key = {(m["market_type"], m["side"]): m for m in latest}
    if not first_by_key and not latest_by_key:
        return "尚無人工校正盤可比較"
    changes: list[str] = []
    for key in sorted(set(first_by_key) | set(latest_by_key)):
        before, after = first_by_key.get(key), latest_by_key.get(key)
        if before is None or after is None:
            changes.append(_market_selection_display(after or before, home, away))
            continue
        if (before.get("line"), before.get("decimal_price")) != (after.get("line"), after.get("decimal_price")):
            changes.append(f"{_market_selection_display(before, home, away)} → {_market_selection_display(after, home, away)}")
    return "；".join(changes) if changes else "首次與最新人工校正盤無差異"

def _risk_display(risk: Any) -> str:
    if not isinstance(risk, Mapping):
        return str(risk or "資料風險未提供")
    light = {"green": "綠燈", "yellow": "黃燈", "red": "紅燈"}.get(str(risk.get("light")), "風險")
    title = str(risk.get("title") or "資料風險未提供")
    reasons = "；".join(str(item) for item in risk.get("reasons", []) if item)
    return f"{light}｜{title}" + (f"：{reasons}" if reasons else "")

def _risk_warning(risk: Any) -> str:
    if not isinstance(risk, Mapping):
        return ""
    return str(risk.get("disclosure") or "")

def _decode_source_summary(value: Any) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        decoded = {}
    return decoded if isinstance(decoded, Mapping) else {}

def _sources_display(summary: Mapping[str, Any]) -> str:
    labels = (
        ("api_football_events", "API-Football", "場"),
        ("espn_events", "ESPN", "場"),
        ("clubelo_teams", "ClubElo", "隊"),
        ("odds_quotes", "The Odds API", "筆盤口"),
        ("api_football_prematch_quotes", "API-Football 賽前盤", "筆"),
    )
    parts = [f"{label} {_number_display(summary[key])}{unit}" for key, label, unit in labels if key in summary]
    parts.extend(f"{name}：{message}" for name, message in summary.get("diagnostics", {}).items())
    return "｜".join(parts) if parts else "尚未儲存資料來源摘要"


def _automatic_market_status(summary: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> str:
    """Create a short, safe admin-only explanation of automatic market use."""

    rows = list(rows)
    market_events = int(summary.get("market_reference_events") or 0)
    odds_quotes = int(summary.get("odds_quotes") or 0)
    fallback_events = int(summary.get("api_football_prematch_events") or 0)
    evaluated = sum(bool(row.get("market_evaluations")) for row in rows)
    playable = sum(
        1 for row in rows for item in row.get("market_evaluations", ())
        if bool(item.get("playable"))
    )
    return (
        f"足球盤口診斷：The Odds API {odds_quotes} 筆／API-Football 賽前盤補強 {fallback_events} 場／"
        f"可用盤口 {market_events}/{len(rows)} 場／已運算 {evaluated} 場／達 +EV {playable} 筆"
    )

def _team_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().casefold())
    return _TEAM_ALIASES.get(key, key)

def _local_date(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_UTC)
    return parsed.astimezone(TZ_TAIPEI).date().isoformat()

def _league_goal_prior(league: str) -> tuple[float, float]:
    return {"eng.1": (1.55,1.25), "esp.1": (1.40,1.10), "ger.1": (1.65,1.35),
            "ita.1": (1.45,1.15), "fra.1": (1.42,1.12), "uefa.champions": (1.58,1.28)}.get(league, (1.50,1.20))

def _team_rates(stats: Mapping[str, Any], venue: str) -> tuple[float, float]:
    # API-Football returns nested goal totals/averages. Use xG if provider exposes
    # it, otherwise goals for/conceded per venue; all fallbacks are bounded.
    goals = stats.get("goals", {}) if isinstance(stats, Mapping) else {}
    for_key = goals.get("for", {}).get("average", {}).get(venue) or goals.get("for", {}).get("average", {}).get("total")
    against_key = goals.get("against", {}).get("average", {}).get(venue) or goals.get("against", {}).get("average", {}).get("total")
    try: attack = float(for_key)
    except (TypeError, ValueError): attack = 1.25
    try: defence = float(against_key)
    except (TypeError, ValueError): defence = 1.25
    return max(.35, min(2.8, attack)), max(.35, min(2.8, defence))

def _injury_count(injuries: Iterable[Mapping[str, Any]], team_name: str) -> int:
    key = _team_key(team_name)
    return sum(1 for row in injuries if _team_key(row.get("team", {}).get("name")) == key)

def _assess_risk(event: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
    """Football-specific traffic light: risk changes disclosure, not eligibility."""
    reasons: list[str] = []
    lineups = event.get("lineups") or []
    has_confirmed_lineups = len(lineups) >= 2 and all(x.get("startXI") for x in lineups[:2])
    injuries = quality.get("injury_count", {})
    total_injuries = int(injuries.get("home", 0)) + int(injuries.get("away", 0))
    if not has_confirmed_lineups:
        reasons.append("先發名單尚未最終確認，模型以目前可取得的球隊資料計算")
    if total_injuries:
        reasons.append(f"已納入 {total_injuries} 名傷停／停賽球員；出賽名單仍可能變動")
    if not quality.get("home_stats") or not quality.get("away_stats"):
        reasons.append("至少一隊近期主客場統計不足，部分參數使用聯賽基準")
    if not quality.get("clubelo"):
        reasons.append("ClubElo 未完整匹配，長期強度採中性備援值")
    if not event.get("espn_verified"):
        reasons.append("ESPN 未完成同場賽程交叉驗證")
    if has_confirmed_lineups and total_injuries <= 2 and quality.get("home_stats") and quality.get("away_stats") and quality.get("clubelo"):
        light = "green"
    elif not has_confirmed_lineups and (total_injuries >= 5 or not quality.get("home_stats") or not quality.get("away_stats")):
        light = "red"
    else:
        light = "yellow"
    title = {"green":"資料完整", "yellow":"資料仍可能更新", "red":"資料風險較高"}[light]
    return {"light": light, "title": title, "lineup_status": "confirmed" if has_confirmed_lineups else "projected_or_unconfirmed",
            "reasons": reasons, "disclosure": "風險燈號不會自動封鎖 +EV；確認先發變動後將重算。"}

def _consensus_event(league: str, event: Mapping[str, Any]) -> list[dict[str, Any]]:
    home, away = event.get("home_team", ""), event.get("away_team", "")
    buckets: dict[tuple[str,str], list[tuple[Optional[float],float,str]]] = {}
    for book in event.get("bookmakers", []):
        observed = book.get("last_update") or _now()
        for market in book.get("markets", []):
            mt = {"h2h":"moneyline", "spreads":"spread", "totals":"total"}.get(market.get("key"))
            if not mt: continue
            for outcome in market.get("outcomes", []):
                name = str(outcome.get("name", ""))
                # Provider team labels can be abbreviated or accent-normalised.
                # Use the same identity key as event matching so a real price is
                # not discarded before it reaches the existing market validator.
                side = (
                    "over" if mt == "total" and name.casefold() == "over" else
                    "under" if mt == "total" and name.casefold() == "under" else
                    "home" if _team_key(name) == _team_key(home) else
                    "away" if _team_key(name) == _team_key(away) else
                    "draw" if mt == "moneyline" and name.casefold() == "draw" else None
                )
                if side and outcome.get("price") is not None:
                    buckets.setdefault((mt,side), []).append((outcome.get("point"), float(outcome["price"]), observed))
    rows = []
    for (mt,side), values in buckets.items():
        points, prices = [x[0] for x in values if x[0] is not None], [x[1] for x in values]
        rows.append({"league": league, "kickoff": event.get("commence_time"), "home": home, "away": away,
            "market_type": mt, "side": side, "line": float(np.median(points)) if points else None,
            "price": float(np.median(prices)), "provider": "The Odds API consensus",
            "observed_at": max(x[2] for x in values), "source_count": len(values)})
    return rows


def _has_usable_market_records(records: Iterable[Mapping[str, Any]]) -> bool:
    """Use the existing market validator to decide whether a fallback is needed."""

    values = list(records)
    if not values:
        return False
    try:
        normalised = [_validate_market({
            **dict(value),
            "decimal_price": value.get("decimal_price", value.get("price")),
        }) for value in values]
        _validate_market_set(normalised)
    except (TypeError, ValueError):
        return False
    return bool(normalised)


def _api_football_prematch_records(event: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt API-Football's already-configured pre-match odds to normal rows.

    No figure is inferred here.  A market is retained only when its sides form
    a valid moneyline, Asian-handicap, or total pair.  The Football domain
    module later applies its existing validator and Poisson/+EV calculation.
    """

    home, away = str(event.get("home") or ""), str(event.get("away") or "")
    if not home or not away:
        return []
    buckets: dict[tuple[str, str, Optional[float]], list[tuple[float, str]]] = {}
    for fixture in payload.get("response") or ():
        if not isinstance(fixture, Mapping):
            continue
        observed = str(fixture.get("update") or _now())
        for bookmaker in fixture.get("bookmakers") or ():
            if not isinstance(bookmaker, Mapping):
                continue
            for bet in bookmaker.get("bets") or ():
                if not isinstance(bet, Mapping):
                    continue
                market_type = _api_football_market_type(bet.get("name"))
                if not market_type:
                    continue
                for outcome in bet.get("values") or ():
                    if not isinstance(outcome, Mapping):
                        continue
                    parsed = _api_football_outcome(
                        market_type, outcome.get("value"), home, away,
                    )
                    if parsed is None:
                        continue
                    side, line = parsed
                    try:
                        price = float(outcome.get("odd", outcome.get("price")))
                    except (TypeError, ValueError):
                        continue
                    if price > 1:
                        buckets.setdefault((market_type, side, line), []).append((price, observed))

    def record(market_type: str, side: str, line: Optional[float]) -> Optional[dict[str, Any]]:
        values = buckets.get((market_type, side, line), [])
        if not values:
            return None
        prices, observed_times = zip(*values)
        return {
            "league": event.get("league_key"), "kickoff": event.get("kickoff"),
            "home": home, "away": away, "market_type": market_type, "side": side,
            "line": line, "price": float(np.median(prices)),
            "provider": "API-Football pre-match odds", "observed_at": max(observed_times),
            "source_count": len(values),
        }

    rows: list[dict[str, Any]] = []
    # A standard 1X2 market can be calculated even when draw is not offered.
    for side in ("home", "draw", "away"):
        item = record("moneyline", side, None)
        if item:
            rows.append(item)

    # Preserve a coherent Asian pair rather than combining unrelated lines from
    # different bookmakers.  Pick the most frequently observed valid pair.
    spread_pairs: list[tuple[int, float]] = []
    for (market_type, side, line), values in buckets.items():
        if market_type == "spread" and side == "home" and line is not None:
            opposite = buckets.get(("spread", "away", -line), [])
            if opposite:
                spread_pairs.append((len(values) + len(opposite), line))
    if spread_pairs:
        _, line = max(spread_pairs, key=lambda item: (item[0], -abs(item[1])))
        rows.extend(item for item in (record("spread", "home", line), record("spread", "away", -line)) if item)

    total_pairs: list[tuple[int, float]] = []
    for (market_type, side, line), values in buckets.items():
        if market_type == "total" and side == "over" and line is not None:
            opposite = buckets.get(("total", "under", line), [])
            if opposite:
                total_pairs.append((len(values) + len(opposite), line))
    if total_pairs:
        _, line = max(total_pairs, key=lambda item: (item[0], -item[1]))
        rows.extend(item for item in (record("total", "over", line), record("total", "under", line)) if item)
    return rows if _has_usable_market_records(rows) else []


def _api_football_market_type(value: Any) -> Optional[str]:
    name = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
    if any(token in name for token in ("asian handicap", "handicap")):
        return "spread"
    if any(token in name for token in ("over/under", "over under", "total goals", "goals over")):
        return "total"
    if name in {"match winner", "winner", "1x2", "match result", "result"}:
        return "moneyline"
    return None


def _api_football_outcome(
    market_type: str, value: Any, home: str, away: str,
) -> Optional[tuple[str, Optional[float]]]:
    text = str(value or "").strip()
    folded = text.casefold()
    if market_type == "moneyline":
        if folded in {"1", "home", "home win"} or folded.startswith("home "):
            return "home", None
        if folded in {"x", "draw", "tie"}:
            return "draw", None
        if folded in {"2", "away", "away win"} or folded.startswith("away "):
            return "away", None
        team_key = _team_key(re.sub(r"[^A-Za-zÀ-ÿ .'-]", "", text))
        if team_key == _team_key(home):
            return "home", None
        if team_key == _team_key(away):
            return "away", None
        return None
    if market_type == "total":
        side = "over" if folded.startswith("over") else "under" if folded.startswith("under") else ""
        line = _api_football_line(text)
        return (side, line) if side and line is not None else None
    if market_type == "spread":
        side = "home" if folded.startswith("home") else "away" if folded.startswith("away") else ""
        if not side:
            stripped = re.sub(r"[+-]?\d+(?:\.\d+)?", "", text)
            key = _team_key(stripped)
            side = "home" if key == _team_key(home) else "away" if key == _team_key(away) else ""
        line = _api_football_line(text)
        return (side, line) if side and line is not None else None
    return None


def _api_football_line(value: str) -> Optional[float]:
    match = re.search(r"(?<![A-Za-z])([+-]?\d+(?:\.\d+)?)", str(value))
    if not match:
        return None
    try:
        line = float(match.group(1))
    except ValueError:
        return None
    return line if math.isclose(line * 4, round(line * 4)) else None

def _match_odds(records: Iterable[Mapping[str, Any]], event: Mapping[str, Any]) -> list[dict[str, Any]]:
    # Kept outside the class for testability; match on league, two teams, kickoff within 30 min.
    target = datetime.fromisoformat(str(event["kickoff"]).replace("Z", "+00:00"))
    target_teams = (_team_key(event["home"]), _team_key(event["away"]))
    out=[]
    for row in records:
        try: delta=abs((target-datetime.fromisoformat(str(row["kickoff"]).replace("Z", "+00:00"))).total_seconds())
        except (ValueError, TypeError): continue
        if row.get("league") == event.get("league_key") and target_teams == (_team_key(row.get("home")),_team_key(row.get("away"))) and delta <= 1800:
            out.append(dict(row))
    return out

def _asian_parts(line: float) -> list[float]:
    if not np.isclose(line * 4, round(line * 4)):
        raise ValueError("football Asian lines must be multiples of 0.25")
    return [math.floor(line * 2) / 2, math.ceil(line * 2) / 2] if round(line * 4) % 2 else [line]

def _asian_ev(values: np.ndarray, handicap: float, decimal_price: float) -> tuple[float, float]:
    profits, wins = [], []
    for leg in _asian_parts(handicap):
        settled = np.sign(np.asarray(values, dtype=float) + leg)
        profits.append(np.where(settled > 0, decimal_price - 1, np.where(settled < 0, -1.0, 0.0)))
        wins.append(settled > 0)
    return float(np.mean(profits)), float(np.mean(wins))

def _validate_market(value: Mapping[str, Any]) -> dict[str, Any]:
    m = dict(value)
    if m.get("market_type") not in {"moneyline", "spread", "total"}:
        raise ValueError("market_type must be moneyline, spread or total")
    allowed = {"moneyline":{"home","away","draw"}, "spread":{"home","away"}, "total":{"over","under"}}
    if m.get("side") not in allowed[m["market_type"]]: raise ValueError("invalid market side")
    m["decimal_price"] = float(m["decimal_price"])
    if m["decimal_price"] <= 1: raise ValueError("decimal_price must be greater than 1")
    if m["market_type"] != "moneyline":
        m["line"] = float(m["line"])
        _asian_parts(m["line"])
    else: m["line"] = None
    return m

def _validate_market_set(markets: list[Mapping[str, Any]]) -> None:
    by = {(m["market_type"],m["side"]):m for m in markets}
    for mt, sides in (("spread", ("home","away")), ("total", ("over","under"))):
        if any((mt,s) in by for s in sides) and not all((mt,s) in by for s in sides):
            raise ValueError(f"{mt} requires both sides")
    if ("spread","home") in by and not np.isclose(by[("spread","home")]["line"] + by[("spread","away")]["line"], 0):
        raise ValueError("home and away handicap lines must be opposites")
    if ("total","over") in by and not np.isclose(by[("total","over")]["line"], by[("total","under")]["line"]):
        raise ValueError("over and under lines must match")


_TEAM_ALIASES = {
    "manchesterunited": "manunited", "manchestercity": "mancity",
    "tottenhamhotspur": "tottenham", "newcastleunited": "newcastle",
    "westhamunited": "westham", "wolverhamptonwanderers": "wolves",
    "brightonhovealbion": "brighton", "nottinghamforest": "forest",
    "bayernmunich": "bayern", "bayernmunchen": "bayern",
    "borussiadortmund": "dortmund", "bayerleverkusen": "leverkusen",
    "borussiamonchengladbach": "gladbach", "rbleipzig": "leipzig",
    "internazionale": "inter", "intermilan": "inter", "acmilan": "milan",
    "parissaintgermain": "psg", "parissg": "psg", "psg": "psg",
    "atleticomadrid": "atletico", "atlmadrid": "atletico",
    "athleticclub": "bilbao", "athleticbilbao": "bilbao",
    "athbilbao": "bilbao", "celtavigo": "celta", "realbetis": "betis",
    "realsociedad": "sociedad",
    "bdortmund": "dortmund", "monchengladbach": "gladbach",
    "mgladbach": "gladbach", "psveindhoven": "psv",
    "rcdespanyol": "espanyol", "dalaves": "alaves",
    "deportivo": "alaves", "deportivoalaves": "alaves", "alaves": "alaves",
    "villarrealcf": "villarreal", "realvalladolid": "valladolid",
    "parisfc": "parisfc", "olympiquelyonnais": "lyon",
    "olympiquedemarseille": "marseille", "asroma": "roma",
}

def _provider_failure(exc):
    try:
        body = exc.response.json()
        if body.get("error_code") == "OUT_OF_USAGE_CREDITS":
            return "The Odds API 可用額度已用完"
        if body.get("error_code") == "INVALID_KEY":
            return "The Odds API 金鑰無效"
    except (AttributeError, TypeError, ValueError):
        pass
    code = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "code", None)
    if code in (401, 403): return "認證或存取權限失敗，請檢查金鑰與方案"
    if code == 429: return "請求額度或速率限制，請檢查帳戶額度"
    if code == 422: return "API 不接受查詢參數或市場，請檢查方案與市場設定"
    if code: return f"來源 HTTP {int(code)} 錯誤"
    if isinstance(exc, (ValueError, KeyError)): return "來源資料格式不符"
    return "來源連線、逾時或 TLS 失敗"

def _saved_forecast(model):
    """Backend-only display projection of the existing independent Poisson model.

    Does not feed recommendations, odds validation or the Base Model.
    Members read the resulting numbers from snapshots, never call this function.
    """
    h, a = float(model["lambda_home"]), float(model["lambda_away"])
    n = max(30, int(max(h, a) + 12 * math.sqrt(max(h, a)) + 10))
    hp, ap = [math.exp(-h)], [math.exp(-a)]
    for i in range(1, n):
        hp.append(hp[-1] * h / i); ap.append(ap[-1] * a / i)
    home = sum(hp[i] * sum(ap[:i]) for i in range(n))
    draw = sum(hp[i]*ap[i] for i in range(n))
    away = sum(ap[i] * sum(hp[:i]) for i in range(n))
    total = home + draw + away
    return {"home_probability": home/total, "draw_probability": draw/total,
            "away_probability": away/total, "home_xg": h, "away_xg": a,
            "score": f"{max(range(n), key=hp.__getitem__)}–{max(range(n), key=ap.__getitem__)}"}
