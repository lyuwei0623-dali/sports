"""Core-owned, dependency-injected member release boundary.

Member routes/pages construct one ``MemberReleaseService`` during application
composition and call only its public view methods. The service accepts saved
snapshot providers; it has no imports or hooks for sport-side operations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

try:
    from integration_adapter import MemberReleaseGate, ReleaseStatus, decorate_snapshot_report, football_rows_to_shared_report, member_release_gate, mlb_payloads_to_shared_report
    from core_shared_ui import render_shared_report
except ModuleNotFoundError:  # pragma: no cover - package import path
    from .integration_adapter import MemberReleaseGate, ReleaseStatus, decorate_snapshot_report, football_rows_to_shared_report, member_release_gate, mlb_payloads_to_shared_report
    from .core_shared_ui import render_shared_report


MEMBER_SNAPSHOT_PENDING_MESSAGE = "系統正在更新賽事運算賽事推薦分析"


class MemberReleaseService:
    """Read-only member entry point, composed with public snapshot providers."""

    def __init__(self, mlb_snapshot_store: Any, football_module: Any) -> None:
        self._mlb_snapshot_store = mlb_snapshot_store
        self._football_module = football_module

    def get_mlb_member_view(self, date_str: str, now: Optional[datetime] = None) -> MemberReleaseGate:
        snapshot = _safe_snapshot_from(self._mlb_snapshot_store, date_str)
        if not _has_saved_content(snapshot, "payloads"):
            return _no_snapshot_gate(snapshot)
        payloads = snapshot.get("payloads")
        report = _with_snapshot_provenance(mlb_payloads_to_shared_report(payloads), snapshot)
        return member_release_gate(snapshot.get("release_status", "failed"), report, now=now)

    def get_football_member_view(self, date_str: str, now: Optional[datetime] = None) -> MemberReleaseGate:
        snapshot = _safe_snapshot_from(self._football_module, date_str)
        if not _has_saved_content(snapshot, "rows"):
            return _no_snapshot_gate(snapshot)
        report = _football_report_from_snapshot(snapshot)
        return member_release_gate(snapshot.get("release_status", "failed"), report, now=now)

    def get_mlb_admin_preview(self, date_str: str) -> MemberReleaseGate:
        """Read the saved MLB snapshot for an administrator preview."""
        snapshot = _safe_snapshot_from(self._mlb_snapshot_store, date_str)
        if not _has_saved_content(snapshot, "payloads"):
            return _no_snapshot_gate(snapshot)
        report = _with_snapshot_provenance(
            mlb_payloads_to_shared_report(snapshot.get("payloads")), snapshot)
        return _admin_preview_gate(snapshot, report)

    def get_football_admin_preview(self, date_str: str) -> MemberReleaseGate:
        """Read the saved Football snapshot for an administrator preview."""
        snapshot = _safe_snapshot_from(self._football_module, date_str)
        if not _has_saved_content(snapshot, "rows"):
            return _no_snapshot_gate(snapshot)
        return _admin_preview_gate(snapshot, _football_report_from_snapshot(snapshot))

    def get_member_view(self, sport: str, date_str: str, now: Optional[datetime] = None) -> MemberReleaseGate:
        """One Core UI dispatcher for the selected sport and date."""

        normalised = sport.strip().lower()
        if normalised in {"mlb", "baseball"}:
            return self.get_mlb_member_view(date_str, now)
        if normalised in {"football", "soccer", "足球"}:
            return self.get_football_member_view(date_str, now)
        return _no_snapshot_gate(None)


def render_member_view(gate: MemberReleaseGate) -> str:
    """Render either a Core state message or an allowed stored report, never both."""

    return render_shared_report(gate.report) if gate.allowed and gate.report is not None else gate.message


def _safe_snapshot_from(provider: Any, date_str: str) -> Optional[Mapping[str, Any]]:
    """Translate an absent/unreadable saved snapshot to a safe member state."""

    try:
        snapshot = provider.get_member_snapshot(date_str)
    except Exception:
        return None
    return snapshot if isinstance(snapshot, Mapping) else None


def _has_saved_content(snapshot: Optional[Mapping[str, Any]], key: str) -> bool:
    return bool(snapshot and snapshot.get(key))


def _no_snapshot_gate(snapshot: Optional[Mapping[str, Any]]) -> MemberReleaseGate:
    raw_status = (snapshot or {}).get("release_status", ReleaseStatus.FETCHING.value)
    try:
        status = ReleaseStatus(raw_status)
    except ValueError:
        status = ReleaseStatus.FETCHING
    return MemberReleaseGate(False, status, MEMBER_SNAPSHOT_PENDING_MESSAGE)


def _with_snapshot_provenance(report: Any, metadata: Mapping[str, Any]) -> Any:
    return decorate_snapshot_report(
        report,
        snapshot_kind=metadata.get("snapshot_kind"),
        calibration_state=metadata.get("calibration_state"),
        updated_at=metadata.get("updated_at"),
        stale_warning=metadata.get("stale_warning"),
    )


def _football_report_from_snapshot(snapshot: Mapping[str, Any]) -> Any:
    # Football stores Core-facing provenance at the outer snapshot level,
    # while sport-specific source details live under ``run_metadata``.
    metadata = dict(snapshot.get("run_metadata") or {})
    for key in ("snapshot_kind", "calibration_state", "updated_at", "stale_warning"):
        if snapshot.get(key) is not None:
            metadata[key] = snapshot[key]
    report = football_rows_to_shared_report(snapshot.get("rows"), metadata)
    return _with_snapshot_provenance(report, metadata)


def _admin_preview_gate(snapshot: Mapping[str, Any], report: Any) -> MemberReleaseGate:
    """Allow a protected admin preview without changing member release rules."""
    raw_status = snapshot.get("release_status", ReleaseStatus.AUTOMATIC_AVAILABLE.value)
    try:
        status = ReleaseStatus(raw_status)
    except ValueError:
        status = ReleaseStatus.AUTOMATIC_AVAILABLE
    return MemberReleaseGate(True, status, "後台預覽已保存快照。", report)
