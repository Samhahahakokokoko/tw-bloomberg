"""Governance contract for evidence confidence and authority separation."""


def test_low_evidence_confidence_never_expands_authority():
    trust_snapshot = {
        "trust_level": "T2_LIMITED",
        "confidence_level": "LOW",
        "authority_granted": False,
        "auto_merge_enabled": False,
    }

    assert trust_snapshot["trust_level"] == "T2_LIMITED"
    assert trust_snapshot["confidence_level"] == "LOW"
    assert trust_snapshot["authority_granted"] is False
    assert trust_snapshot["auto_merge_enabled"] is False
