"""Application composition only; this module contains no Streamlit UI or sport rules."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from football_module import FootballModule
from member_release_service import MemberReleaseService
from mlb_pre_release_module import MLBReleaseSnapshotStore


@dataclass(frozen=True)
class AppServices:
    mlb_store: MLBReleaseSnapshotStore
    football: FootballModule
    members: MemberReleaseService
    seasons: Mapping[str, int]


def compose_services() -> AppServices:
    data_dir = Path(os.environ.get("APP_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    mlb_store = MLBReleaseSnapshotStore(str(data_dir / "mlb_release.sqlite3"))
    football = FootballModule.from_environment(str(data_dir / "football.sqlite3"))
    try:
        seasons = _football_seasons()
    except RuntimeError:
        seasons = {}
    return AppServices(mlb_store, football, MemberReleaseService(mlb_store, football), seasons)


def _football_seasons() -> Mapping[str, int]:
    raw = os.environ.get("FOOTBALL_SEASONS_JSON", "")
    if not raw:
        raise RuntimeError("FOOTBALL_SEASONS_JSON 必須設定，例如 {\"eng.1\": 2026, \"esp.1\": 2026}")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value:
            raise ValueError
        return {str(key): int(year) for key, year in value.items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("FOOTBALL_SEASONS_JSON 必須是非空的 league-to-season JSON object") from exc
