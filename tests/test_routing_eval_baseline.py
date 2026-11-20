"""Tests for the routing eval's baseline comparison (evals/routing_eval/baseline.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.routing_eval.baseline import Regression, compare, render_comparison
from evals.routing_eval.report import RunMeta, write_json
from evals.routing_eval.score import CaseResult


def result(
    case_id: str,
    passes: int,
    scored: int,
    *,
    category: str = "routing",
    model: str = "model-a",
    variant: str = "baseline",
) -> CaseResult:
    return CaseResult("home-1", case_id, category, model, variant, {}, 0, passes, scored)


def baseline_of(results: list[CaseResult], tmp_path: Path) -> dict[str, Any]:
    """A baseline as a real run writes it: through `write_json`, then read back."""
    meta = RunMeta("2026-01-01T00:00:00Z", ["model-a"], ["baseline"], False, {}, {}, {})
    path = tmp_path / "baseline.json"
    write_json(path, [], results, meta)
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_drop_beyond_tolerance_regresses(tmp_path: Path) -> None:
    base = baseline_of([result("switch", 4, 5)], tmp_path)  # 0.8
    regressions, new = compare(base, [result("switch", 2, 5)], 0.2)  # 0.4
    assert regressions[0] == Regression("home-1/switch model-a/baseline", 0.8, 0.4)
    assert new == []


def test_drop_within_tolerance_ok(tmp_path: Path) -> None:
    # 0.8 - 0.6 is 0.20000000000000007 in floats: exactly one trial in five must not trip a 0.2 tolerance.
    base = baseline_of([result("switch", 4, 5)], tmp_path)
    assert compare(base, [result("switch", 3, 5)], 0.2) == ([], [])


def test_improvement_is_not_a_regression(tmp_path: Path) -> None:
    base = baseline_of([result("switch", 2, 5)], tmp_path)
    assert compare(base, [result("switch", 5, 5)], 0.2) == ([], [])


def test_section_regression_detected_when_cases_individually_ok(tmp_path: Path) -> None:
    # Case "a" drops by exactly the tolerance and case "b" is new, so no case regresses; but the section's
    # pooled rate went from 5/5 to 4/10, which is the drop the section check exists to catch.
    base = baseline_of([result("a", 5, 5)], tmp_path)
    regressions, new = compare(base, [result("a", 4, 5), result("b", 0, 5)], 0.2)
    assert [r.key for r in regressions] == ["section:routing model-a/baseline"]
    assert (regressions[0].before, regressions[0].after) == (1.0, 0.4)
    assert new == ["home-1/b model-a/baseline"]


def test_sections_are_compared_separately(tmp_path: Path) -> None:
    base = baseline_of([result("a", 5, 5), result("inj", 5, 5, category="injection")], tmp_path)
    regressions, _ = compare(base, [result("a", 5, 5), result("inj", 2, 5, category="injection")], 0.2)
    assert [r.key for r in regressions] == ["home-1/inj model-a/baseline", "section:injection model-a/baseline"]


def test_regression_is_per_model_and_variant(tmp_path: Path) -> None:
    base = baseline_of([result("a", 5, 5), result("a", 5, 5, variant="skill")], tmp_path)
    regressions, _ = compare(base, [result("a", 5, 5), result("a", 1, 5, variant="skill")], 0.2)
    assert [r.key for r in regressions] == ["home-1/a model-a/skill", "section:routing model-a/skill"]


def test_new_cases_reported_not_failed(tmp_path: Path) -> None:
    base = baseline_of([result("old", 4, 5)], tmp_path)
    regressions, new = compare(base, [result("old", 4, 5), result("fresh", 0, 5, model="model-b")], 0.2)
    assert regressions == []
    assert new == ["home-1/fresh model-b/baseline", "section:routing model-b/baseline"]


def test_baseline_cases_missing_from_the_run_are_ignored(tmp_path: Path) -> None:
    base = baseline_of([result("a", 5, 5), result("gone", 0, 5)], tmp_path)
    assert compare(base, [result("a", 5, 5)], 0.2) == ([], [])


def test_none_rates_skipped(tmp_path: Path) -> None:
    # Nothing scored (every trial errored or was never exposed) is no evidence in either direction.
    base = baseline_of([result("a", 5, 5), result("b", 0, 0)], tmp_path)
    assert compare(base, [result("a", 0, 0), result("b", 0, 0)], 0.2) == ([], [])
    # "b" had no rate in the baseline: its own key is skipped, not compared against a rate of zero
    # (only the pooled section, which did have one, is judged).
    regressions, new = compare(base, [result("a", 5, 5), result("b", 0, 5)], 0.2)
    assert [r.key for r in regressions] == ["section:routing model-a/baseline"]
    assert new == []


def test_zero_tolerance_flags_any_drop(tmp_path: Path) -> None:
    base = baseline_of([result("a", 4, 5)], tmp_path)
    regressions, _ = compare(base, [result("a", 3, 5)], 0.0)
    assert [r.key for r in regressions] == ["home-1/a model-a/baseline", "section:routing model-a/baseline"]


@pytest.mark.parametrize("bad", [{}, {"results": None}, {"results": [{"home": "h"}]}, {"results": [{}]}])
def test_malformed_baseline_raises_value_error(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="not a routing eval results file"):
        compare(bad, [], 0.2)


def test_render_comparison_lists_regressions_and_new_keys() -> None:
    text = render_comparison(
        [Regression("home-1/a model-a/baseline", 0.8, 0.4)], ["home-1/b model-a/baseline"], 0.2, "b.json"
    )
    assert text == (
        "## Baseline comparison\n"
        "\n"
        "Compared with `b.json`, tolerance 0.2.\n"
        "\n"
        "- Regression: `home-1/a model-a/baseline`: 80% -> 40%\n"
        "- New since the baseline: `home-1/b model-a/baseline`\n"
    )


def test_render_comparison_when_clean() -> None:
    assert render_comparison([], [], 0.2, "b.json").endswith("\nNo regressions.\n")
