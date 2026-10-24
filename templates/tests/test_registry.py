"""The assumption registry's machinery: statuses, queries and doctor's summary.

Shared by the sibling servers along with registry.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

These test the machinery with a made-up registry. Each sibling's own
test_assumptions.py checks its real one (ids, citations, docs).
"""

from __future__ import annotations

import dataclasses

import pytest

from __PKG__.registry import Assumption, by_id, contradicted, doctor_lines, unverified

FAKE = (
    Assumption("X-ONE", "a", "docs", "high"),
    Assumption("X-TWO", "b", "source", "medium", status="hardware-verified", note="seen"),
    Assumption("X-THREE", "c", "guess", "low", status="hardware-contradicted", note="saw otherwise"),
    Assumption("X-FOUR", "d", "docs", "high"),
)


def test_defaults_to_simulator_only_with_no_note():
    a = Assumption("X-NEW", "claim", "docs", "high")
    assert (a.status, a.note) == ("simulator-only", "")


def test_records_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FAKE[0].status = "hardware-verified"  # type: ignore[misc]


def test_by_id_indexes_every_entry():
    assert list(by_id(FAKE)) == ["X-ONE", "X-TWO", "X-THREE", "X-FOUR"]
    assert by_id(FAKE)["X-TWO"].note == "seen"


def test_by_id_refuses_a_repeated_id():
    with pytest.raises(ValueError, match="X-ONE"):
        by_id((*FAKE, Assumption("X-ONE", "again", "docs", "low")))


def test_unverified_is_simulator_only_and_excludes_contradicted():
    # The bug this module was shared to fix: "anything not hardware-verified" counted
    # X-THREE as not yet confirmed, though hardware checked it and the code works around it.
    assert [a.id for a in unverified(FAKE)] == ["X-ONE", "X-FOUR"]


def test_contradicted_is_its_own_list():
    assert [a.id for a in contradicted(FAKE)] == ["X-THREE"]


def test_every_status_lands_in_exactly_one_place():
    verified = [a for a in FAKE if a.status == "hardware-verified"]
    assert len(unverified(FAKE)) + len(contradicted(FAKE)) + len(verified) == len(FAKE)


def test_queries_take_any_iterable():
    assert [a.id for a in unverified(iter(FAKE))] == ["X-ONE", "X-FOUR"]


# --- doctor's summary ------------------------------------------------------------------------------


def test_brief_counts_unverified_and_lists_contradicted():
    lines = doctor_lines(["X-ONE: a", "X-FOUR: d"], ["X-THREE: c"])
    assert lines == [
        "Contradicted on hardware, no longer relied on (1):",
        "   X-THREE: c",
        "Not yet confirmed on hardware: 2 (doctor --verbose lists them)",
    ]


def test_verbose_lists_unverified():
    lines = doctor_lines(["X-ONE: a", "X-FOUR: d"], [], verbose=True)
    assert lines == ["Not yet confirmed on hardware (2):", "   X-ONE: a", "   X-FOUR: d"]


@pytest.mark.parametrize("verbose", [False, True])
def test_nothing_pending_says_zero_without_the_hint(verbose):
    assert doctor_lines([], [], verbose=verbose) == ["Not yet confirmed on hardware: 0"]
