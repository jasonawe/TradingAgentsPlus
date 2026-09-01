"""Per-stage artifact persistence for analysis runs.

Artifacts are partial or completed stage outputs (analyst report,
research debate, risk assessment, trader plan, portfolio decision,
executive summary). They are upserted idempotently as chunks arrive so
a failed run still exposes the analyst reports that already finished.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .storage import SQLiteStore

VALID_ARTIFACT_TYPES = frozenset(
    {
        "analyst_report",
        "research_debate",
        "risk_assessment",
        "trader_plan",
        "portfolio_decision",
        "executive_summary",
    }
)

VALID_ARTIFACT_STATUSES = frozenset({"partial", "completed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_key: str
    artifact_type: str
    phase: str
    agent: str | None
    title: str
    content_markdown: str
    status: str
    sequence: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_key": self.artifact_key,
            "artifact_type": self.artifact_type,
            "phase": self.phase,
            "agent": self.agent,
            "title": self.title,
            "content_markdown": self.content_markdown,
            "status": self.status,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _decode_row(row: sqlite3.Row | None) -> ArtifactRecord | None:
    if row is None:
        return None
    row.keys()
    return ArtifactRecord(
        artifact_key=row["artifact_key"],
        artifact_type=row["artifact_type"],
        phase=row["phase"],
        agent=row["agent"],
        title=row["title"],
        content_markdown=row["content_markdown"],
        status=row["status"],
        sequence=int(row["sequence"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ArtifactRepository:
    """SQLite-backed upsert + read API for ``analysis_run_artifacts``."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def upsert(
        self,
        run_id: str,
        *,
        artifact_key: str,
        artifact_type: str,
        phase: str,
        title: str,
        content_markdown: str,
        status: str,
        sequence: int,
        agent: str | None = None,
    ) -> ArtifactRecord:
        if artifact_type not in VALID_ARTIFACT_TYPES:
            raise ValueError(f"invalid artifact_type: {artifact_type}")
        if status not in VALID_ARTIFACT_STATUSES:
            raise ValueError(f"invalid artifact status: {status}")
        now = _now_iso()
        with self.store.connection() as conn:
            existing = conn.execute(
                "SELECT created_at FROM analysis_run_artifacts WHERE run_id=? AND artifact_key=?",
                (run_id, artifact_key),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else now
            conn.execute(
                """
                INSERT INTO analysis_run_artifacts (
                    run_id, artifact_key, artifact_type, phase, agent,
                    title, content_markdown, status, sequence,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, artifact_key) DO UPDATE SET
                    artifact_type=excluded.artifact_type,
                    phase=excluded.phase,
                    agent=excluded.agent,
                    title=excluded.title,
                    content_markdown=excluded.content_markdown,
                    status=excluded.status,
                    sequence=excluded.sequence,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    artifact_key,
                    artifact_type,
                    phase,
                    agent,
                    title,
                    content_markdown,
                    status,
                    int(sequence),
                    created_at,
                    now,
                ),
            )
        return ArtifactRecord(
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            phase=phase,
            agent=agent,
            title=title,
            content_markdown=content_markdown,
            status=status,
            sequence=int(sequence),
            created_at=created_at,
            updated_at=now,
        )

    def list_for_run(self, run_id: str) -> list[ArtifactRecord]:
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_run_artifacts WHERE run_id=? ORDER BY sequence, artifact_key",
                (run_id,),
            ).fetchall()
        return [record for record in (_decode_row(row) for row in rows) if record is not None]

    def counts(self, run_id: str) -> dict[str, int]:
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM analysis_run_artifacts WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        completed = 0
        partial = 0
        for row in rows:
            status = row["status"]
            count = int(row["n"])
            if status == "completed":
                completed = count
            elif status == "partial":
                partial = count
        return {
            "artifact_count": completed + partial,
            "completed_artifact_count": completed,
            "partial_artifact_count": partial,
        }


__all__ = [
    "ArtifactRecord",
    "ArtifactRepository",
    "VALID_ARTIFACT_STATUSES",
    "VALID_ARTIFACT_TYPES",
]
