"""The assumption registry: status, confidence, queries and doctor formatting.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why a registry instead of comments (the pattern across all sibling MCP servers):
a comment saying "unverified" or "works on the simulator only" is easy to miss
and never goes away. Here each protocol detail this server relies on has an id
that:
  - code cites next to where it depends on it (``# ASSUMPTION H-PROVISION``),
  - a test cites to document it,
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - the README's verification table lists, with its status,
  - `doctor` prints, with its status and note.

tests/test_assumptions.py fails if an id is missing from the README or
HARDWARE_VALIDATION.md, so the three can't drift apart.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    """One protocol detail or device behavior this server relies on."""

    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""

    def __post_init__(self) -> None:
        # The Literal types only bind under mypy. Checked here too, so a typo'd status in a sibling's
        # ASSUMPTIONS ("hardware-verfied") fails at import rather than quietly matching no query.
        if not self.id.strip() or not self.claim.strip() or not self.source.strip():
            raise ValueError(f"assumption {self.id!r}: id, claim and source must be non-empty")
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(
                f"assumption {self.id!r}: confidence {self.confidence!r} is not one of ('high', 'medium', 'low')"
            )
        if self.status not in ("simulator-only", "hardware-verified", "hardware-contradicted"):
            raise ValueError(
                f"assumption {self.id!r}: status {self.status!r} is not one of"
                " ('simulator-only', 'hardware-verified', 'hardware-contradicted')"
            )
        if self.status in ("hardware-verified", "hardware-contradicted") and not self.note.strip():
            raise ValueError(f"assumption {self.id!r}: status {self.status!r} requires a non-empty note")


def by_id(assumptions: Iterable[Assumption]) -> dict[str, Assumption]:
    """Map assumptions by id. Raises ValueError on duplicate ids so a copy-paste typo fails at import."""
    out: dict[str, Assumption] = {}
    for a in assumptions:
        if a.id in out:
            raise ValueError(f"duplicate assumption id: {a.id!r}")
        out[a.id] = a
    return out


def unverified(assumptions: Iterable[Assumption]) -> list[Assumption]:
    """Still ``simulator-only``. A contradicted assumption was checked, so it isn't counted here."""
    return [a for a in assumptions if a.status == "simulator-only"]


def contradicted(assumptions: Iterable[Assumption]) -> list[Assumption]:
    """Seen wrong on hardware (``hardware-contradicted``); the code was changed to stop relying on them."""
    return [a for a in assumptions if a.status == "hardware-contradicted"]


def hardware_verified(assumptions: Iterable[Assumption]) -> list[Assumption]:
    """Seen right on hardware (``hardware-verified``)."""
    return [a for a in assumptions if a.status == "hardware-verified"]


# --- doctor output: text and JSON --------------------------------------------------------
def doctor_lines(
    unverified: list[Assumption],
    contradicted: list[Assumption],
    *,
    verbose: bool = False,
) -> list[str]:
    """Format the assumption section for a server's `doctor` command.

    Contradicted assumptions come first: if a server is failing, an assumption
    already known to be wrong on hardware is the first place to look.
    """
    lines: list[str] = []
    if contradicted:
        lines += [
            "",
            f"Contradicted on hardware, no longer relied on ({len(contradicted)}):",
        ]
        for a in contradicted:
            lines.append(f"   ! {a.id}: {a.claim}")
            if a.note:
                lines.append(f"     saw: {a.note}")
    if not unverified:
        return lines
    lines += ["", f"Not yet confirmed on hardware ({len(unverified)}):"]
    if verbose:
        for a in unverified:
            lines.append(f"   ? {a.id} [{a.confidence} confidence]: {a.claim}")
            lines.append(f"     from: {a.source}")
    else:
        lines += [f"   ? {a.id}: {a.claim}" for a in unverified]
    return lines


def doctor_summary(assumptions: Iterable[Assumption]) -> dict[str, Any]:
    """The JSON block `doctor --json` carries under `assumptions`.

    Identical across all sibling servers, so one dashboard or CI job can check
    every server's status without having to know which server it is looking at.
    """
    all_list = list(assumptions)
    unv = unverified(all_list)
    cnt = contradicted(all_list)
    vfd = hardware_verified(all_list)
    return {
        "total": len(all_list),
        "unverified_count": len(unv),
        "contradicted_count": len(cnt),
        "verified_count": len(vfd),
        "unverified": [asdict(a) for a in unv],
        "contradicted": [asdict(a) for a in cnt],
        "verified": [asdict(a) for a in vfd],
    }
