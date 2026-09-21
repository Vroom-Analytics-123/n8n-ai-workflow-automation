"""File-backed dead-letter queue (DLQ).

The pattern the shipped n8n workflows implement with Code nodes: when a
record fails every retry, park it with its payload, the error, and where it
came from — instead of silently dropping it. A separate reprocessor
workflow (see ``workflows/dead-letter-reprocessor.json``) picks parked
items back up.

Lifecycle of an entry: ``pending`` -> ``requeue`` (another attempt) ->
``done`` (it worked) or ``quarantined`` (it failed ``max_requeues`` times
and is now poison — a human looks at it). Entries are stored as JSON on
disk so they survive restarts; there is no network and no external service.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DlqEntry:
    """One parked failure."""

    id: str
    payload: dict[str, Any]
    error: str
    source: str
    attempts: int = 0
    status: str = "pending"  # pending | quarantined | done
    first_failed_at: str = field(default_factory=_now_iso)
    last_failed_at: str = field(default_factory=_now_iso)


class DeadLetterQueue:
    """Append / list / requeue / discard failed items, persisted as JSON.

    Args:
        path: File the queue is stored in (created on first write).
        max_requeues: How many times an entry may be requeued before it is
            quarantined as poison. Must be >= 0.
    """

    def __init__(self, path: str | Path, max_requeues: int = 3) -> None:
        if max_requeues < 0:
            raise ValueError("max_requeues must be >= 0")
        self.path = Path(path)
        self.max_requeues = max_requeues

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text()).get("entries", {})

    def _save(self, entries: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": entries}, indent=2))

    # -- API ---------------------------------------------------------------

    def append(self, payload: dict[str, Any], error: str, source: str) -> str:
        """Park a failed item. Returns the new entry's id."""
        entries = self._load()
        entry_id = uuid.uuid4().hex[:12]
        entry = DlqEntry(
            id=entry_id, payload=payload, error=str(error), source=source
        )
        entries[entry_id] = asdict(entry)
        self._save(entries)
        return entry_id

    def get(self, entry_id: str) -> DlqEntry | None:
        """Fetch an entry by id, or ``None`` if it does not exist."""
        raw = self._load().get(entry_id)
        return DlqEntry(**raw) if raw is not None else None

    def pending(self) -> list[DlqEntry]:
        """Pending entries, oldest failure first."""
        entries = [
            DlqEntry(**raw)
            for raw in self._load().values()
            if raw.get("status") == "pending"
        ]
        return sorted(entries, key=lambda e: e.first_failed_at)

    def requeue(self, entry_id: str) -> DlqEntry | None:
        """Mark an entry for another attempt.

        Returns the updated entry while it still has requeues left, or
        ``None`` if the entry hit the requeue limit and was quarantined
        instead. Quarantined entries stay quarantined — they never silently
        re-enter the pending queue.
        """
        entries = self._load()
        raw = entries.get(entry_id)
        if raw is None or raw.get("status") != "pending":
            return None
        raw["attempts"] += 1
        raw["last_failed_at"] = _now_iso()
        if raw["attempts"] > self.max_requeues:
            raw["status"] = "quarantined"
            self._save(entries)
            return None
        self._save(entries)
        return DlqEntry(**raw)

    def discard(self, entry_id: str) -> bool:
        """Delete an entry permanently. Returns ``False`` if unknown."""
        entries = self._load()
        if entry_id not in entries:
            return False
        del entries[entry_id]
        self._save(entries)
        return True

    def mark_done(self, entry_id: str) -> bool:
        """Record that a requeued entry finally succeeded.

        The record is kept for audit but leaves the pending queue.
        Returns ``False`` if the id is unknown.
        """
        entries = self._load()
        raw = entries.get(entry_id)
        if raw is None:
            return False
        raw["status"] = "done"
        raw["last_failed_at"] = _now_iso()
        self._save(entries)
        return True

    def __len__(self) -> int:
        return len(self.pending())
