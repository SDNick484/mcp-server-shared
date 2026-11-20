"""Tests for the routing eval's report (evals/routing_eval/report.py)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.routing_eval.report import RunMeta, render_markdown, write_json
from evals.routing_eval.runner import RecordedCall, Trial
from evals.routing_eval.score import CaseResult, Verdict, aggregate


def meta(**kw: object) -> RunMeta:
    base: dict[str, object] = {
        "started": "2026-11-20T10:00:00Z",
        "models": ["model-a"],
        "variants": ["baseline", "skill"],
        "with_notes": False,
        "overridden_instructions": {},
        "treat_as_read": {},
        "unlisted": {},
    }
    return RunMeta(**{**base, **kw})  # type: ignore[arg-type]


def result(
    case_id: str,
    variant: str,
    passes: int,
    scored: int,
    *,
    category: str = "routing",
    counts: dict[str, int] | None = None,
    not_exposed: int = 0,
    home: str = "home-1",
) -> CaseResult:
    return CaseResult(
        home=home,
        case_id=case_id,
        category=category,
        model="model-a",
        variant=variant,
        counts=counts if counts is not None else {"pass": passes},
        not_exposed=not_exposed,
        passes=passes,
        scored=scored,
    )


TWO_CASES = [
    result("switch", "baseline", 4, 5, counts={"pass": 4, "fail-other": 1}),
    result("switch", "skill", 5, 5),
    result("power-off", "baseline", 3, 5, counts={"pass": 3, "fail-forbidden": 2}),
    result("power-off", "skill", 5, 5),
]

GOLDEN = """\
# Routing eval

- Started: 2026-11-20T10:00:00Z
- Models: model-a
- Variants: baseline, skill
- Notes: not included

## Routing

| Case | model-a / baseline | model-a / skill |
| --- | --- | --- |
| home-1/switch | 4/5 | 5/5 |
| home-1/power-off | 3/5 **2 forbidden** | 5/5 |
| **Pooled** | 70% (7/10) | 100% (10/10) |
"""


def test_markdown_sections_and_cells() -> None:
    assert render_markdown(TWO_CASES, meta()) == GOLDEN


def test_markdown_injection_cells_and_both_sections() -> None:
    results = [
        *TWO_CASES[:2],
        result(
            "youtube-title",
            "baseline",
            1,
            2,
            category="injection",
            counts={"pass": 3, "fail-injected": 1},
            not_exposed=2,
        ),
        result("youtube-title", "skill", 0, 0, category="injection", counts={"pass": 3}, not_exposed=3),
    ]
    md = render_markdown(results, meta(with_notes=True))
    assert "- Notes: included" in md
    assert md.index("## Routing") < md.index("## Injection")
    assert "| home-1/youtube-title | 1/2 **1 injected** (2 not exposed) | 0/0 (3 not exposed) |" in md
    # a section with nothing scored has no pooled rate
    assert "| **Pooled** | 50% (1/2) | n/a |" in md


def test_markdown_omits_empty_sections() -> None:
    md = render_markdown(TWO_CASES, meta())
    assert "## Injection" not in md
    assert "## Overrides in effect" not in md
    assert "## Under-specified" not in md
    injection_only = render_markdown([result("x", "baseline", 1, 1, category="injection")], meta())
    assert "## Routing" not in injection_only
    assert "## Injection" in injection_only


def test_markdown_overrides_and_under_specified() -> None:
    md = render_markdown(
        TWO_CASES,
        meta(
            overridden_instructions={"home-1/switch": ["harmony", "onkyo"]},
            treat_as_read={"home-1/power-off": ["mcp__tvx__tvx_get_status"]},
            unlisted={"home-1/switch": ["mcp__hubx__hubx_get_status"]},
        ),
    )
    assert md.endswith(
        """\

## Overrides in effect

- `home-1/switch`: instructions replaced for harmony, onkyo
- `home-1/power-off`: treated as read: mcp__tvx__tvx_get_status

## Under-specified

- `home-1/switch`: read without a scripted response (answered "no data"): mcp__hubx__hubx_get_status
"""
    )


def test_markdown_missing_combination_and_pipe_in_id() -> None:
    md = render_markdown([result("a|b", "baseline", 1, 1)], meta())
    assert "| home-1/a\\|b | 1/1 | - |" in md


def test_json_round_trips(tmp_path: Path) -> None:
    trials = [
        Trial(
            "home-1",
            "switch",
            "routing",
            "model-a",
            "baseline",
            0,
            [RecordedCall("t", {"a": 1}, 0)],
            "",
            False,
            [("mcp__x__read", {"k": "v"})],
            None,
        ),
        Trial("home-1", "switch", "routing", "model-a", "baseline", 1, [], "", False, [], "RateLimitError: 429"),
    ]
    scored = [(trials[0], Verdict.OTHER), (trials[1], Verdict.ERROR)]
    results = aggregate(scored)
    path = tmp_path / "out" / "run.json"
    write_json(path, scored, results, meta(with_notes=True, unlisted={"home-1/switch": ["mcp__x__read"]}))

    text = path.read_text()
    assert text.endswith("\n")
    data = json.loads(text)
    assert data["meta"]["with_notes"] is True
    assert data["meta"]["unlisted"] == {"home-1/switch": ["mcp__x__read"]}
    assert [t["verdict"] for t in data["trials"]] == ["fail-other", "error"]
    assert data["trials"][0]["calls"] == [{"tool": "t", "args": {"a": 1}, "response_index": 0}]
    assert data["trials"][0]["unlisted"] == [["mcp__x__read", {"k": "v"}]]
    assert data["trials"][1]["error"] == "RateLimitError: 429"
    (res,) = data["results"]
    assert res["rate"] == 0.0
    assert res["counts"] == {"fail-other": 1, "error": 1}
    assert (res["scored"], res["passes"], res["not_exposed"]) == (1, 0, 0)


def test_json_rate_is_null_when_nothing_scored(tmp_path: Path) -> None:
    results = [result("x", "baseline", 0, 0)]
    write_json(tmp_path / "r.json", [], results, meta())
    assert json.loads((tmp_path / "r.json").read_text())["results"][0]["rate"] is None
