"""The shared assumption registry: record, queries and doctor formatting.

Shared by the sibling servers along with registry.py; only the package name
in the import below differs between repos. Change it in mcp-server-shared, not
in a copy, then sync.
"""

from __future__ import annotations

import pytest

from __PKG__.registry import (
    Assumption,
    by_id,
    contradicted,
    doctor_lines,
    doctor_summary,
    hardware_verified,
    unverified,
)

SAMPLE = (
    Assumption("A-ONE", "First claim", "lib source", "high", "simulator-only"),
    Assumption("A-TWO", "Second claim", "inferred", "medium", "hardware-verified", "seen on hardware"),
    Assumption("A-THREE", "Third claim", "guess", "low", "hardware-contradicted", "behaved differently"),
)


def test_by_id_indexes_every_entry():
    indexed = by_id(SAMPLE)
    assert set(indexed) == {"A-ONE", "A-TWO", "A-THREE"}
    assert indexed["A-ONE"].claim == "First claim"


def test_by_id_rejects_duplicates():
    dup = (
        Assumption("A-ONE", "First", "source", "high"),
        Assumption("A-ONE", "Duplicate", "source", "low"),
    )
    with pytest.raises(ValueError, match="duplicate assumption id: 'A-ONE'"):
        by_id(dup)


def test_unverified_filters_to_simulator_only():
    assert [a.id for a in unverified(SAMPLE)] == ["A-ONE"]


def test_contradicted_filters_to_hardware_contradicted():
    assert [a.id for a in contradicted(SAMPLE)] == ["A-THREE"]


def test_hardware_verified_filters_to_hardware_verified():
    assert [a.id for a in hardware_verified(SAMPLE)] == ["A-TWO"]


def test_doctor_lines_lists_contradicted_then_unverified():
    lines = doctor_lines(unverified(SAMPLE), contradicted(SAMPLE))
    text = "\n".join(lines)
    assert "Contradicted on hardware" in text
    assert "A-THREE: Third claim" in text
    assert "saw: behaved differently" in text
    assert "Not yet confirmed on hardware (1):" in text
    assert "A-ONE: First claim" in text


def test_doctor_lines_verbose_adds_confidence_and_source():
    lines = doctor_lines(unverified(SAMPLE), contradicted(SAMPLE), verbose=True)
    text = "\n".join(lines)
    assert "[high confidence]" in text
    assert "from: lib source" in text


def test_doctor_lines_empty_when_all_verified():
    assert doctor_lines([], []) == []


def test_doctor_summary_counts_and_lists():
    summary = doctor_summary(SAMPLE)
    assert summary["total"] == 3
    assert summary["unverified_count"] == 1
    assert summary["contradicted_count"] == 1
    assert summary["verified_count"] == 1
    assert [a["id"] for a in summary["unverified"]] == ["A-ONE"]
    assert [a["id"] for a in summary["contradicted"]] == ["A-THREE"]
    assert [a["id"] for a in summary["verified"]] == ["A-TWO"]


def test_typo_in_status_fails_fast():
    with pytest.raises(ValueError, match="status 'hardware-verfied' is not one of"):
        Assumption("A-BAD", "Claim", "source", "high", status="hardware-verfied")  # type: ignore[arg-type]


def test_typo_in_confidence_fails_fast():
    with pytest.raises(ValueError, match="confidence 'hi' is not one of"):
        Assumption("A-BAD", "Claim", "source", "hi")  # type: ignore[arg-type]


def test_every_status_is_accepted():
    for status in ("simulator-only", "hardware-verified", "hardware-contradicted"):
        a = Assumption("A-OK", "Claim", "source", "high", status=status, note="seen on hardware")
        assert a.status == status


def test_empty_id_claim_or_source_rejected():
    with pytest.raises(ValueError, match="id, claim and source must be non-empty"):
        Assumption("", "Claim", "Source", "high")
    with pytest.raises(ValueError, match="id, claim and source must be non-empty"):
        Assumption("A-ID", "", "Source", "high")
    with pytest.raises(ValueError, match="id, claim and source must be non-empty"):
        Assumption("A-ID", "Claim", "", "high")


def test_hardware_status_requires_note():
    with pytest.raises(ValueError, match="status 'hardware-verified' requires a non-empty note"):
        Assumption("A-ID", "Claim", "Source", "high", status="hardware-verified", note="")
    with pytest.raises(ValueError, match="status 'hardware-contradicted' requires a non-empty note"):
        Assumption("A-ID", "Claim", "Source", "high", status="hardware-contradicted", note="   ")
