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
import unicodedata
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

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
TZ_TAIPEI = ZoneInfo("Asia/Taipei")
FOOTBALL_MODEL_VERSION = "football-v2.1-quant-shadow"
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
    v21_promoted: bool = False


class AutoSnapshotDiagnosticError(RuntimeError):
    """Expected backend snapshot outcome with a safe, displayable diagnosis."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FootballModule:
    """Persistence and calculation boundary for the Football module only."""

    def __init__(self, config: FootballConfig) -> None:
        self.config = config
        # Safe, operator-facing source status only.  Never store provider
        # response bodies, URLs or exceptions here because they may contain
        # credentials.
        self._source_diagnostics: dict[str, str] = {}
        # Per-run, safe counters.  They make a missing team-statistics feed
        # visible instead of silently turning every fixture into league priors.
        self._team_stat_health = {"requested": 0, "available": 0, "failed": 0}
        self._init_db()

    @classmethod
    def from_environment(cls, db_path: str) -> "FootballModule":
        return cls(FootballConfig(
            db_path=db_path,
            api_football_key=os.environ.get("API_FOOTBALL_KEY", ""),
            odds_api_key=os.environ.get("THE_ODDS_API_KEY", ""),
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
            CREATE TABLE IF NOT EXISTS football_model_versions (
              model_version TEXT PRIMARY KEY, training_cutoff TEXT, sources_json TEXT NOT NULL,
              acceptance_json TEXT NOT NULL, promoted INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS football_quant_shadow_predictions (
              date_str TEXT NOT NULL, event_id TEXT NOT NULL, model_version TEXT NOT NULL,
              forecast_json TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(date_str,event_id,model_version)
            );
            CREATE TABLE IF NOT EXISTS football_quant_backtests (
              model_version TEXT NOT NULL, training_cutoff TEXT NOT NULL, metrics_json TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(model_version,training_cutoff)
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
        target = date.fromisoformat(date_str)
        self._team_stat_health = {"requested": 0, "available": 0, "failed": 0}
        espn_index = self._fetch_espn_fixtures(target)
        api_events: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        if not self.config.api_football_key:
            diagnostics.append({"code": "api_football_configuration_error", "message": "API-Football 金鑰未設定，改用 ESPN 賽程備援"})
        elif not _has_configured_season(season_by_league):
            diagnostics.append({"code": "league_season_configuration_error", "message": "聯賽或賽季設定不正確，改用 ESPN 賽程備援"})
        else:
            try:
                api_events = self._fetch_api_football_fixtures(target, season_by_league)
            except AutoSnapshotDiagnosticError as exc:
                diagnostics.append({"code": exc.code, "message": f"{exc}: 改用 ESPN 賽程備援"})
            except requests.RequestException:
                diagnostics.append({"code": "external_api_error", "message": "API-Football 賽程暫時無法取得，改用 ESPN 賽程備援"})
        elo = self._fetch_clubelo(target)
        # A market-feed outage must never discard an otherwise valid schedule
        # or model snapshot.  The source method itself isolates per-league
        # failures; this second guard protects the daily refresh boundary.
        try:
            odds = self._fetch_odds_consensus(target)
        except Exception:
            odds = []
            self._last_odds_diagnostics = [{"code": "odds_api_unavailable", "message": "即時盤口暫時無法取得；將保留賽程並顯示 PASS"}]
        diagnostics.extend(getattr(self, "_last_odds_diagnostics", []))
        events = api_events
        if not events:
            events = self._espn_fallback_events(espn_index)
            if events:
                diagnostics.append({"code": "espn_schedule_fallback", "message": "已使用 ESPN 賽程備援"})
        status = "awaiting_manual_calibration" if events else ("failed" if diagnostics else "no_events")
        stored = 0
        with self._db() as conn:
            conn.execute("DELETE FROM football_events WHERE date_str=?", (date_str,))
            conn.execute("DELETE FROM football_market_reference WHERE date_str=?", (date_str,))
            for event in events:
                if event.get("schedule_source") != "ESPN":
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
                for q in _match_odds(odds, event):
                    conn.execute("""INSERT INTO football_market_reference VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                        date_str, event["event_id"], q["market_type"], q["side"], q["line"],
                        q["price"], q["provider"], q["observed_at"], q["source_count"],
                        json.dumps(q, ensure_ascii=False),
                    ))
                stored += 1
            summary = {"api_football_events": len(api_events), "espn_events": len(espn_index),
                       "stored_events": stored, "schedule_source": "API-Football" if api_events else ("ESPN" if events else None),
                       "diagnostics": diagnostics, "clubelo_teams": len(elo), "odds_quotes": len(odds),
                       "team_statistics": dict(self._team_stat_health),
                       "timezone": "Asia/Taipei"}
            conn.execute("""INSERT INTO football_daily_runs VALUES(?,?,?,?)
              ON CONFLICT(date_str) DO UPDATE SET fetched_at=excluded.fetched_at,
              status=excluded.status,source_summary=excluded.source_summary""",
              (date_str, _now(), status, json.dumps(summary, ensure_ascii=False)))
        return {"date": date_str, "events": stored, "status": "valid" if stored else status,
                "sources": summary, "diagnostics": diagnostics}

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
                    "updated_at": attempted_at, "events": len(rows), "member_visible": True}
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
        shadow_records: list[tuple[str, str]] = []
        for event in events:
            markets = list(reference_by_event.get(event["event_id"], {}).values())
            model = json.loads(event["model_json"])
            normalised = [_validate_market(market) for market in markets]
            if normalised:
                _validate_market_set(normalised)
                v21_recommendations = self._calculate_recommendations(model, normalised)
                calculated = v21_recommendations if self.config.v21_promoted else self._calculate_legacy_recommendations(model, normalised)
                # Never turn league-average fallbacks into a member-facing
                # +EV tip.  Keep the saved forecast/risk so the operator can
                # see exactly why this event needs a source retry.
                recommendations = calculated if _has_reliable_team_inputs(model) else []
            else:
                # No price means no implied probability, fusion, or +EV.  An
                # empty recommendation list is the existing Core-compatible
                # PASS representation; no synthetic market is introduced.
                v21_recommendations, recommendations = [], []
            forecast = _quant_forecast(model, matrix=_score_matrix(float(model["lambda_home"]), float(model["lambda_away"]), rho=-0.08), recommendations=v21_recommendations)
            risk = model.get("risk", {})
            row = {
                "event_id": event["event_id"], "sport": "football", "league_key": event["league_key"], "kickoff": event["kickoff"],
                "home": event["home_team"], "away": event["away_team"], "model": model,
                "risk": _risk_display(risk), "risk_display": _risk_display(risk),
                "warning": _join_warning("自動更新／尚未人工校正", _risk_warning(risk),
                    None if _has_reliable_team_inputs(model) else "球隊統計未完整取得：僅顯示聯賽基準預估，不提供自動推薦",
                    None if normalised else "即時盤口暫時無法取得：PASS（無有效可驗證價格）"),
                "settlement_status": "pending",
                "first_market": _markets_display(normalised, event["home_team"], event["away_team"]) if normalised else "尚無可驗證即時盤口",
                "latest_market": _markets_display(normalised, event["home_team"], event["away_team"]) if normalised else "尚無可驗證即時盤口",
                "market_change": "自動快照盤口（尚無人工校正歷程）" if normalised else "無可用盤口，推薦為 PASS",
                # ``forecast`` is the long-standing Core contract.  The
                # richer V2.1 audit payload below must never replace it.
                "forecast": _saved_forecast(model, forecast),
                "quant_forecast": forecast,
                "recommendations": [],
            }
            for recommendation in recommendations:
                if not recommendation["playable"]:
                    continue
                display = _market_selection_display(recommendation, event["home_team"], event["away_team"])
                row["recommendations"].append({**recommendation, "display": display, "selection": display})
            rows.append(row)
            shadow_records.append((event["event_id"], json.dumps(forecast, ensure_ascii=False)))
        if not rows:
            raise AutoSnapshotDiagnosticError("no_events", "當日查無可用足球賽事")
        metadata = self.get_run_metadata(date_str)
        metadata.update({
            "release_status": "automatic_available",
            "calibration_source": "自動更新／尚未人工校正",
            "updated_at": updated_at,
            "market_observed_at": market_observed_at,
            "model_version": FOOTBALL_MODEL_VERSION,
            "model_mode": "shadow" if not self.config.v21_promoted else "promoted",
        })
        with self._db() as conn:
            conn.execute("""INSERT INTO football_model_versions VALUES(?,?,?,?,?,?)
              ON CONFLICT(model_version) DO UPDATE SET sources_json=excluded.sources_json,
                promoted=excluded.promoted,created_at=excluded.created_at""",
                (FOOTBALL_MODEL_VERSION, None, json.dumps(["API-Football", "ESPN", "ClubElo", "The Odds API"]),
                 json.dumps({"status": "shadow_pending_backtest"}), int(self.config.v21_promoted), updated_at))
            for event_id, forecast_json in shadow_records:
                conn.execute("""INSERT INTO football_quant_shadow_predictions VALUES(?,?,?,?,?)
                  ON CONFLICT(date_str,event_id,model_version) DO UPDATE SET forecast_json=excluded.forecast_json,created_at=excluded.created_at""",
                    (date_str, event_id, FOOTBALL_MODEL_VERSION, forecast_json, updated_at))
        return rows, metadata, market_observed_at

    def run_quant_backtest(self, records: Iterable[Mapping[str, Any]], training_cutoff: str) -> dict[str, Any]:
        """Time-ordered V2.1 evaluation; caller supplies only pre-match records.

        Each record must include ``pre_match_at``, the saved pre-match ``model``,
        and final ``home_goals``/``away_goals``.  Rows at or after the cutoff are
        evaluated only; future outcomes are never used to alter their forecast.
        """
        cutoff = _parse_timestamp(training_cutoff)
        if cutoff is None: raise ValueError("training_cutoff must be ISO-8601")
        evaluated = []
        for record in sorted(records, key=lambda item: str(item.get("pre_match_at", ""))):
            when = _parse_timestamp(record.get("pre_match_at"))
            if when is None or when < cutoff: continue
            model = record.get("model") or {}
            if "lambda_home" not in model or "lambda_away" not in model: continue
            matrix = _score_matrix(float(model["lambda_home"]), float(model["lambda_away"]), rho=-0.08)
            home_goals, away_goals = int(record["home_goals"]), int(record["away_goals"])
            mode = _matrix_mode(matrix)
            probabilities = [float(matrix[np.indices(matrix.shape)[0] > np.indices(matrix.shape)[1]].sum()),
                             float(np.trace(matrix)), float(matrix[np.indices(matrix.shape)[0] < np.indices(matrix.shape)[1]].sum())]
            actual = 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2
            evaluated.append({"probabilities": probabilities, "actual": actual, "home": home_goals, "away": away_goals,
                              "lambda_home": float(model["lambda_home"]), "lambda_away": float(model["lambda_away"]), "mode": mode})
        if not evaluated: raise ValueError("no post-cutoff pre-match records available for backtest")
        log_loss = float(np.mean([-math.log(max(.001, row["probabilities"][row["actual"]])) for row in evaluated]))
        brier = float(np.mean([sum((prob - float(index == row["actual"])) ** 2 for index, prob in enumerate(row["probabilities"])) for row in evaluated]))
        metrics = {"period_start": training_cutoff, "samples": len(evaluated), "one_x_two": {"log_loss": log_loss, "brier_score": brier},
                   "expected_goals": {"mae": float(np.mean([abs(row["lambda_home"]-row["home"]) + abs(row["lambda_away"]-row["away"]) for row in evaluated]) / 2),
                                      "rmse": float(math.sqrt(np.mean([(row["lambda_home"]-row["home"])**2 + (row["lambda_away"]-row["away"])**2 for row in evaluated]) / 2))},
                   "correct_score": {"top_1": float(np.mean([row["mode"] == (row["home"], row["away"]) for row in evaluated]))},
                   "calibration_summary": "時間序列 holdout；完整曲線需在樣本足夠後產生", "status": "shadow_evaluation"}
        with self._db() as conn:
            conn.execute("INSERT INTO football_quant_backtests VALUES(?,?,?,?) ON CONFLICT(model_version,training_cutoff) DO UPDATE SET metrics_json=excluded.metrics_json,created_at=excluded.created_at",
                         (FOOTBALL_MODEL_VERSION, training_cutoff, json.dumps(metrics, ensure_ascii=False), _now()))
        return metrics

    def _fetch_api_football_fixtures(self, target: date, seasons: Mapping[str, int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        headers = {"x-apisports-key": self.config.api_football_key}
        attempted, failures = 0, 0
        for league_key, league_id in DEFAULT_LEAGUES.items():
            season = seasons.get(league_key)
            if not season:
                continue
            attempted += 1
            try:
                response = requests.get(f"{self.config.api_football_base}/fixtures", headers=headers,
                    params={"league": league_id, "season": season, "date": target.isoformat(), "timezone": "Asia/Taipei"}, timeout=12)
                response.raise_for_status()
            except requests.RequestException:
                failures += 1
                continue  # A single league outage must not block other leagues or MLB.
            for item in response.json().get("response", []):
                fixture = item.get("fixture", {})
                teams = item.get("teams", {})
                if not fixture.get("id") or not teams.get("home", {}).get("name"):
                    continue
                if _taipei_date(fixture.get("date")) != target.isoformat():
                    continue
                home_id, away_id = teams["home"].get("id"), teams["away"].get("id")
                team_stats = {
                    "home": self._fetch_team_statistics(headers, league_id, season, home_id, target),
                    "away": self._fetch_team_statistics(headers, league_id, season, away_id, target),
                }
                # Availability endpoints are optional enrichment.  A timeout
                # here must not discard an otherwise valid fixture/statistics
                # record or force the whole daily run into ESPN fallback.
                try:
                    injuries = self._fetch_fixture_injuries(headers, fixture["id"])
                except Exception:
                    injuries = []
                    self._source_diagnostics["API-Football 傷停"] = "傷停資料暫時無法取得；已提高風險提示"
                try:
                    lineups = self._fetch_fixture_lineups(headers, fixture["id"])
                except Exception:
                    lineups = []
                    self._source_diagnostics["API-Football 先發"] = "先發資料暫時無法取得；已提高風險提示"
                out.append({"event_id": str(fixture["id"]), "league_key": league_key,
                    "kickoff": fixture.get("date"), "home": teams["home"]["name"],
                    "away": teams["away"]["name"], "home_id": home_id, "away_id": away_id,
                    "status": fixture.get("status", {}).get("short", "NS"),
                    "team_statistics": team_stats, "injuries": injuries, "lineups": lineups,
                    "api_source": "API-Football"})
        if attempted and attempted == failures:
            raise AutoSnapshotDiagnosticError("external_api_error", "外部足球賽程資料暫時無法取得")
        return out

    def _fetch_team_statistics(self, headers: Mapping[str, str], league: int, season: int,
                               team: Optional[int], target: date) -> dict[str, Any]:
        if not team:
            return {}
        self._team_stat_health["requested"] += 1
        try:
            r = requests.get(f"{self.config.api_football_base}/teams/statistics", headers=headers,
                params={"league": league, "season": season, "team": team, "date": target.isoformat()}, timeout=12)
        except Exception:
            self._team_stat_health["failed"] += 1
            self._source_diagnostics["API-Football 球隊統計"] = "球隊統計暫時無法取得；不會以聯賽預設值發出自動推薦"
            return {}
        if not r.ok:
            self._team_stat_health["failed"] += 1
            self._source_diagnostics["API-Football 球隊統計"] = "球隊統計來源拒絕或限制查詢；請檢查方案、額度與賽季設定"
            return {}
        response = r.json().get("response") or {}
        if response:
            self._team_stat_health["available"] += 1
        else:
            self._team_stat_health["failed"] += 1
            self._source_diagnostics["API-Football 球隊統計"] = "球隊統計未回傳有效資料；不會以聯賽預設值發出自動推薦"
        return response

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
            # ESPN's scoreboard date is not a Taiwan-day contract.  Query the
            # adjacent UTC day as well, then keep only events whose kickoff is
            # on the administrator's Taiwan date.
            for lookup_day in (target - timedelta(days=1), target):
                try:
                    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_key}/scoreboard"
                    r = requests.get(url, params={"dates": lookup_day.strftime("%Y%m%d"), "limit": 100}, timeout=8)
                    for event in r.json().get("events", []) if r.ok else []:
                        if _taipei_date(event.get("date")) != target.isoformat():
                            continue
                        comp = (event.get("competitions") or [{}])[0]
                        names = {c.get("homeAway"): c.get("team", {}).get("name", "") for c in comp.get("competitors", [])}
                        if names.get("home") and names.get("away"):
                            # Store the ESPN league key with the event so it can
                            # become a complete fallback event when API-Football
                            # is unavailable.  No raw provider payload is exposed.
                            saved_event = dict(event)
                            saved_event["_football_league_key"] = league_key
                            out[(_team_key(names["home"]), _team_key(names["away"]))] = saved_event
                except requests.RequestException:
                    continue
        return out

    def _espn_fallback_events(self, espn_index: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Convert saved ESPN schedule entries into safe Football event rows.

        ESPN is used only for fixture identity/venue/time in this path.  It
        deliberately supplies no invented team statistics, xG, injuries or
        line-ups; the base model will record its normal fallback-quality flags.
        """
        events: list[dict[str, Any]] = []
        for raw in espn_index.values():
            competition = (raw.get("competitions") or [{}])[0]
            competitors = competition.get("competitors") or []
            teams = {item.get("homeAway"): item.get("team", {}) for item in competitors}
            home, away = teams.get("home", {}).get("name"), teams.get("away", {}).get("name")
            if not home or not away:
                continue
            espn_id = str(raw.get("id") or competition.get("id") or "")
            if not espn_id:
                continue
            events.append({
                "event_id": f"espn:{espn_id}", "league_key": raw.get("_football_league_key", "eng.1"),
                "kickoff": raw.get("date") or competition.get("date"), "home": home, "away": away,
                "home_id": None, "away_id": None, "status": raw.get("status", {}).get("type", {}).get("shortDetail", "NS"),
                "team_statistics": {"home": {}, "away": {}}, "injuries": [], "lineups": [],
                "api_source": "ESPN", "schedule_source": "ESPN", "espn_verified": True,
                "espn_event_id": espn_id,
            })
        return events

    def _fetch_clubelo(self, target: date) -> dict[str, float]:
        try:
            # ClubElo's published CSV API is served from HTTP, not the HTTPS
            # endpoint used by the public website.  It is auxiliary only: a
            # failed rating read must never block fixture, market or snapshot
            # creation.
            r = requests.get(f"http://api.clubelo.com/{target.isoformat()}", timeout=10)
            r.raise_for_status()
            rows = r.text.splitlines()
            headings = rows[0].split(",")
            club_i, elo_i = headings.index("Club"), headings.index("Elo")
            return {_team_key(line.split(",")[club_i]): float(line.split(",")[elo_i]) for line in rows[1:] if "," in line}
        except (requests.RequestException, ValueError, IndexError):
            return {}

    def _fetch_odds_consensus(self, target: date) -> list[dict[str, Any]]:
        """Fetch standard pre-match markets without allowing a feed failure to escape.

        A provider exception is retained as a short diagnostic only; no key,
        URL, response body, or provider exception text is persisted/displayed.
        """
        self._last_odds_diagnostics: list[dict[str, str]] = []
        if not self.config.odds_api_key:
            self._last_odds_diagnostics.append({"code": "odds_api_configuration_error", "message": "The Odds API 未設定；將保留賽程並顯示 PASS"})
            self._source_diagnostics["The Odds API"] = "未設定金鑰；無有效盤口的賽事將顯示 PASS"
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
                for event in r.json():
                    if _local_date(event.get("commence_time")) != target.isoformat():
                        continue
                    out.extend(_consensus_event(league, event))
            except Exception as exc:
                self._last_odds_diagnostics.append({"code": "odds_api_unavailable", "message": "部分即時盤口暫時無法取得；無有效價格的賽事將顯示 PASS"})
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in {401, 403}:
                    self._source_diagnostics["The Odds API"] = "認證或存取權限失敗，請檢查 The Odds API 金鑰與方案"
                else:
                    self._source_diagnostics["The Odds API"] = "部分即時盤口暫時無法取得；無有效價格的賽事將顯示 PASS"
                continue
        return out

    # -------------------------- Base Model: football only ---------------------
    def _build_base_model(self, event: Mapping[str, Any], elo: Mapping[str, float]) -> dict[str, Any]:
        home_stats = event.get("team_statistics", {}).get("home", {})
        away_stats = event.get("team_statistics", {}).get("away", {})
        home_attack, home_defence, home_quality = _team_rates_with_quality(home_stats, "home")
        away_attack, away_defence, away_quality = _team_rates_with_quality(away_stats, "away")
        league_home, league_away = _league_goal_prior(event["league_key"])
        home_elo = elo.get(_team_key(event["home"]), 1650.0)
        away_elo = elo.get(_team_key(event["away"]), 1650.0)
        elo_adjust = max(.80, min(1.20, 1.0 + (home_elo - away_elo) / 2200.0))
        home_injuries = _injury_count(event.get("injuries", []), event.get("home"))
        away_injuries = _injury_count(event.get("injuries", []), event.get("away"))
        # Current feeds do not provide verified shot-event xG.  These are model
        # expected goals, using venue rates with explicit shrinkage to league priors.
        home_attack = _shrink_rate(home_attack, league_home, home_quality["shrinkage"])
        away_attack = _shrink_rate(away_attack, league_away, away_quality["shrinkage"])
        home_defence = _shrink_rate(home_defence, league_away, home_quality["shrinkage"])
        away_defence = _shrink_rate(away_defence, league_home, away_quality["shrinkage"])
        lh = max(.25, league_home * math.sqrt((home_attack / league_home) * (away_defence / league_home)) * elo_adjust * (1 - .035 * home_injuries))
        la = max(.20, league_away * math.sqrt((away_attack / league_away) * (home_defence / league_away)) / elo_adjust * (1 - .035 * away_injuries))
        clubelo_matched = _team_key(event["home"]) in elo and _team_key(event["away"]) in elo
        lineups_confirmed = _has_confirmed_lineups(event)
        fallback_reasons = list(home_quality["fallback_reason"]) + list(away_quality["fallback_reason"])
        if not clubelo_matched:
            fallback_reasons.append("ClubElo 未匹配：使用中性強度值")
        quality = {"home_stats": bool(home_stats), "away_stats": bool(away_stats), "clubelo": clubelo_matched,
                   "injury_count": {"home": home_injuries, "away": away_injuries},
                   "teams": {"home": home_quality, "away": away_quality},
                   "lineups_confirmed": lineups_confirmed, "fallback_reason": fallback_reasons,
                   "confidence": _model_confidence(home_quality, away_quality, clubelo_matched, lineups_confirmed)}
        matrix = _score_matrix(lh, la, rho=-0.08)
        exact_home, exact_away = _matrix_mode(matrix)
        return {"model_version": FOOTBALL_MODEL_VERSION,
                "lambda_home": round(lh, 4), "lambda_away": round(la, 4),
                "model_lambda_home": round(lh, 4), "model_lambda_away": round(la, 4),
                "expected_goals_conceded_home": round(la, 4), "expected_goals_conceded_away": round(lh, 4),
                "goal_semantics": "模型預估進球（非 shot-based 資料 xG）",
                "projected_total": round(lh + la, 4), "exact_score_mode": f"{exact_home}:{exact_away}",
                "score_distribution_method": "Dixon-Coles adjusted Poisson joint score matrix",
                "quality": quality, "risk": _assess_risk(event, quality),
                "notes": "V2.1：API-Football/ClubElo 賽前資料；未取得可驗證 shot-based xG 時使用進球/失球備援"}

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
        results = self._calculate_recommendations(model, normalised) if self.config.v21_promoted else self._calculate_legacy_recommendations(model, normalised)
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
        payload = json.dumps({"rows": self.get_published_rows(date_str), "run_metadata": self.get_run_metadata(date_str)}, ensure_ascii=False)
        with self._db() as conn:
            conn.execute("""INSERT INTO football_manual_snapshots VALUES(?,?,?,?,?)
              ON CONFLICT(date_str) DO UPDATE SET payload_json=excluded.payload_json,
                published_at=excluded.published_at,note=excluded.note,updated_at=excluded.updated_at""",
                (date_str, payload, now, note, now))

    def _calculate_recommendations(self, model: Mapping[str, Any], markets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        saved_markets = [dict(m) for m in markets]
        matrix = _score_matrix(float(model["lambda_home"]), float(model["lambda_away"]), rho=-0.08)
        market_probabilities = _devig_market_probabilities(saved_markets)
        confidence = float(model.get("quality", {}).get("confidence", .35))
        results = []
        for m in saved_markets:
            raw_probability, raw_ev = _matrix_market_value(matrix, m)
            key = _market_key(m)
            market_probability = market_probabilities.get(key)
            if market_probability is None:
                final_probability, model_weight, market_weight = raw_probability, 1.0, 0.0
                fusion_reason = "缺少可去水的對應市場價格；僅保存模型機率，PASS"
                final_ev = None
            else:
                model_weight = _fusion_model_weight(confidence, model.get("quality", {}))
                market_weight = 1.0 - model_weight
                final_probability = _logit_blend(raw_probability, market_probability, model_weight)
                # Preserve the existing Asian settlement payoff protection, then
                # adjust only for the fused selection probability delta.
                final_ev = raw_ev + (final_probability - raw_probability) * float(m["decimal_price"])
                fusion_reason = _fusion_reason(model_weight, model.get("quality", {}))
            playable = bool(final_ev is not None and final_ev >= self.config.min_ev and market_probability is not None)
            results.append({**m, "model_probability": raw_probability, "raw_model_probability": raw_probability,
                            "de_vig_market_probability": market_probability, "final_probability": final_probability,
                            "model_weight": model_weight, "market_weight": market_weight,
                            "fair_decimal_odds": 1 / final_probability if final_probability else None,
                            "final_ev": final_ev, "fusion_reason": fusion_reason,
                            "implied_probability": 1 / m["decimal_price"], "ev": final_ev,
                            "playable": playable, "label": "可推薦" if playable else "PASS｜未達融合 +EV 門檻或市場不完整"})
        return results

    def _calculate_legacy_recommendations(self, model: Mapping[str, Any], markets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Frozen V2.0 recommendation path used while V2.1 remains shadow-only."""
        seed = abs(hash(json.dumps(model, sort_keys=True))) % (2**32)
        rng = np.random.default_rng(seed)
        home = rng.poisson(float(model["lambda_home"]), self.config.simulations)
        away = rng.poisson(float(model["lambda_away"]), self.config.simulations)
        total, results = home + away, []
        for m in markets:
            if m["market_type"] == "moneyline":
                p = float(np.mean(home > away) if m["side"] == "home" else np.mean(away > home) if m["side"] == "away" else np.mean(home == away))
                ev = p * m["decimal_price"] - 1
            elif m["market_type"] == "spread":
                ev, p = _asian_ev(home - away if m["side"] == "home" else away - home, float(m["line"]), m["decimal_price"])
            else:
                values = total if m["side"] == "over" else -total
                ev, p = _asian_ev(values, -float(m["line"]) if m["side"] == "over" else float(m["line"]), m["decimal_price"])
            playable = bool(ev >= self.config.min_ev)
            results.append({**m, "model_probability": p, "implied_probability": 1 / m["decimal_price"], "ev": ev,
                            "playable": playable, "label": "可推薦" if playable else "PASS｜未達 +EV 門檻"})
        return results

    # ---------------------- internal display read layer -----------------------
    def get_published_rows(self, date_str: str) -> list[dict[str, Any]]:
        """Internal SQLite display reader; member routes must use get_member_snapshot."""
        with self._db() as conn:
            rows = conn.execute("""SELECT e.event_id,e.kickoff,e.home_team,e.away_team,e.model_json,
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
                    "event_id": key, "sport": "football", "kickoff": row["kickoff"],
                    "home": row["home_team"], "away": row["away_team"], "league_key": row["league_key"], "model": model,
                    # Compatibility display values for Core's existing
                    # football_rows_to_shared_report contract.  This pure
                    # helper consumes the already-saved model only.
                    "forecast": _saved_forecast(model),
                    "risk": _risk_display(risk), "risk_display": _risk_display(risk),
                    "warning": _risk_warning(risk), "settlement_status": "pending",
                    "first_market": _markets_display(first, row["home_team"], row["away_team"]),
                    "latest_market": _markets_display(latest, row["home_team"], row["away_team"]),
                    "market_change": _market_change_display(first, latest, row["home_team"], row["away_team"]),
                    "recommendations": [],
                }
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
                "release_status": "published", "rows": _rows_with_saved_forecast(payload["rows"]), "run_metadata": metadata,
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
                "release_status": "automatic_available", "rows": _rows_with_saved_forecast(payload["rows"]), "run_metadata": metadata,
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
    )
    parts = [f"{label} {_number_display(summary[key])}{unit}" for key, label, unit in labels if key in summary]
    team_statistics = summary.get("team_statistics")
    if isinstance(team_statistics, Mapping):
        available = _number_display(team_statistics.get("available", 0))
        requested = _number_display(team_statistics.get("requested", 0))
        parts.append(f"球隊統計 {available}/{requested} 隊可用")
    return "｜".join(parts) if parts else "尚未儲存資料來源摘要"

def _team_key(value: Any) -> str:
    # Source names vary in accents (Málaga/Malaga), punctuation and common
    # club suffixes.  Removing combining accents first keeps ClubElo, ESPN and
    # The Odds API matching deterministic without altering displayed names.
    text = unicodedata.normalize("NFKD", str(value).casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"\b(?:fc|cf|afc|sc)\b", "", text)
    key = re.sub(r"[^a-z0-9]", "", text)
    # Provider naming aliases only.  These normalise the same club across
    # schedule, ratings and market feeds; they do not affect any model rule.
    aliases = {
        "celtavigo": "celta", "celta": "celta",
        "parissaintgermain": "psg", "parissg": "psg", "psg": "psg",
        "manchestercity": "mancity", "mancity": "mancity",
        "manchesterunited": "manunited", "manunited": "manunited",
        "tottenhamhotspur": "tottenham", "tottenham": "tottenham",
        "newcastleunited": "newcastle", "newcastle": "newcastle",
        "atleticomadrid": "atletico", "atletico": "atletico",
        "athleticclub": "bilbao", "athleticbilbao": "bilbao", "bilbao": "bilbao",
    }
    return aliases.get(key, key)

def _taipei_date(value: Any) -> str:
    """Return the calendar date in Taiwan for every provider timestamp."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_UTC)
    return parsed.astimezone(TZ_TAIPEI).date().isoformat()

def _local_date(value: Any) -> str:
    """Backward-compatible name; Football's operating date is Taiwan time."""
    return _taipei_date(value)

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

def _team_rates_with_quality(stats: Mapping[str, Any], venue: str) -> tuple[float, float, dict[str, Any]]:
    """Return rates plus honest source/fallback metadata; no claim of event xG."""
    goals = stats.get("goals", {}) if isinstance(stats, Mapping) else {}
    averages = goals.get("for", {}).get("average", {}) if isinstance(goals, Mapping) else {}
    against = goals.get("against", {}).get("average", {}) if isinstance(goals, Mapping) else {}
    attack_value = averages.get(venue) or averages.get("total")
    defence_value = against.get(venue) or against.get("total")
    fallback: list[str] = []
    try: attack = float(attack_value)
    except (TypeError, ValueError): attack, fallback = 1.25, [f"{venue} 進球統計不足：fallback_league"]
    try: defence = float(defence_value)
    except (TypeError, ValueError): defence, fallback = 1.25, fallback + [f"{venue} 失球統計不足：fallback_league"]
    fixtures = stats.get("fixtures", {}) if isinstance(stats, Mapping) else {}
    played = fixtures.get("played", {}).get(venue) or fixtures.get("played", {}).get("total") or 0
    try: played = max(0, int(played))
    except (TypeError, ValueError): played = 0
    shrinkage = min(.80, played / 25)  # 20–30 match target, shrunk when shorter.
    if played < 20:
        fallback.append(f"{venue} 樣本 {played} 場：向聯賽均值收縮 {1 - shrinkage:.0%}")
    return max(.35, min(2.8, attack)), max(.35, min(2.8, defence)), {
        "venue": venue, "recent_venue_stats": bool(attack_value is not None and defence_value is not None),
        "shot_based_xg": False, "xg_source": "unavailable", "quality": "fallback_goals" if not fallback else "fallback_league",
        "goals_fallback": True, "matches_used": played, "shrinkage": round(shrinkage, 3), "fallback_reason": fallback,
    }

def _shrink_rate(value: float, prior: float, shrinkage: float) -> float:
    return prior + max(0.0, min(.80, shrinkage)) * (value - prior)

def _has_confirmed_lineups(event: Mapping[str, Any]) -> bool:
    lineups = event.get("lineups") or []
    return len(lineups) >= 2 and all(item.get("startXI") for item in lineups[:2])

def _model_confidence(home: Mapping[str, Any], away: Mapping[str, Any], clubelo: bool, confirmed_lineups: bool) -> float:
    score = .25 + .25 * min(home.get("shrinkage", 0), away.get("shrinkage", 0))
    score += .20 if clubelo else 0
    score += .15 if confirmed_lineups else 0
    score -= .05 * (len(home.get("fallback_reason", [])) + len(away.get("fallback_reason", [])))
    return round(max(.15, min(.80, score)), 3)

def _has_reliable_team_inputs(model: Mapping[str, Any]) -> bool:
    """Allow automatic recommendations only when both teams supplied data.

    ClubElo and lineups improve confidence but can legitimately be unavailable
    close to kickoff.  Missing both teams' season/venue statistics is different:
    it collapses the forecast to league priors, so publishing an apparent +EV
    selection would be misleading.
    """
    quality = model.get("quality") if isinstance(model, Mapping) else {}
    return bool(isinstance(quality, Mapping) and quality.get("home_stats") and quality.get("away_stats"))

def _dixon_coles_tau(home: int, away: int, home_lambda: float, away_lambda: float, rho: float) -> float:
    if home == 0 and away == 0: return 1 - home_lambda * away_lambda * rho
    if home == 0 and away == 1: return 1 + home_lambda * rho
    if home == 1 and away == 0: return 1 + away_lambda * rho
    if home == 1 and away == 1: return 1 - rho
    return 1.0

def _score_matrix(home_lambda: float, away_lambda: float, rho: float = -0.08, max_goals: int = 8) -> np.ndarray:
    home = np.arange(max_goals + 1)
    away = np.arange(max_goals + 1)
    hp = np.exp(-home_lambda) * np.power(home_lambda, home) / np.vectorize(math.factorial)(home)
    ap = np.exp(-away_lambda) * np.power(away_lambda, away) / np.vectorize(math.factorial)(away)
    matrix = np.outer(hp, ap)
    for i in range(2):
        for j in range(2): matrix[i, j] *= _dixon_coles_tau(i, j, home_lambda, away_lambda, rho)
    return matrix / matrix.sum()

def _matrix_mode(matrix: np.ndarray) -> tuple[int, int]:
    index = np.unravel_index(int(np.argmax(matrix)), matrix.shape)
    return int(index[0]), int(index[1])

def _market_key(market: Mapping[str, Any]) -> tuple[str, str, Optional[float]]:
    line = market.get("line")
    if market.get("market_type") == "spread": line = abs(float(line))
    return str(market.get("market_type")), str(market.get("side")), None if line is None else round(float(line), 2)

def _devig_market_probabilities(markets: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, Optional[float]], float]:
    groups: dict[tuple[str, Optional[float]], list[Mapping[str, Any]]] = {}
    for market in markets:
        line = market.get("line")
        group_line = None if market.get("market_type") == "moneyline" else round(abs(float(line)), 2)
        groups.setdefault((str(market.get("market_type")), group_line), []).append(market)
    output: dict[tuple[str, str, Optional[float]], float] = {}
    expected = {"moneyline": 3, "spread": 2, "total": 2}
    for (market_type, _), items in groups.items():
        if len(items) != expected.get(market_type): continue
        inverse = [1 / float(item["decimal_price"]) for item in items]
        total = sum(inverse)
        for item, implied in zip(items, inverse): output[_market_key(item)] = implied / total
    return output

def _matrix_market_value(matrix: np.ndarray, market: Mapping[str, Any]) -> tuple[float, float]:
    home, away = np.indices(matrix.shape)
    if market["market_type"] == "moneyline":
        settled = home > away if market["side"] == "home" else away > home if market["side"] == "away" else home == away
        probability = float(matrix[settled].sum())
        return probability, probability * float(market["decimal_price"]) - 1
    values = home - away if market["market_type"] == "spread" and market["side"] == "home" else away - home if market["market_type"] == "spread" else home + away if market["side"] == "over" else -(home + away)
    handicap = float(market["line"]) if market["market_type"] == "spread" else -float(market["line"]) if market["side"] == "over" else float(market["line"])
    profits, wins = [], []
    for leg in _asian_parts(handicap):
        settled = np.sign(values + leg)
        profits.append(np.where(settled > 0, float(market["decimal_price"]) - 1, np.where(settled < 0, -1., 0.)))
        wins.append(settled > 0)
    return float((np.mean(np.stack(wins), axis=0) * matrix).sum()), float((np.mean(np.stack(profits), axis=0) * matrix).sum())

def _logit_blend(model_probability: float, market_probability: float, model_weight: float) -> float:
    clamp = lambda p: min(.999, max(.001, float(p)))
    logit = lambda p: math.log(clamp(p) / (1 - clamp(p)))
    value = model_weight * logit(model_probability) + (1 - model_weight) * logit(market_probability)
    return 1 / (1 + math.exp(-value))

def _fusion_model_weight(confidence: float, quality: Mapping[str, Any]) -> float:
    penalty = .10 if not quality.get("clubelo") else 0
    penalty += .10 if not quality.get("lineups_confirmed") else 0
    penalty += min(.20, .03 * len(quality.get("fallback_reason", [])))
    return round(max(.15, min(.75, confidence - penalty)), 3)

def _fusion_reason(model_weight: float, quality: Mapping[str, Any]) -> str:
    if model_weight <= .30: return "資料品質/先發/ClubElo 不完整：提高市場權重"
    if model_weight >= .60: return "資料完整且模型品質較高：提高模型權重"
    return "模型與市場依資料品質進行 logit 融合"

def _has_configured_season(seasons: Mapping[str, int]) -> bool:
    """Whether any supported league has an API-Football season configured."""
    return any(isinstance(seasons.get(league), int) and seasons[league] >= 1900 for league in DEFAULT_LEAGUES)

def _saved_forecast(model: Mapping[str, Any], quant_forecast: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Build the legacy display forecast from an already-saved Football model.

    This is deliberately a pure compatibility wrapper for Core and old
    integrations.  It does not fetch, write SQLite, validate markets, or run
    recommendation/+EV calculation.  ``home_xg`` and ``away_xg`` retain their
    historic field names but are explicitly labelled as model expected goals,
    not shot-based provider xG.
    """
    audit = quant_forecast if isinstance(quant_forecast, Mapping) else model.get("quant_forecast")
    home_lambda = float((audit or {}).get("model_lambda_home", model.get("model_lambda_home", model.get("lambda_home", 0.0))) or 0.0)
    away_lambda = float((audit or {}).get("model_lambda_away", model.get("model_lambda_away", model.get("lambda_away", 0.0))) or 0.0)
    matrix = _score_matrix(home_lambda, away_lambda, rho=-0.08)
    mode = (audit or {}).get("exact_score_mode") or model.get("exact_score_mode")
    if not mode:
        selected = _matrix_mode(matrix)
        mode = f"{selected[0]}:{selected[1]}"
    one_x_two = (audit or {}).get("one_x_two") or {}
    home, away = np.indices(matrix.shape)
    return {
        "home_xg": home_lambda,
        "away_xg": away_lambda,
        "score": str(mode),
        "home_probability": float(one_x_two.get("home", matrix[home > away].sum())),
        "draw_probability": float(one_x_two.get("draw", matrix[home == away].sum())),
        "away_probability": float(one_x_two.get("away", matrix[home < away].sum())),
        "goal_semantics": "模型預估進球（非 shot-based 資料 xG）",
        "score_distribution_method": "Dixon-Coles adjusted Poisson joint score matrix",
    }

def _rows_with_saved_forecast(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalise older persisted snapshot rows without mutating SQLite."""
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if not isinstance(row.get("forecast"), Mapping):
            row["forecast"] = _saved_forecast(row.get("model") or {}, row.get("quant_forecast"))
        output.append(row)
    return output

def _quant_forecast(model: Mapping[str, Any], matrix: np.ndarray, recommendations: list[Mapping[str, Any]]) -> dict[str, Any]:
    home, away = np.indices(matrix.shape)
    mode = _matrix_mode(matrix)
    return {"model_version": FOOTBALL_MODEL_VERSION, "goal_semantics": model.get("goal_semantics"),
            "model_lambda_home": model.get("model_lambda_home", model.get("lambda_home")),
            "model_lambda_away": model.get("model_lambda_away", model.get("lambda_away")),
            "expected_goals_conceded_home": model.get("expected_goals_conceded_home"),
            "expected_goals_conceded_away": model.get("expected_goals_conceded_away"),
            "exact_score_mode": f"{mode[0]}:{mode[1]}", "score_distribution_method": model.get("score_distribution_method"),
            "one_x_two": {"home": float(matrix[home > away].sum()), "draw": float(matrix[home == away].sum()), "away": float(matrix[home < away].sum())},
            "quality": model.get("quality", {}), "recommendation_audit": recommendations}

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
                side = "over" if mt == "total" and name.casefold() == "over" else "under" if mt == "total" else "home" if name == home else "away" if name == away else "draw" if mt == "moneyline" and name.casefold() == "draw" else None
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

def _match_odds(records: Iterable[Mapping[str, Any]], event: Mapping[str, Any]) -> list[dict[str, Any]]:
    # Kept outside the class for testability; match on league, two teams, kickoff within 30 min.
    target = datetime.fromisoformat(str(event["kickoff"]).replace("Z", "+00:00"))
    # Market sides must remain ordered: accepting a reversed home/away pair
    # would invert Asian-handicap and 1X2 recommendations.
    target_teams = (_team_key(event["home"]), _team_key(event["away"]))
    out=[]
    for row in records:
        try: delta=abs((target-datetime.fromisoformat(str(row["kickoff"]).replace("Z", "+00:00"))).total_seconds())
        except (ValueError, TypeError): continue
        row_teams = (_team_key(row.get("home")), _team_key(row.get("away")))
        if row.get("league") == event.get("league_key") and target_teams == row_teams and delta <= 1800:
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
