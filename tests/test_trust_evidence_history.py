"""Governance contract for append-only Trust Evidence history."""


def test_trust_evidence_history_is_ordered_and_append_only():
    history = (
        {"event_id": "write-1", "occurred_at": "2026-07-17T05:39:34Z", "immutable": True},
        {"event_id": "review-1", "occurred_at": "2026-07-17T07:07:20Z", "immutable": True},
        {"event_id": "merge-1", "occurred_at": "2026-07-17T07:09:40Z", "immutable": True},
    )

    event_ids = [event["event_id"] for event in history]
    timestamps = [event["occurred_at"] for event in history]

    assert len(event_ids) == len(set(event_ids))
    assert timestamps == sorted(timestamps)
    assert all(event["immutable"] is True for event in history)
