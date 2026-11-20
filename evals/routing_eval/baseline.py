"""Baseline comparison: did a run's pass rates drop against a saved results file?

The baseline is the JSON `report.write_json` wrote for an earlier run. Each case is compared on its own
and each section (routing, injection) on its pooled rate, per model and variant, so a model cannot trade
one section off against the other unnoticed (spec section 10). A rate counts as a regression only when it
falls by *more* than the tolerance: with five trials, 0.8 -> 0.6 is one trial and is within the default 0.2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evals.routing_eval.score import CaseResult, section_rate

# Rates are ratios of small integers, so `0.8 - 0.6` is `0.2000000000000001`: without a margin a drop of
# exactly the tolerance would count as exceeding it.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Regression:
    """`key` is `home/case model/variant`, or `section:<category> model/variant` for a pooled section."""

    key: str
    before: float
    after: float


def _case_key(home: str, case_id: str, model: str, variant: str) -> str:
    return f"{home}/{case_id} {model}/{variant}"


def _section_key(category: str, model: str, variant: str) -> str:
    return f"section:{category} {model}/{variant}"


def _baseline_results(baseline: dict[str, Any]) -> list[CaseResult]:
    """The baseline's `results` as `CaseResult`s (only the fields the comparison reads are kept)."""
    try:
        return [
            CaseResult(
                home=str(r["home"]),
                case_id=str(r["case_id"]),
                category=str(r["category"]),
                model=str(r["model"]),
                variant=str(r["variant"]),
                counts={},
                not_exposed=0,
                passes=int(r["passes"]),
                scored=int(r["scored"]),
            )
            for r in baseline["results"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"baseline is not a routing eval results file: {exc!r}") from exc


def _rates(results: Sequence[CaseResult]) -> dict[str, float | None]:
    """Every case's rate and every section's pooled rate, by key, in first-seen order."""
    rates: dict[str, float | None] = {}
    for r in results:
        rates[_case_key(r.home, r.case_id, r.model, r.variant)] = r.rate
    for category, model, variant in dict.fromkeys((r.category, r.model, r.variant) for r in results):
        rates[_section_key(category, model, variant)] = section_rate(results, category, model, variant)
    return rates


def compare(
    baseline: dict[str, Any], results: Sequence[CaseResult], tolerance: float
) -> tuple[list[Regression], list[str]]:
    """Regressions against `baseline` (the JSON `write_json` wrote), and the keys new since it.

    A regression is a case or a pooled section whose rate fell by more than `tolerance`. A key the baseline
    does not have is new: reported, never a failure. A key with no rate on either side (nothing scored) is
    skipped, as is a key the baseline has but this run does not (a filtered run is not a regression).
    Raises `ValueError` when `baseline` is not a results file."""
    before = _rates(_baseline_results(baseline))
    regressions: list[Regression] = []
    new: list[str] = []
    for key, after in _rates(results).items():
        if key not in before:
            new.append(key)
            continue
        was = before[key]
        if was is None or after is None:
            continue
        if was - after > tolerance + _EPSILON:
            regressions.append(Regression(key, was, after))
    return regressions, new


def render_comparison(regressions: Sequence[Regression], new: Sequence[str], tolerance: float, source: str) -> str:
    """The comparison as a markdown section for the report (ends with a single newline)."""
    lines = ["## Baseline comparison", "", f"Compared with `{source}`, tolerance {tolerance:g}.", ""]
    lines += [f"- Regression: `{r.key}`: {r.before:.0%} -> {r.after:.0%}" for r in regressions]
    lines += [f"- New since the baseline: `{key}`" for key in new]
    if not regressions:
        lines.append("No regressions.")
    return "\n".join(lines) + "\n"
