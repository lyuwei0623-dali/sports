"""Independent MLB pre-release module.

Purpose
-------
Build the Taiwan 19:30 MLB recommendation set without requiring confirmed
lineups.  It uses only public/free sources for objective baseball data and
keeps model risk separate from betting recommendation eligibility:

* Green / yellow / red describe data certainty.
* A red match is still priced and can be recommended when its calibrated EV
  clears the configured threshold.
* The published record always states which data were inferred and why.

Required third-party packages: requests, numpy

This file is intentionally independent of the existing Core and Football
modules.  The host app only needs to call ``MLBPreReleaseService.run`` and
render the returned dictionaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import csv
from io import StringIO
import json
import math
import os
import re
import sqlite3
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np


TZ_TAIPEI = timezone(timedelta(hours=8))
MLB_API = "https://statsapi.mlb.com/api/v1"
SAVANT_PROBABLES = "https://baseballsavant.mlb.com/probable-pitchers"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
THE_ODDS_API = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
AUTO_ODDS_MAX_AGE = timedelta(hours=3)
AUTO_MONEYLINE_MIN_PROBABILITY_GAP = .03
DEFAULT_RELEASE_DB_PATH = os.environ.get("MLB_RELEASE_DB_PATH", "mlb_release.sqlite3")


class RiskLevel(str, Enum):
    GREEN = "綠燈"
    YELLOW = "黃燈"
    RED = "紅燈"


@dataclass(frozen=True)
class SourceStamp:
    source: str
    fetched_at: str
    status: str = "ok"


@dataclass(frozen=True)
class Game:
    event_id: str
    kickoff: str
    home: str
    away: str
    home_team_id: Optional[int]
    away_team_id: Optional[int]
    home_pitcher: Optional[Mapping[str, Any]]
    away_pitcher: Optional[Mapping[str, Any]]
    venue_id: Optional[int]
    venue_name: str
    status: str


@dataclass(frozen=True)
class DataRisk:
    level: RiskLevel
    reasons: tuple[str, ...]
    starter_status: str
    lineup_status: str
    last_checked_at: str


@dataclass(frozen=True)
class TeamFeature:
    team: str
    expected_lineup: tuple[int, ...]
    lineup_xwoba: float
    bullpen_multiplier: float
    bullpen_note: str
    opponent_hand: str


@dataclass(frozen=True)
class ModelOutput:
    event_id: str
    home_lambda: float
    away_lambda: float
    home_win_probability: float
    away_win_probability: float
    projected_total: float
    fair_home_spread: float
    simulations: int


@dataclass(frozen=True)
class SuperQuote:
    """One manually verified SUPER market quote.

    ``price`` is the SUPER Hong Kong odds excluding stake, e.g. 0.94.
    ``raw_line`` accepts regular Asian lines and special forms such as -1+65,
    +1-50, 9+90, or 8-55.
    """

    market_type: str  # moneyline, spread, total
    side: str         # home, away, over, under
    raw_line: Optional[str]
    price: float


@dataclass(frozen=True)
class Recommendation:
    market_type: str
    side: str
    raw_line: Optional[str]
    decimal_price: float
    probability: float
    implied_probability: float
    ev: float
    playable: bool
    label: str


@dataclass(frozen=True)
class OddsMarket:
    """Latest coherent bookmaker quote set for one MLB event."""

    home_team: str
    away_team: str
    commence_time: str
    bookmaker: str
    updated_at: str
    moneyline: Mapping[str, float]
    spreads: Mapping[str, tuple[float, float]]
    totals: Mapping[str, tuple[float, float]]
    market_updated_at: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AutoMarketDecision:
    favored_team: Optional[str]
    underdog_team: Optional[str]
    favorite_side: Optional[str]
    baseline_line: Optional[float]
    direction_source: Optional[str]
    odds_updated_at: Optional[str]
    spread_prices: Mapping[str, float]
    total_line: Optional[float]
    total_prices: Mapping[str, float]
    moneyline_prices: Mapping[str, float]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PublishedGame:
    game: Game
    risk: DataRisk
    model: ModelOutput
    recommendations: tuple[Recommendation, ...]
    sources: tuple[SourceStamp, ...]
    first_market_quotes: tuple[SuperQuote, ...] = field(default_factory=tuple)
    latest_market_quotes: tuple[SuperQuote, ...] = field(default_factory=tuple)
    calibrated_at: Optional[str] = None
    settlement_status: str = "pending"

    def as_member_payload(self) -> dict[str, Any]:
        """Stable MLB-only payload for later integration into the shared UI."""
        risk_display = format_risk_display(self.risk)
        sources_display = format_sources_display(self.sources)
        first_market = format_market_quotes(self.first_market_quotes, self.game)
        latest_market = format_market_quotes(self.latest_market_quotes, self.game)
        market_change = format_market_change(
            self.first_market_quotes, self.latest_market_quotes, self.game)
        calibrated_at = self.calibrated_at or latest_source_time(self.sources)
        return {
            "sport": "mlb",
            "event_id": self.game.event_id,
            "kickoff": self.game.kickoff,
            "teams": {"away": self.game.away, "home": self.game.home},
            "pitchers_display": "客：" + str((self.game.away_pitcher or {}).get("fullName") or "尚未確認") +
                                "\n主：" + str((self.game.home_pitcher or {}).get("fullName") or "尚未確認"),
            "venue_display": self.game.venue_name,
            "first_market": first_market,
            "latest_market": latest_market,
            "market_change": market_change,
            "updated_at": calibrated_at,
            "calibrated_at": calibrated_at,
            "risk": risk_display,
            "risk_display": risk_display,
            "warning": "",
            "model": format_model_display(self.model),
            "model_data": asdict(self.model),
            "recommendations": [format_recommendation_payload(r, self.game) for r in self.recommendations],
            "sources": sources_display,
            "sources_display": sources_display,
            "settlement_status": normalize_settlement_status(self.settlement_status),
        }


def format_risk_display(risk: DataRisk) -> str:
    details = "／".join(risk.reasons) if risk.reasons else "資料狀態正常"
    return f"{risk.level.value}｜{details}"


def format_sources_display(sources: Iterable[SourceStamp]) -> str:
    values = []
    for source in sources:
        timestamp = f"（{source.fetched_at}）" if source.fetched_at else ""
        status = "" if source.status == "ok" else f"[{source.status}]"
        values.append(f"{source.source}{timestamp}{status}")
    return "；".join(values) if values else "資料來源未提供"


def format_model_display(model: ModelOutput) -> str:
    return (
        f"主勝 {model.home_win_probability * 100:.1f}%／"
        f"客勝 {model.away_win_probability * 100:.1f}%｜"
        f"預估總分 {model.projected_total:.2f}｜"
        f"主隊合理讓分 {model.fair_home_spread:+g}"
    )


def market_selection(quote: SuperQuote, game: Game) -> str:
    side = {
        "home": game.home,
        "away": game.away,
        "over": "大分",
        "under": "小分",
    }.get(quote.side, quote.side)
    line = f" {quote.raw_line}" if quote.raw_line else ""
    if quote.market_type == "moneyline":
        selection = f"{side}獨贏"
    elif quote.market_type == "spread":
        selection = f"{side}讓分{line}"
    elif quote.market_type == "total":
        selection = f"{side}{line}"
    else:
        selection = f"{side}{quote.market_type}{line}"
    price = f"{quote.price:.3f}".rstrip("0").rstrip(".")
    return f"{selection}｜香港盤 {price}"


def format_market_quotes(quotes: Iterable[SuperQuote], game: Game) -> str:
    rows = tuple(quotes)
    return "；".join(market_selection(q, game) for q in rows) if rows else "人工校正盤未提供"


def format_market_change(first: Iterable[SuperQuote], latest: Iterable[SuperQuote], game: Game) -> str:
    first_map = {(q.market_type, q.side): q for q in first}
    latest_map = {(q.market_type, q.side): q for q in latest}
    if not first_map or not latest_map:
        return "無法比較：首次盤或最新盤未提供"
    changes = []
    for key in sorted(set(first_map) | set(latest_map)):
        old, new = first_map.get(key), latest_map.get(key)
        if old is None:
            changes.append(f"新增：{market_selection(new, game)}")
        elif new is None:
            changes.append(f"移除：{market_selection(old, game)}")
        elif old.raw_line != new.raw_line or not math.isclose(old.price, new.price):
            changes.append(f"{market_selection(old, game)} → {market_selection(new, game)}")
    return "；".join(changes) if changes else "無變化"


def format_recommendation_payload(rec: Recommendation, game: Game) -> dict[str, Any]:
    hk_price = rec.decimal_price - 1.0
    quote = SuperQuote(rec.market_type, rec.side, rec.raw_line, hk_price)
    payload = asdict(rec)
    payload.update({
        "price": hk_price,
        "display": market_selection(quote, game),
        "selection": market_selection(quote, game),
    })
    return payload


def latest_source_time(sources: Iterable[SourceStamp]) -> Optional[str]:
    values = [source.fetched_at for source in sources if source.fetched_at]
    return max(values) if values else None


def normalize_settlement_status(value: Any) -> str:
    normalized = str(value or "pending").lower()
    return normalized if normalized in {"win", "push", "loss", "pending"} else "pending"


MLB_RELEASE_STATUSES = {
    "fetching",
    "awaiting_manual_calibration",
    "ready_to_publish",
    "published",
    "failed",
}


class MLBReleaseSnapshotStore:
    """SQLite persistence for calibrated MLB member payloads.

    This class is deliberately isolated from ``MLBPreReleaseService``.  Read
    methods perform SQLite reads and JSON decoding only; they cannot fetch
    external data or invoke model calculations.
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS mlb_daily_release_snapshots (
                    date_str TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '[]',
                    calibrated_at TEXT,
                    published_at TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    CHECK (status IN (
                        'fetching', 'awaiting_manual_calibration',
                        'ready_to_publish', 'published', 'failed'
                    ))
                );

                CREATE TABLE IF NOT EXISTS mlb_daily_release_history (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_str TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '[]',
                    calibrated_at TEXT,
                    published_at TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_mlb_release_history_date
                ON mlb_daily_release_history(date_str, revision_id);

                CREATE TABLE IF NOT EXISTS mlb_automatic_snapshots (
                    date_str TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '[]',
                    source_updated_at TEXT,
                    last_success_at TEXT,
                    last_attempt_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    CHECK (status IN ('automatic_snapshot', 'failed')),
                    CHECK (stale IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS mlb_automatic_snapshot_history (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_str TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '[]',
                    source_updated_at TEXT,
                    last_success_at TEXT,
                    last_attempt_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_mlb_auto_history_date
                ON mlb_automatic_snapshot_history(date_str, revision_id);
            """)

    def save_calibrated_snapshot(
        self,
        date_str: str,
        published_games: Iterable[PublishedGame | Mapping[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """Save a full calibrated payload list as ``ready_to_publish``."""
        _validate_date_str(date_str)
        payloads = [
            game.as_member_payload() if isinstance(game, PublishedGame) else dict(game)
            for game in published_games
        ]
        _validate_member_payloads(payloads)
        payload_json = json.dumps(payloads, ensure_ascii=False, separators=(",", ":"))
        calibrated_at = _latest_calibrated_at(payloads) or _taipei_now_iso()
        updated_at = _taipei_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO mlb_daily_release_snapshots
                    (date_str,status,payload_json,calibrated_at,published_at,note,updated_at)
                VALUES (?, 'ready_to_publish', ?, ?, NULL, ?, ?)
                ON CONFLICT(date_str) DO UPDATE SET
                    status='ready_to_publish',
                    payload_json=excluded.payload_json,
                    calibrated_at=excluded.calibrated_at,
                    published_at=NULL,
                    note=excluded.note,
                    updated_at=excluded.updated_at
            """, (date_str, payload_json, calibrated_at, str(note), updated_at))
            self._append_history(connection, date_str)
        return {
            "date_str": date_str,
            "status": "ready_to_publish",
            "calibrated_at": calibrated_at,
            "updated_at": updated_at,
        }

    def confirm_daily_release(self, date_str: str, note: str = "") -> dict[str, Any]:
        """Publish only an existing ``ready_to_publish`` snapshot."""
        _validate_date_str(date_str)
        now = _taipei_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM mlb_daily_release_snapshots WHERE date_str=?",
                (date_str,),
            ).fetchone()
            if row is None or row["status"] != "ready_to_publish":
                raise ValueError("MLB 每日發布僅允許由 ready_to_publish 狀態確認")
            if note:
                connection.execute("""
                    UPDATE mlb_daily_release_snapshots
                    SET status='published', published_at=?, note=?, updated_at=?
                    WHERE date_str=?
                """, (now, str(note), now, date_str))
            else:
                connection.execute("""
                    UPDATE mlb_daily_release_snapshots
                    SET status='published', published_at=?, updated_at=?
                    WHERE date_str=?
                """, (now, now, date_str))
            self._append_history(connection, date_str)
        return {
            "date_str": date_str,
            "status": "published",
            "published_at": now,
            "updated_at": now,
        }

    def mark_release_failed(self, date_str: str, note: str) -> dict[str, Any]:
        """Mark current state failed while retaining payload and all revisions."""
        _validate_date_str(date_str)
        if not str(note).strip():
            raise ValueError("failed 狀態必須提供 note")
        now = _taipei_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO mlb_daily_release_snapshots
                    (date_str,status,payload_json,calibrated_at,published_at,note,updated_at)
                VALUES (?, 'failed', '[]', NULL, NULL, ?, ?)
                ON CONFLICT(date_str) DO UPDATE SET
                    status='failed', note=excluded.note, updated_at=excluded.updated_at
            """, (date_str, str(note), now))
            self._append_history(connection, date_str)
        return {"date_str": date_str, "status": "failed", "updated_at": now}

    def save_automatic_snapshot(
        self,
        date_str: str,
        payloads: Iterable[Mapping[str, Any]],
        source_updated_at: Optional[str],
        note: str = "",
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Persist one successful automatic baseline without touching manual data."""
        _validate_date_str(date_str)
        rows = [dict(payload) for payload in payloads]
        _validate_member_payloads(rows)
        for index, payload in enumerate(rows):
            if payload.get("snapshot_kind") != "automatic_baseline":
                raise ValueError(f"MLB automatic payload[{index}] snapshot_kind 不合法")
        payload_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        timestamp = _as_taipei_iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO mlb_automatic_snapshots
                    (date_str,status,payload_json,source_updated_at,last_success_at,
                     last_attempt_at,note,updated_at,stale)
                VALUES (?, 'automatic_snapshot', ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(date_str) DO UPDATE SET
                    status='automatic_snapshot', payload_json=excluded.payload_json,
                    source_updated_at=excluded.source_updated_at,
                    last_success_at=excluded.last_success_at,
                    last_attempt_at=excluded.last_attempt_at,
                    note=excluded.note, updated_at=excluded.updated_at, stale=0
            """, (date_str, payload_json, source_updated_at, timestamp, timestamp,
                  str(note), timestamp))
            self._append_automatic_history(connection, date_str)
        return {"date_str": date_str, "status": "automatic_snapshot",
                "updated_at": timestamp, "game_count": len(rows),
                "snapshot_kind": "automatic_baseline", "member_available": True}

    def mark_automatic_snapshot_failed(
        self,
        date_str: str,
        note: str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Record a failed attempt while retaining the last successful payload."""
        _validate_date_str(date_str)
        if not str(note).strip():
            raise ValueError("automatic failed 狀態必須提供 note")
        timestamp = _as_taipei_iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO mlb_automatic_snapshots
                    (date_str,status,payload_json,source_updated_at,last_success_at,
                     last_attempt_at,note,updated_at,stale)
                VALUES (?, 'failed', '[]', NULL, NULL, ?, ?, ?, 1)
                ON CONFLICT(date_str) DO UPDATE SET
                    status='failed', last_attempt_at=excluded.last_attempt_at,
                    note=excluded.note, stale=1
            """, (date_str, timestamp, str(note), timestamp))
            self._append_automatic_history(connection, date_str)
            row = connection.execute(
                "SELECT payload_json,updated_at FROM mlb_automatic_snapshots WHERE date_str=?",
                (date_str,),
            ).fetchone()
        retained = bool(row and json.loads(row["payload_json"]))
        return {"date_str": date_str, "status": "failed", "updated_at": row["updated_at"],
                "retained_previous_snapshot": retained}

    def mark_automatic_no_games(
        self,
        date_str: str,
        note: str = "當日無可用 MLB 賽事",
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Record a definitive empty slate without publishing an empty snapshot."""
        _validate_date_str(date_str)
        timestamp = _as_taipei_iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO mlb_automatic_snapshots
                    (date_str,status,payload_json,source_updated_at,last_success_at,
                     last_attempt_at,note,updated_at,stale)
                VALUES (?, 'failed', '[]', NULL, NULL, ?, ?, ?, 0)
                ON CONFLICT(date_str) DO UPDATE SET
                    status='failed', payload_json='[]', source_updated_at=NULL,
                    last_success_at=NULL, last_attempt_at=excluded.last_attempt_at,
                    note=excluded.note, updated_at=excluded.updated_at, stale=0
            """, (date_str, timestamp, str(note), timestamp))
            self._append_automatic_history(connection, date_str)
        return {"date_str": date_str, "status": "failed", "updated_at": timestamp,
                "retained_previous_snapshot": False}

    def get_automatic_snapshot_for_backend(self, date_str: str) -> Optional[dict[str, Any]]:
        """Backend history lookup used to render first/latest market changes."""
        _validate_date_str(date_str)
        with self._connect() as connection:
            row = connection.execute("""
                SELECT payload_json,updated_at,source_updated_at,status,stale,note
                FROM mlb_automatic_snapshots WHERE date_str=?
            """, (date_str,)).fetchone()
        if row is None:
            return None
        payloads = json.loads(row["payload_json"])
        if not isinstance(payloads, list):
            raise ValueError("MLB 自動快照 payload_json 必須是 JSON array")
        return {"payloads": payloads, "updated_at": row["updated_at"],
                "source_updated_at": row["source_updated_at"], "status": row["status"],
                "stale": bool(row["stale"]), "note": row["note"]}

    def get_member_snapshot(self, date_str: str) -> dict[str, Any]:
        """Read a member snapshot without API access or model execution.

        A manual ``published`` snapshot has priority.  Otherwise the latest
        saved automatic baseline is exposed as ``automatic_available``.
        """
        _validate_date_str(date_str)
        with self._connect() as connection:
            manual = connection.execute("""
                SELECT status,payload_json,updated_at,published_at,note
                FROM mlb_daily_release_snapshots WHERE date_str=?
            """, (date_str,)).fetchone()
            automatic = connection.execute("""
                SELECT status,payload_json,updated_at,last_success_at,note,stale
                FROM mlb_automatic_snapshots WHERE date_str=?
            """, (date_str,)).fetchone()
        if manual is not None and manual["status"] == "published":
            payloads = json.loads(manual["payload_json"])
            if not isinstance(payloads, list):
                raise ValueError("MLB 發布快照 payload_json 必須是 JSON array")
            return {
                "release_status": "published", "payloads": payloads,
                "snapshot_kind": "manual",
                "calibration_state": "人工 SUPER 校正",
                "updated_at": manual["updated_at"], "published_at": manual["published_at"],
                "calibration_source": "SUPER 人工校正", "stale_warning": None,
                "note": manual["note"],
            }
        if automatic is not None:
            payloads = json.loads(automatic["payload_json"])
            if not isinstance(payloads, list):
                raise ValueError("MLB 自動快照 payload_json 必須是 JSON array")
            if payloads:
                if automatic["stale"]:
                    payloads = [_with_stale_warning(p, automatic["note"]) for p in payloads]
                return {
                    "release_status": "automatic_available", "payloads": payloads,
                    "snapshot_kind": "automatic_baseline",
                    "calibration_state": "尚未人工 SUPER 校正",
                    "updated_at": automatic["updated_at"],
                    "published_at": automatic["last_success_at"],
                    "calibration_source": "The Odds API 自動基準盤",
                    "stale_warning": automatic["note"] if automatic["stale"] else None,
                    "note": automatic["note"],
                }
        if manual is None and automatic is None:
            return {
                "release_status": "fetching",
                "payloads": [],
                "updated_at": None,
                "published_at": None,
                "calibration_source": "SUPER 人工校正",
                "note": "",
            }
        status_row = manual if manual is not None else automatic
        return {
            "release_status": status_row["status"], "payloads": [],
            "updated_at": status_row["updated_at"], "published_at": None,
            "calibration_source": "SUPER 人工校正" if manual is not None else "The Odds API 自動基準盤",
            "note": status_row["note"],
        }

    @staticmethod
    def _append_history(connection: sqlite3.Connection, date_str: str) -> None:
        connection.execute("""
            INSERT INTO mlb_daily_release_history
                (date_str,status,payload_json,calibrated_at,published_at,note,updated_at)
            SELECT date_str,status,payload_json,calibrated_at,published_at,note,updated_at
            FROM mlb_daily_release_snapshots WHERE date_str=?
        """, (date_str,))

    @staticmethod
    def _append_automatic_history(connection: sqlite3.Connection, date_str: str) -> None:
        connection.execute("""
            INSERT INTO mlb_automatic_snapshot_history
                (date_str,status,payload_json,source_updated_at,last_success_at,
                 last_attempt_at,note,updated_at,stale)
            SELECT date_str,status,payload_json,source_updated_at,last_success_at,
                   last_attempt_at,note,updated_at,stale
            FROM mlb_automatic_snapshots WHERE date_str=?
        """, (date_str,))


def _validate_date_str(date_str: str) -> None:
    try:
        date.fromisoformat(str(date_str))
    except ValueError as exc:
        raise ValueError("date_str 必須是 YYYY-MM-DD") from exc


def _validate_member_payloads(payloads: list[Mapping[str, Any]]) -> None:
    required = {
        "first_market", "latest_market", "market_change", "risk", "warning",
        "model", "recommendations", "sources", "settlement_status",
    }
    for index, payload in enumerate(payloads):
        missing = required - set(payload)
        if missing:
            raise ValueError(f"MLB payload[{index}] 缺少會員合約欄位：{sorted(missing)}")
        if normalize_settlement_status(payload.get("settlement_status")) != payload.get("settlement_status"):
            raise ValueError(f"MLB payload[{index}] settlement_status 不合法")


def _latest_calibrated_at(payloads: Iterable[Mapping[str, Any]]) -> Optional[str]:
    values = [
        str(payload.get("calibrated_at") or payload.get("updated_at"))
        for payload in payloads
        if payload.get("calibrated_at") or payload.get("updated_at")
    ]
    return max(values) if values else None


def _taipei_now_iso() -> str:
    return datetime.now(TZ_TAIPEI).isoformat()


def _as_taipei_iso(value: Optional[datetime]) -> str:
    current = value or datetime.now(TZ_TAIPEI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ_TAIPEI)
    return current.astimezone(TZ_TAIPEI).isoformat()


def _with_stale_warning(payload: Mapping[str, Any], note: str) -> dict[str, Any]:
    result = dict(payload)
    suffix = "非最新自動快照"
    if note:
        suffix += f"｜{note}"
    warning = str(result.get("warning") or "")
    if "非最新自動快照" not in warning:
        result["warning"] = "｜".join(value for value in (warning, suffix) if value)
    return result


class HTTP:
    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self._cache = {}

    def _read(self, url: str, params: Mapping[str, Any]) -> bytes:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        request_url = f"{url}?{query}" if query else url
        if request_url in self._cache:
            return self._cache[request_url]
        request = Request(request_url, headers={"User-Agent": "MLB-pre-release-module/1.0"})
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    self._cache[request_url] = body
                    return body
            except Exception as exc:
                if attempt or getattr(exc, "code", None) in (400, 401, 403, 404, 422, 429):
                    raise

    def get_json(self, url: str, **params: Any) -> Mapping[str, Any]:
        data = json.loads(self._read(url, params).decode("utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError(f"Expected JSON object from {url}")
        return data

    def get_text(self, url: str, **params: Any) -> str:
        return self._read(url, params).decode("utf-8")


class TheOddsAPIClient:
    """Backend-only reader for conventional MLB odds.

    It intentionally requests decimal odds so automatic snapshots never share
    SUPER's Hong Kong special-line vocabulary.
    """

    def __init__(self, http: Optional[HTTP] = None):
        self.http = http or HTTP()

    def fetch_mlb_markets(self, api_key: str) -> list[OddsMarket]:
        if not str(api_key).strip():
            raise ValueError("The Odds API key 不可為空")
        raw = json.loads(self.http._read(THE_ODDS_API, {
            "apiKey": api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }).decode("utf-8"))
        if not isinstance(raw, list):
            raise ValueError("The Odds API MLB 回應必須是 JSON array")
        return [market for event in raw if (market := _parse_odds_event(event)) is not None]


def _parse_odds_event(event: Any) -> Optional[OddsMarket]:
    if not isinstance(event, Mapping):
        return None
    home, away = str(event.get("home_team") or ""), str(event.get("away_team") or "")
    commence = str(event.get("commence_time") or "")
    if not home or not away or not commence:
        return None
    latest: dict[str, tuple[datetime, str, Mapping[str, Any]]] = {}
    for bookmaker in event.get("bookmakers") or ():
        if not isinstance(bookmaker, Mapping):
            continue
        book_name = str(bookmaker.get("title") or bookmaker.get("key") or "bookmaker")
        for market in bookmaker.get("markets") or ():
            if not isinstance(market, Mapping) or market.get("key") not in {"h2h", "spreads", "totals"}:
                continue
            updated = str(market.get("last_update") or bookmaker.get("last_update") or "")
            try:
                parsed = parse_iso(updated)
            except (TypeError, ValueError):
                continue
            key = str(market["key"])
            if key not in latest or parsed > latest[key][0]:
                latest[key] = (parsed, book_name, market)
    if not latest:
        return None
    moneyline: dict[str, float] = {}
    spreads: dict[str, tuple[float, float]] = {}
    totals: dict[str, tuple[float, float]] = {}
    for key, (_, _, market) in latest.items():
        for outcome in market.get("outcomes") or ():
            if not isinstance(outcome, Mapping):
                continue
            try:
                price = float(outcome["price"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(outcome.get("name") or "")
            if key == "h2h" and name in {home, away}:
                moneyline[name] = price
            elif key == "spreads" and name in {home, away}:
                try:
                    spreads[name] = (float(outcome["point"]), price)
                except (KeyError, TypeError, ValueError):
                    continue
            elif key == "totals" and name in {"Over", "Under"}:
                try:
                    totals[name.lower()] = (float(outcome["point"]), price)
                except (KeyError, TypeError, ValueError):
                    continue
    newest = max(value[0] for value in latest.values())
    books = " / ".join(sorted({value[1] for value in latest.values()}))
    market_times = {key: value[0].isoformat() for key, value in latest.items()}
    return OddsMarket(home, away, commence, books, newest.isoformat(), moneyline, spreads, totals,
                      market_times)


def decide_automatic_market(
    game: Game,
    odds: Optional[OddsMarket],
    now: datetime,
) -> AutoMarketDecision:
    """Validate direction and conventional lines without changing EV rules."""
    empty = AutoMarketDecision(None, None, None, None, None, None, {}, None, {}, {},
                               ("讓分方向待人工校正｜The Odds API 無對應場次",))
    if odds is None:
        return empty
    current = now if now.tzinfo else now.replace(tzinfo=TZ_TAIPEI)
    def is_stale(market_key: str) -> bool:
        value = odds.market_updated_at.get(market_key, odds.updated_at)
        updated = parse_iso(value)
        return current.astimezone(timezone.utc) - updated.astimezone(timezone.utc) > AUTO_ODDS_MAX_AGE

    warnings: list[str] = []
    favored: Optional[str] = None
    source: Optional[str] = None
    negative = [(team, line_price) for team, line_price in odds.spreads.items()
                if line_price[0] < 0]
    if odds.spreads and is_stale("spreads"):
        warnings.append("讓分方向待人工校正｜The Odds API spreads 資料已過期")
    elif len(negative) == 1:
        favored = negative[0][0]
        source = "The Odds API spreads"
    elif not odds.spreads:
        home_price, away_price = odds.moneyline.get(game.home), odds.moneyline.get(game.away)
        if odds.moneyline and is_stale("h2h"):
            warnings.append("讓分方向待人工校正｜The Odds API moneyline 資料已過期")
        elif home_price and away_price and home_price > 1 and away_price > 1:
            home_p, away_p = 1 / home_price, 1 / away_price
            if abs(home_p - away_p) >= AUTO_MONEYLINE_MIN_PROBABILITY_GAP:
                favored = game.home if home_p > away_p else game.away
                source = "The Odds API moneyline inferred"
            else:
                warnings.append("讓分方向待人工校正｜兩隊 moneyline 隱含機率過於接近")
        else:
            warnings.append("讓分方向待人工校正｜The Odds API 缺少 spreads 與完整 moneyline")
    else:
        warnings.append("讓分方向待人工校正｜spreads 無法可靠判定唯一讓分隊")

    underdog = game.away if favored == game.home else game.home if favored == game.away else None
    favorite_side = "home" if favored == game.home else "away" if favored == game.away else None
    spread_prices: dict[str, float] = {}
    if favored and underdog and odds.spreads:
        favored_quote, underdog_quote = odds.spreads.get(favored), odds.spreads.get(underdog)
        if (favored_quote and underdog_quote and math.isclose(favored_quote[0], -1.5)
                and math.isclose(underdog_quote[0], 1.5)):
            spread_prices = {favorite_side: favored_quote[1],
                             "away" if favorite_side == "home" else "home": underdog_quote[1]}
        else:
            warnings.append("讓分方向待人工校正｜缺少標準 -1.5／+1.5 對盤價格")
    elif favored:
        warnings.append("moneyline 僅供方向推定；缺少 spreads，未產出自動讓分推薦")

    total_line: Optional[float] = None
    total_prices: dict[str, float] = {}
    over, under = odds.totals.get("over"), odds.totals.get("under")
    if odds.totals and is_stale("totals"):
        warnings.append("The Odds API totals 資料已過期，未產出大小分推薦")
    elif over and under and math.isclose(over[0], under[0]):
        total_line = over[0]
        total_prices = {"over": over[1], "under": under[1]}
    elif odds.totals:
        warnings.append("The Odds API totals 中心盤不一致，未產出大小分推薦")

    return AutoMarketDecision(
        favored, underdog, favorite_side, -1.5 if favored else None, source,
        odds.updated_at, spread_prices, total_line, total_prices,
        {"home": odds.moneyline[game.home], "away": odds.moneyline[game.away]}
        if (game.home in odds.moneyline and game.away in odds.moneyline
            and not is_stale("h2h")) else {},
        tuple(warnings),
    )


class MLBOfficialClient:
    """Free official MLB source: schedule, rosters, boxscores, venue data."""

    def __init__(self, http: HTTP):
        self.http = http

    def games_for_taiwan_date(self, day: date) -> list[Game]:
        start = (day - timedelta(days=1)).isoformat()
        # Taiwan's day spans the prior UTC afternoon through the current UTC
        # afternoon. Querying one unnecessary future MLB date roughly doubles
        # the schedule payload on a full slate and was a common timeout source.
        end = day.isoformat()
        payload = self.http.get_json(
            f"{MLB_API}/schedule", sportId=1, startDate=start, endDate=end,
            hydrate="probablePitcher,venue",
        )
        games: list[Game] = []
        for game_day in payload.get("dates", []):
            for raw in game_day.get("games", []):
                kickoff = raw.get("gameDate")
                if not kickoff or taipei_date(kickoff) != day:
                    continue
                teams = raw.get("teams", {})
                home, away = teams.get("home", {}), teams.get("away", {})
                games.append(Game(
                    event_id=str(raw["gamePk"]), kickoff=kickoff,
                    home=home.get("team", {}).get("name", "主隊"),
                    away=away.get("team", {}).get("name", "客隊"),
                    home_team_id=home.get("team", {}).get("id"),
                    away_team_id=away.get("team", {}).get("id"),
                    home_pitcher=home.get("probablePitcher"),
                    away_pitcher=away.get("probablePitcher"),
                    venue_id=raw.get("venue", {}).get("id"),
                    venue_name=raw.get("venue", {}).get("name", "未取得球場"),
                    status=raw.get("status", {}).get("abstractGameState", "Preview"),
                ))
        return games

    def recent_starting_lineups(self, team_id: int, day: date, games: int = 7) -> list[list[int]]:
        """Use recent official boxscores to create a transparent projected lineup."""
        payload = self.http.get_json(
            f"{MLB_API}/schedule", sportId=1, teamId=team_id,
            startDate=(day - timedelta(days=12)).isoformat(),
            endDate=(day - timedelta(days=1)).isoformat(),
        )
        lineups: list[list[int]] = []
        finals = [g for d in payload.get("dates", []) for g in d.get("games", [])
                  if g.get("status", {}).get("abstractGameState") == "Final"][-games:]
        for raw in finals:
            box = self.http.get_json(f"{MLB_API}/game/{raw['gamePk']}/boxscore")
            side = next((s for s in ("home", "away")
                         if box.get("teams", {}).get(s, {}).get("team", {}).get("id") == team_id), None)
            if not side:
                continue
            players = box.get("teams", {}).get(side, {}).get("players", {})
            starters = []
            for key, player in players.items():
                stats = player.get("stats", {}).get("batting", {})
                order = player.get("battingOrder")
                if order and stats.get("atBats", 0) is not None:
                    starters.append((int(order), int(key.replace("ID", ""))))
            if starters:
                lineups.append([pid for _, pid in sorted(starters)[:9]])
        return lineups

    def bullpen_usage(self, team_id: int, day: date) -> tuple[float, str]:
        payload = self.http.get_json(
            f"{MLB_API}/schedule", sportId=1, teamId=team_id,
            startDate=(day - timedelta(days=3)).isoformat(), endDate=(day - timedelta(days=1)).isoformat(),
        )
        total_ip, yesterday_relief = 0.0, 0
        for d in payload.get("dates", []):
            yesterday = d.get("date") == (day - timedelta(days=1)).isoformat()
            for raw in d.get("games", []):
                if raw.get("status", {}).get("abstractGameState") != "Final":
                    continue
                box = self.http.get_json(f"{MLB_API}/game/{raw['gamePk']}/boxscore")
                side = next((s for s in ("home", "away")
                             if box.get("teams", {}).get(s, {}).get("team", {}).get("id") == team_id), None)
                if not side:
                    continue
                pitchers = box.get("teams", {}).get(side, {}).get("pitchers", [])[1:]
                used = 0
                for pid in pitchers:
                    stat = box.get("teams", {}).get(side, {}).get("players", {}).get(f"ID{pid}", {}).get("stats", {}).get("pitching", {})
                    ip = innings_to_float(stat.get("inningsPitched", "0"))
                    if ip > 0:
                        total_ip += ip
                        used += 1
                if yesterday:
                    yesterday_relief += used
        if total_ip >= 12 or yesterday_relief >= 5:
            return 1.08, f"牛棚偏疲勞：近三日 {total_ip:.1f} IP，昨日 {yesterday_relief} 位後援"
        if total_ip >= 8 or yesterday_relief >= 4:
            return 1.04, f"牛棚略有消耗：近三日 {total_ip:.1f} IP，昨日 {yesterday_relief} 位後援"
        return 1.00, f"牛棚負荷正常：近三日 {total_ip:.1f} IP，昨日 {yesterday_relief} 位後援"

    def venue_coordinates(self, venue_id: Optional[int]) -> Optional[tuple[float, float]]:
        if not venue_id:
            return None
        data = self.http.get_json(f"{MLB_API}/venues/{venue_id}")
        point = (data.get("venues") or [{}])[0].get("location", {}).get("defaultCoordinates", {})
        try:
            return float(point["latitude"]), float(point["longitude"])
        except (KeyError, TypeError, ValueError):
            return None


class SavantClient:
    """Public MLB Statcast source.  Cache responses in the host process daily."""

    def __init__(self, http: HTTP):
        self.http = http
        self._xwoba_by_year: dict[int, dict[int, float]] = {}

    def probable_names(self, day: date) -> set[str]:
        # Savant's public page is deliberately used only for name-level
        # cross-checking; the official MLB schedule remains the canonical ID map.
        text = self.http.get_text(SAVANT_PROBABLES, date=day.isoformat())
        return {normalize_name(name) for name in re.findall(r"<h3[^>]*>\s*([^<]{2,80})\s*</h3>", text)}

    def player_xwoba(self, player_id: int, day: date) -> Optional[float]:
        # Download the public season CSV once, then look up every projected
        # batter locally.  This keeps the value genuinely current and avoids
        # one remote request per player.
        if day.year not in self._xwoba_by_year:
            values: dict[int, float] = {}
            try:
                text = self.http.get_text(
                    "https://baseballsavant.mlb.com/leaderboard/expected_statistics",
                    type="batter", year=day.year, csv="true")
                for row in csv.DictReader(StringIO(text.lstrip("\ufeff"))):
                    try:
                        value = float(row.get("est_woba") or "")
                        if .20 <= value <= .45:
                            values[int(row["player_id"])] = value
                    except (KeyError, TypeError, ValueError):
                        continue
            except Exception:
                pass
            self._xwoba_by_year[day.year] = values
        return self._xwoba_by_year[day.year].get(player_id)


class WeatherClient:
    def __init__(self, http: HTTP):
        self.http = http

    def multiplier(self, coordinates: Optional[tuple[float, float]], kickoff: str) -> tuple[float, str]:
        if not coordinates:
            return 1.0, "球場座標未取得；天氣不調整"
        try:
            lat, lon = coordinates
            data = self.http.get_json(OPEN_METEO, latitude=lat, longitude=lon,
                hourly="temperature_2m,precipitation_probability,wind_speed_10m",
                timezone="UTC", forecast_days=16, past_days=1)
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            target = parse_iso(kickoff).astimezone(timezone.utc)
            index = min(range(len(times)), key=lambda i: abs(parse_iso(times[i] + "+00:00") - target))
            temp = float(hourly["temperature_2m"][index]); rain = float(hourly["precipitation_probability"][index]); wind = float(hourly["wind_speed_10m"][index])
            value = 1.015 if temp >= 30 else .985 if temp <= 10 else 1.0
            value *= .99 if rain >= 60 else 1.0
            value *= 1.01 if wind >= 30 else 1.0
            return min(1.04, max(.96, value)), f"Open-Meteo：{temp:.0f}°C，降雨 {rain:.0f}%，風 {wind:.0f} km/h"
        except Exception:
            return 1.0, "天氣取得失敗；本次不調整"


class MLBPreReleaseService:
    """Taiwan 19:30 MLB calculation orchestrator.

    ``super_quotes`` is keyed by MLB gamePk and supplied by the administrator.
    No odds are fetched or inferred by this module.
    """

    def __init__(self, simulations: int = 30_000, min_ev: float = .03, http: Optional[HTTP] = None):
        self.simulations = simulations
        self.min_ev = min_ev
        http = http or HTTP()
        self.mlb = MLBOfficialClient(http)
        self.savant = SavantClient(http)
        self.weather = WeatherClient(http)

    def run(
        self,
        release_day: date,
        super_quotes: Mapping[str, Iterable[SuperQuote]],
        *,
        first_super_quotes: Optional[Mapping[str, Iterable[SuperQuote]]] = None,
        calibrated_at: Optional[str] = None,
        games: Optional[Iterable[Game]] = None,
    ) -> list[PublishedGame]:
        """Calculate with latest quotes and attach optional first-quote history.

        ``first_super_quotes`` is presentation history only.  It never enters
        the model, settlement, EV, threshold, or recommendation calculation.
        When omitted on a game's first calibration, latest is also its first
        market and the member display reports ``無變化``.
        """
        # Savant is a secondary cross-check. Its temporary unavailability must
        # not discard an otherwise valid official MLB schedule.
        try:
            savant_probables = self.savant.probable_names(release_day)
        except Exception:
            savant_probables = set()
        output = []
        # The automatic runner already fetched the official schedule to match
        # live markets. Reuse that exact list so a second schedule request
        # cannot make a successful run look like a no-games failure.
        scheduled_games = tuple(games) if games is not None else tuple(self.mlb.games_for_taiwan_date(release_day))
        for game in scheduled_games:
            risk = self._risk(game, savant_probables)
            away = self._team_feature(game.away, game.away_team_id, game.home_pitcher, release_day)
            home = self._team_feature(game.home, game.home_team_id, game.away_pitcher, release_day)
            try:
                coordinates = self.mlb.venue_coordinates(game.venue_id)
            except Exception:
                coordinates = None
            weather_multiplier, _ = self.weather.multiplier(coordinates, game.kickoff)
            model = self._model(game, home, away, weather_multiplier)
            calibrated_quotes = tuple(super_quotes.get(game.event_id, ()))
            initial_quotes = tuple(
                (first_super_quotes or super_quotes).get(game.event_id, ()))
            recommendations = self._recommend(model, calibrated_quotes)
            now = datetime.now(TZ_TAIPEI).isoformat()
            output.append(PublishedGame(game, risk, model, tuple(recommendations), (
                SourceStamp("MLB Stats API", now),
                SourceStamp("Baseball Savant probable pitchers", now),
                SourceStamp("Open-Meteo", now),
            ), first_market_quotes=initial_quotes,
               latest_market_quotes=calibrated_quotes,
               calibrated_at=calibrated_at or now,
               settlement_status="pending"))
        return output

    def _risk(self, game: Game, savant_probables: set[str]) -> DataRisk:
        reasons: list[str] = []
        names = [p.get("fullName") for p in (game.home_pitcher, game.away_pitcher) if p]
        if len(names) != 2:
            reasons.append("至少一隊預定先發尚未公布（TBA）")
        elif not all(normalize_name(name) in savant_probables for name in names):
            reasons.append("MLB 官方與 Baseball Savant 的預定先發尚未完成交叉確認")
        # At 19:30 Taiwan an official MLB lineup normally does not exist yet.
        reasons.append("正式打線尚未公布；本模型以近 7 場官方先發推估")
        level = RiskLevel.RED if len(names) != 2 else RiskLevel.YELLOW if reasons else RiskLevel.GREEN
        return DataRisk(level, tuple(reasons), "預定先發" if len(names) == 2 else "TBA／未完整", "推估打線", datetime.now(TZ_TAIPEI).isoformat())

    def _team_feature(self, name: str, team_id: Optional[int], opposing_pitcher: Optional[Mapping[str, Any]], day: date) -> TeamFeature:
        try:
            lineups = self.mlb.recent_starting_lineups(team_id, day) if team_id else []
        except Exception:
            lineups = []
        projected = projected_lineup(lineups)
        hand = "LHP" if opposing_pitcher and opposing_pitcher.get("pitchHand", {}).get("code") == "L" else "RHP"
        xwobas = [self.savant.player_xwoba(pid, day) for pid in projected]
        values = [v for v in xwobas if v is not None and .20 <= v <= .45]
        lineup_xwoba = float(np.mean(values)) if values else .315
        try:
            multiplier, note = self.mlb.bullpen_usage(team_id, day) if team_id else (1.0, "牛棚資料未取得")
        except Exception:
            multiplier, note = 1.0, "牛棚資料暫時無法取得；本次不調整"
        return TeamFeature(name, tuple(projected), lineup_xwoba, multiplier, note, hand)

    def _model(self, game: Game, home: TeamFeature, away: TeamFeature, weather_multiplier: float) -> ModelOutput:
        # Transparent, conservative pre-lineup model.  Team run environments are
        # based on live inferred lineup xwOBA and verified bullpen workload.
        home_lambda = 4.35 * (home.lineup_xwoba / .315) ** 2.0 * away.bullpen_multiplier * weather_multiplier * 1.035
        away_lambda = 4.25 * (away.lineup_xwoba / .315) ** 2.0 * home.bullpen_multiplier * weather_multiplier
        rng = np.random.default_rng(stable_seed(game.event_id, game.kickoff))
        home_runs, away_runs = simulate_finish(rng, self.simulations, home_lambda, away_lambda)
        diff, total = home_runs - away_runs, home_runs + away_runs
        home_p = float(np.mean(diff > 0))
        return ModelOutput(game.event_id, home_lambda, away_lambda, home_p, 1.0 - home_p,
            float(np.mean(total)), -round(float(np.mean(diff)) * 2) / 2, self.simulations)

    def _recommend(self, model: ModelOutput, quotes: Iterable[SuperQuote]) -> list[Recommendation]:
        rng = np.random.default_rng(stable_seed("settlement", model.event_id))
        home, away = simulate_finish(rng, self.simulations, model.home_lambda, model.away_lambda)
        result = []
        for q in quotes:
            decimal = hk_to_decimal(q.price)
            if q.market_type == "moneyline":
                p = float(np.mean(home > away)) if q.side == "home" else float(np.mean(away > home))
                ev = p * decimal - 1
            elif q.market_type == "spread" and q.raw_line:
                margin = home - away if q.side == "home" else away - home
                p, ev = special_spread_ev(margin, q.raw_line, parse_super_line(q.raw_line), decimal)
            elif q.market_type == "total" and q.raw_line:
                p, ev = special_total_ev(home + away, q.side, parse_super_line(q.raw_line), decimal)
            else:
                continue
            ok = ev >= self.min_ev
            result.append(Recommendation(q.market_type, q.side, q.raw_line, decimal, p, 1 / decimal, ev, ok,
                                         "可推薦" if ok else "PASS｜未達 +EV 門檻"))
        return sorted(result, key=lambda r: r.ev, reverse=True)


class MLBAutoSnapshotRunner:
    """Administrator-triggered automatic baseline producer.

    The runner adapts conventional decimal odds into the existing calculation
    entry point.  It does not change the model, +EV threshold, settlement code,
    or the member recommendation schema.
    """

    def __init__(
        self,
        store: MLBReleaseSnapshotStore,
        service: Optional[MLBPreReleaseService] = None,
        odds_client: Optional[TheOddsAPIClient] = None,
    ):
        self.store = store
        self.service = service or MLBPreReleaseService()
        self.odds_client = odds_client or TheOddsAPIClient()

    def run(
        self,
        date_str: str,
        odds_api_key: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        _validate_date_str(date_str)
        current = now or datetime.now(TZ_TAIPEI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=TZ_TAIPEI)
        day = date.fromisoformat(date_str)
        try:
            games = self.service.mlb.games_for_taiwan_date(day)
        except Exception as exc:
            return self._failed_result(
                date_str, _mlb_source_failure(exc, "schedule"),
                f"MLB 即時資料取得失敗：{_safe_admin_error(exc, odds_api_key)}", current)
        if not games:
            return self._failed_result(
                date_str, "no_games", "當日無可用 MLB 賽事", current)
        odds_warning: Optional[str] = None
        try:
            odds_markets = self.odds_client.fetch_mlb_markets(odds_api_key)
        except Exception as exc:
            # The official schedule is still useful and must remain visible.
            # Without a verified market the existing rules naturally produce
            # PASS rows, so no odds, direction, EV, or recommendation is
            # invented here.
            odds_markets = []
            odds_warning = "The Odds API：" + _mlb_failure_label(_mlb_source_failure(exc, "odds")) + "；保留 MLB 官方完整賽表並標示 PASS"
        try:
            decisions = {
                game.event_id: decide_automatic_market(
                    game, _match_odds_market(game, odds_markets), current)
                for game in games
            }
            quotes = {game.event_id: _automatic_quotes(decisions[game.event_id]) for game in games}
            calculated = self.service.run(
                day, quotes, calibrated_at=_as_taipei_iso(current), games=games)
            previous = self.store.get_automatic_snapshot_for_backend(date_str)
            previous_by_event = {
                str(payload.get("event_id")): payload
                for payload in (previous or {}).get("payloads", [])
            }
            payloads = []
            for published in calculated:
                decision = decisions.get(published.game.event_id)
                if decision is None:
                    decision = decide_automatic_market(published.game, None, current)
                payload = _automatic_member_payload(
                    published, decision, previous_by_event.get(published.game.event_id), current)
                if odds_warning:
                    payload["warning"] = "｜".join(
                        value for value in (str(payload.get("warning") or ""), odds_warning) if value
                    )
                payloads.append(payload)
            if not payloads:
                return self._failed_result(
                    date_str, "no_games", "當日無可用 MLB 賽事", current)
            source_times = [decision.odds_updated_at for decision in decisions.values()
                            if decision.odds_updated_at]
        except Exception as exc:
            return self._failed_result(
                date_str, "processing_failed",
                f"MLB 推薦運算失敗：{_safe_admin_error(exc, odds_api_key)}", current)
        try:
            saved = self.store.save_automatic_snapshot(
                date_str, payloads, max(source_times) if source_times else None,
                note="管理員手動建立自動基準盤", now=current)
        except Exception as exc:
            return _admin_snapshot_result(
                "storage_failed", current, 0, False,
                f"快照儲存失敗：{_safe_admin_error(exc, odds_api_key)}")
        result = _admin_snapshot_result(
            "automatic_available", current,
            int(saved.get("game_count", len(payloads))), True, None)
        result["diagnostic_message"] = odds_warning or "MLB 官方賽程與盤口查詢已完成；未匹配場次請查看賽表警語。"
        return result

    def _failed_result(
        self,
        date_str: str,
        status: str,
        message: str,
        current: datetime,
    ) -> dict[str, Any]:
        note = message
        try:
            if status == "no_games":
                saved = self.store.mark_automatic_no_games(date_str, note, now=current)
            else:
                saved = self.store.mark_automatic_snapshot_failed(
                    date_str, note, now=current)
        except Exception as exc:
            return _admin_snapshot_result(
                "storage_failed", current, 0, False,
                f"快照狀態儲存失敗：{_safe_admin_error(exc, '')}")
        return _admin_snapshot_result(
            status, current, 0,
            bool(saved.get("retained_previous_snapshot", False)), message)


def _admin_snapshot_result(
    status: str,
    now: datetime,
    game_count: int,
    member_available: bool,
    error: Optional[str],
) -> dict[str, Any]:
    """Safe summary for an admin UI; never contains payload or database rows."""
    result: dict[str, Any] = {
        "status": status,
        "updated_at": _as_taipei_iso(now),
        "game_count": int(game_count),
        "snapshot_kind": "automatic_baseline" if member_available else None,
        "member_available": bool(member_available),
    }
    if error:
        result["error"] = error
    return result


def _safe_admin_error(exc: Exception, odds_api_key: str) -> str:
    """Return an understandable error without credentials or raw payloads."""
    message = str(exc).strip() or exc.__class__.__name__
    if odds_api_key:
        message = message.replace(str(odds_api_key), "[REDACTED]")
    message = re.sub(r"(?i)(api(?:_|-)?key=)[^&\s]+", r"\1[REDACTED]", message)
    return message[:300]


def _mlb_source_failure(exc, source):
    code = getattr(exc, "code", None)
    if code in (401, 403): return source + "_auth_failed"
    if code == 429: return source + "_quota_failed"
    if isinstance(exc, (ValueError, KeyError)): return source + "_format_failed"
    return source + "_connection_failed"


def _mlb_failure_label(code):
    return {"auth_failed": "認證或權限失敗，請檢查金鑰與方案",
            "quota_failed": "額度或速率受限，請檢查帳戶用量",
            "format_failed": "回應格式不符"}.get(code.split("_", 1)[1], "連線、逾時或 TLS 失敗")


def _match_odds_market(game: Game, markets: Iterable[OddsMarket]) -> Optional[OddsMarket]:
    candidates = [market for market in markets
                  if normalize_name(market.home_team) == normalize_name(game.home)
                  and normalize_name(market.away_team) == normalize_name(game.away)]
    if not candidates:
        return None
    kickoff = parse_iso(game.kickoff)
    return min(candidates, key=lambda market: abs(parse_iso(market.commence_time) - kickoff))


def _automatic_quotes(decision: AutoMarketDecision) -> tuple[SuperQuote, ...]:
    """Build conventional quotes only; no SUPER ratio syntax is introduced."""
    rows: list[SuperQuote] = []
    for side, decimal in decision.moneyline_prices.items():
        if side in {"home", "away"} and decimal > 1:
            rows.append(SuperQuote("moneyline", side, None, decimal - 1))
    for side, decimal in decision.spread_prices.items():
        if decimal > 1:
            line = "-1.5" if side == decision.favorite_side else "+1.5"
            rows.append(SuperQuote("spread", side, line, decimal - 1))
    if decision.total_line is not None:
        line = f"{decision.total_line:g}"
        for side, decimal in decision.total_prices.items():
            if side in {"over", "under"} and decimal > 1:
                rows.append(SuperQuote("total", side, line, decimal - 1))
    return tuple(rows)


def _automatic_member_payload(
    published: PublishedGame,
    decision: AutoMarketDecision,
    previous: Optional[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    payload = published.as_member_payload()
    latest_market = _automatic_market_display(published.game, decision)
    first_market = str(previous.get("first_market")) if previous and previous.get("first_market") else latest_market
    payload.update({
        "snapshot_kind": "automatic_baseline",
        "calibration_state": "尚未人工 SUPER 校正",
        "favored_team": decision.favored_team,
        "underdog_team": decision.underdog_team,
        "favorite_side": decision.favorite_side,
        "baseline_line": decision.baseline_line,
        "direction_source": decision.direction_source,
        "first_market": first_market,
        "latest_market": latest_market,
        "market_change": "無變化" if first_market == latest_market else f"{first_market} → {latest_market}",
        "updated_at": _as_taipei_iso(now),
        "calibrated_at": None,
        "settlement_status": "pending",
    })
    warning_values = [str(payload.get("warning") or ""), "尚未人工 SUPER 校正"]
    warning_values.extend(decision.warnings)
    payload["warning"] = "｜".join(value for value in warning_values if value)
    odds_stamp = decision.odds_updated_at or "未取得"
    payload["sources"] = f"{payload['sources']}；The Odds API（{odds_stamp}）"
    payload["sources_display"] = payload["sources"]
    payload["recommendations"] = [
        _automatic_recommendation_display(row, published.game)
        for row in payload.get("recommendations", [])
    ]
    return payload


def _automatic_market_display(game: Game, decision: AutoMarketDecision) -> str:
    parts = []
    if decision.favored_team and decision.underdog_team:
        parts.append(
            f"自動基準盤：{decision.favored_team} -1.5／{decision.underdog_team} +1.5")
    else:
        parts.append("讓分方向待人工校正")
    if decision.total_line is not None:
        parts.append(
            f"The Odds API 大小 {decision.total_line:g}（{decision.odds_updated_at}）")
    else:
        parts.append("The Odds API 大小分中心盤未提供")
    return "；".join(parts)


def _automatic_recommendation_display(
    recommendation: Mapping[str, Any],
    game: Game,
) -> dict[str, Any]:
    row = dict(recommendation)
    side = str(row.get("side") or "")
    market_type = str(row.get("market_type") or "")
    raw_line = row.get("raw_line")
    team_or_total = {"home": game.home, "away": game.away,
                     "over": "大分", "under": "小分"}.get(side, side)
    if market_type == "moneyline":
        selection = f"{team_or_total}獨贏"
    elif market_type == "spread":
        selection = f"{team_or_total}讓分 {raw_line}"
    else:
        selection = f"{team_or_total} {raw_line}"
    decimal = float(row.get("decimal_price") or 0)
    display = f"{selection}｜The Odds API 十進位 {decimal:.3f}".rstrip("0").rstrip(".")
    row["display"] = display
    row["selection"] = display
    return row


def run_mlb_auto_snapshot(
    date_str: str,
    odds_api_key: str,
    now: Optional[datetime] = None,
    *,
    is_admin: bool = False,
) -> dict[str, Any]:
    """Administrator button entry point; never call from a member route."""
    if is_admin is not True:
        raise PermissionError("僅管理員可建立 MLB 自動快照")
    store = MLBReleaseSnapshotStore(DEFAULT_RELEASE_DB_PATH)
    return MLBAutoSnapshotRunner(store).run(date_str, odds_api_key, now)


def parse_super_line(raw: str) -> list[tuple[float, float]]:
    """Return original settlement legs; never settle a special line at its mean."""
    text = str(raw).strip().replace(" ", "")
    if text.upper() == "PK" or text.endswith("平"):
        return [(0.0, 1.0)]
    m = re.fullmatch(r"[+-]?(\d+(?:\.\d+)?)([+-])(\d{1,2})", text)
    if not m:
        return [(abs(float(text)), 1.0)]
    number, operator, ratio_text = float(m.group(1)), m.group(2), int(m.group(3))
    ratio = ratio_text / 100
    if not 0 < ratio < 1:
        raise ValueError("SUPER 比例必須介於 1 至 99")
    return [(number - .5, ratio), (number, 1 - ratio)] if operator == "+" else [(number, 1 - ratio), (number + .5, ratio)]


def special_spread_ev(margin: np.ndarray, raw_line: str, legs: list[tuple[float, float]], decimal: float) -> tuple[float, float]:
    """Settle the chosen side's own signed original SUPER line.

    ``margin`` is already chosen-team score minus opponent score.  Thus -1+65
    receives -0.5/-1.0 legs while +1-50 receives +1.0/+1.5 legs.
    """
    direction = -1 if str(raw_line).strip().startswith("-") else 1
    probabilities, evs = [], []
    for line, weight in legs:
        p, ev = asian_ev(margin, direction * line, decimal)
        probabilities.append(weight * p); evs.append(weight * ev)
    return float(sum(probabilities)), float(sum(evs))


def special_total_ev(total: np.ndarray, side: str, legs: list[tuple[float, float]], decimal: float) -> tuple[float, float]:
    values = total if side == "over" else -total
    sign = -1 if side == "over" else 1
    probabilities, evs = [], []
    for line, weight in legs:
        p, ev = asian_ev(values, sign * line, decimal)
        probabilities.append(weight * p); evs.append(weight * ev)
    return float(sum(probabilities)), float(sum(evs))


def asian_ev(values: np.ndarray, handicap: float, decimal: float) -> tuple[float, float]:
    parts = [handicap] if math.isclose(handicap * 4, round(handicap * 4)) and round(handicap * 4) % 2 == 0 else [math.floor(handicap * 2) / 2, math.ceil(handicap * 2) / 2]
    settle = np.mean([np.sign(values + part) for part in parts], axis=0)
    profit = np.where(settle > 0, decimal - 1, np.where(settle < 0, -1, 0))
    return float(np.mean(settle > 0)), float(np.mean(profit))


def simulate_finish(rng: np.random.Generator, size: int, home_mean: float, away_mean: float) -> tuple[np.ndarray, np.ndarray]:
    # Negative-binomial scoring retains the existing MLB module's over-dispersion.
    home = rng.negative_binomial(max(home_mean / .35, .01), 1 / 1.35, size)
    away = rng.negative_binomial(max(away_mean / .35, .01), 1 / 1.35, size)
    tied = home == away
    for _ in range(50):
        if not tied.any():
            break
        home[tied] += rng.poisson(max(home_mean / 9, .1), tied.sum())
        away[tied] += rng.poisson(max(away_mean / 9, .1), tied.sum())
        tied = home == away
    home[tied] += 1  # deterministic final tie-breaker only for non-converged paths
    return home.astype(int), away.astype(int)


def projected_lineup(lineups: list[list[int]]) -> list[int]:
    if not lineups:
        return []
    counts: dict[int, int] = {}
    for lineup in lineups:
        for player in lineup:
            counts[player] = counts.get(player, 0) + 1
    return [p for p, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:9]]


def hk_to_decimal(value: float) -> float:
    value = float(value)
    if value <= 0:
        raise ValueError("SUPER 香港盤水位必須大於 0")
    return value + 1.0


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def taipei_date(value: str) -> date:
    return parse_iso(value).astimezone(TZ_TAIPEI).date()


def innings_to_float(value: Any) -> float:
    whole, _, outs = str(value).partition(".")
    return float(whole) + (float(outs[:1]) / 3 if outs else 0)


def normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value).lower())


def stable_seed(*parts: Any) -> int:
    return abs(hash("|".join(map(str, parts)))) % (2**32)


if __name__ == "__main__":
    # Example: create an empty quote file first, then run this at 19:20 Taiwan.
    # Quotes format: {"gamePk": [{"market_type":"total","side":"over","raw_line":"9+90","price":0.94}]}
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat(), help="Taiwan date (YYYY-MM-DD)")
    parser.add_argument("--quotes", required=True, help="Path to administrator-verified SUPER quotes JSON")
    parser.add_argument("--output", default="mlb_release.json")
    args = parser.parse_args()
    with open(args.quotes, encoding="utf-8") as fh:
        raw_quotes = json.load(fh)
    quotes = {event: [SuperQuote(**q) for q in rows] for event, rows in raw_quotes.items()}
    report = MLBPreReleaseService().run(date.fromisoformat(args.date), quotes)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump([g.as_member_payload() for g in report], fh, ensure_ascii=False, indent=2)
