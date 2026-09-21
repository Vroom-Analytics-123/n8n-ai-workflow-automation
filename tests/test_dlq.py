"""Tests for n8n_reliability.dlq.DeadLetterQueue.

The dead-letter queue is the pattern the shipped workflows implement in n8n
(Code nodes writing failed items aside instead of dropping them). The Python
version here is file-backed and fixture-friendly so the requeue/quarantine
semantics are testable without n8n. No network anywhere.
"""

import json

import pytest

from n8n_reliability.dlq import DeadLetterQueue, DlqEntry


@pytest.fixture
def dlq(tmp_path):
    return DeadLetterQueue(tmp_path / "dead-letters.json")


def test_append_returns_id_and_stores_entry(dlq):
    entry_id = dlq.append(
        {"email": "lead@example.com"}, "CRM timeout", "lead-intake-pipeline"
    )

    assert isinstance(entry_id, str) and entry_id
    entry = dlq.get(entry_id)
    assert isinstance(entry, DlqEntry)
    assert entry.payload == {"email": "lead@example.com"}
    assert entry.error == "CRM timeout"
    assert entry.source == "lead-intake-pipeline"
    assert entry.attempts == 0
    assert entry.status == "pending"


def test_entry_captures_timestamps(dlq):
    entry_id = dlq.append({"x": 1}, "boom", "wf")
    entry = dlq.get(entry_id)
    assert entry.first_failed_at  # non-empty ISO timestamp
    assert entry.last_failed_at
    assert entry.first_failed_at <= entry.last_failed_at


def test_pending_lists_entries_fifo(dlq):
    first = dlq.append({"n": 1}, "e1", "wf")
    second = dlq.append({"n": 2}, "e2", "wf")
    third = dlq.append({"n": 3}, "e3", "wf")

    pending = dlq.pending()
    assert [e.id for e in pending] == [first, second, third]


def test_len_counts_pending_only(dlq):
    a = dlq.append({"n": 1}, "e", "wf")
    b = dlq.append({"n": 2}, "e", "wf")
    assert len(dlq) == 2
    dlq.discard(a)
    assert len(dlq) == 1
    dlq.mark_done(b)
    assert len(dlq) == 0


def test_requeue_increments_attempts_and_stays_pending(dlq):
    entry_id = dlq.append({"n": 1}, "transient", "wf")

    entry = dlq.requeue(entry_id)

    assert entry is not None
    assert entry.attempts == 1
    assert entry.status == "pending"
    assert len(dlq) == 1  # still in the queue, ready for the next run


def test_requeue_beyond_limit_quarantines_and_returns_none(dlq):
    dlq_q = DeadLetterQueue(dlq.path, max_requeues=2)
    entry_id = dlq_q.append({"n": 1}, "poison", "wf")

    assert dlq_q.requeue(entry_id) is not None  # attempt 1
    assert dlq_q.requeue(entry_id) is not None  # attempt 2

    result = dlq_q.requeue(entry_id)  # attempt 3 > max_requeues
    assert result is None

    entry = dlq_q.get(entry_id)
    assert entry.status == "quarantined"
    assert len(dlq_q) == 0  # quarantined items leave the pending queue


def test_poison_message_never_requeued_again(dlq):
    q = DeadLetterQueue(dlq.path, max_requeues=1)
    entry_id = q.append({"n": 1}, "always fails", "wf")
    q.requeue(entry_id)
    assert q.requeue(entry_id) is None
    # A second call stays quarantined — it must not silently re-enter pending.
    assert q.requeue(entry_id) is None
    assert q.get(entry_id).status == "quarantined"


def test_requeue_unknown_id_returns_none(dlq):
    assert dlq.requeue("does-not-exist") is None


def test_discard_removes_entry(dlq):
    entry_id = dlq.append({"n": 1}, "e", "wf")
    assert dlq.discard(entry_id) is True
    assert dlq.get(entry_id) is None
    assert dlq.pending() == []


def test_discard_unknown_id_returns_false(dlq):
    assert dlq.discard("does-not-exist") is False


def test_mark_done_removes_from_pending(dlq):
    entry_id = dlq.append({"n": 1}, "e", "wf")
    assert dlq.mark_done(entry_id) is True
    assert dlq.pending() == []
    # The record is kept for audit, just no longer pending.
    assert dlq.get(entry_id).status == "done"


def test_mark_done_unknown_id_returns_false(dlq):
    assert dlq.mark_done("does-not-exist") is False


def test_entries_survive_reload(tmp_path):
    path = tmp_path / "dlq.json"
    first = DeadLetterQueue(path)
    entry_id = first.append({"lead": "a@b.com"}, "timeout", "intake")

    second = DeadLetterQueue(path)  # new instance, same file
    entry = second.get(entry_id)
    assert entry is not None
    assert entry.payload == {"lead": "a@b.com"}
    assert len(second) == 1


def test_store_file_is_valid_json(tmp_path):
    path = tmp_path / "dlq.json"
    q = DeadLetterQueue(path)
    q.append({"n": 1}, "e", "wf")

    raw = json.loads(path.read_text())
    assert "entries" in raw
    assert len(raw["entries"]) == 1
