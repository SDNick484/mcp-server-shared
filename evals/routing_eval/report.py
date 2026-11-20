"""The report: markdown for people, JSON for the baseline comparison.

Markdown has one section per category (`## Routing`, `## Injection`), a row per case and a column per
`model / variant` showing `passes/scored`, then the overrides and under-specified reads that shaped the run.
Sections with nothing to show are left out. The JSON holds the run's `meta`, every trial with its verdict,
and every `CaseResult` with its `rate`; `baseline.compare` reads that file back.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from evals.routing_eval.runner import Trial
from evals.routing_eval.score import CaseResult, Verdict, section_rate

_SECTIONS = (("routing", "Routing"), ("injection", "Injection"))


@dataclass
class RunMeta:
    """What the run was, for the report. The dicts are keyed by `home/case`."""

    started: str
    models: list[str]
    variants: list[str]
    with_notes: bool
    overridden_instructions: dict[str, list[str]]
    treat_as_read: dict[str, list[str]]
    unlisted: dict[str, list[str]]


def _cell(result: CaseResult) -> str:
    cell = f"{result.passes}/{result.scored}"
    for verdict, label in ((Verdict.FORBIDDEN, "forbidden"), (Verdict.INJECTED, "injected")):
        if n := result.counts.get(verdict.value, 0):
            cell += f" **{n} {label}**"
    if result.not_exposed:
        cell += f" ({result.not_exposed} not exposed)"
    return cell


def _pooled(results: Sequence[CaseResult], category: str, model: str, variant: str) -> str:
    rate = section_rate(results, category, model, variant)
    if rate is None:
        return "n/a"
    chosen = [r for r in results if r.category == category and r.model == model and r.variant == variant]
    return f"{rate:.0%} ({sum(r.passes for r in chosen)}/{sum(r.scored for r in chosen)})"


def _section(results: Sequence[CaseResult], category: str, title: str, meta: RunMeta) -> list[str]:
    chosen = [r for r in results if r.category == category]
    if not chosen:
        return []
    columns = [(m, v) for m in meta.models for v in meta.variants]
    cells = {(r.home, r.case_id, r.model, r.variant): _cell(r) for r in chosen}
    rows = list(dict.fromkeys((r.home, r.case_id) for r in chosen))
    lines = [
        f"## {title}",
        "",
        "| Case | " + " | ".join(f"{m} / {v}" for m, v in columns) + " |",
        "| --- |" + " --- |" * len(columns),
    ]
    for home, case_id in rows:
        name = f"{home}/{case_id}".replace("|", "\\|")
        lines.append(f"| {name} | " + " | ".join(cells.get((home, case_id, m, v), "-") for m, v in columns) + " |")
    lines.append("| **Pooled** | " + " | ".join(_pooled(results, category, m, v) for m, v in columns) + " |")
    return [*lines, ""]


def _bullets(title: str, entries: list[str]) -> list[str]:
    return [f"## {title}", "", *entries, ""] if entries else []


def render_markdown(results: Sequence[CaseResult], meta: RunMeta) -> str:
    """The run as markdown. Ends with a single newline."""
    lines = [
        "# Routing eval",
        "",
        f"- Started: {meta.started}",
        f"- Models: {', '.join(meta.models)}",
        f"- Variants: {', '.join(meta.variants)}",
        f"- Notes: {'included' if meta.with_notes else 'not included'}",
        "",
    ]
    for category, title in _SECTIONS:
        lines += _section(results, category, title, meta)
    overrides = [
        f"- `{key}`: instructions replaced for {', '.join(servers)}"
        for key, servers in meta.overridden_instructions.items()
        if servers
    ] + [f"- `{key}`: treated as read: {', '.join(tools)}" for key, tools in meta.treat_as_read.items() if tools]
    lines += _bullets("Overrides in effect", overrides)
    unlisted = [
        f'- `{key}`: read without a scripted response (answered "no data"): {", ".join(tools)}'
        for key, tools in meta.unlisted.items()
        if tools
    ]
    lines += _bullets("Under-specified", unlisted)
    return "\n".join(lines[:-1] if lines[-1] == "" else lines) + "\n"


def write_json(
    path: Path, scored: Sequence[tuple[Trial, Verdict]], results: Sequence[CaseResult], meta: RunMeta
) -> None:
    """Write the run to `path` (parent directories are created): `meta`, `trials` (each with its `verdict`)
    and `results` (each with its `rate`, null when nothing was scored)."""
    data = {
        "meta": asdict(meta),
        "trials": [{**asdict(trial), "verdict": verdict.value} for trial, verdict in scored],
        "results": [{**asdict(r), "rate": r.rate} for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
