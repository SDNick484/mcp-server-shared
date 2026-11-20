"""Command line: `python -m evals.routing_eval snapshot|run|skeleton`.

Exit codes: 0 ok, 1 a baseline regression, 2 an error (an invalid case file, a missing `[eval]` extra or API
key, an unreadable baseline, a failed snapshot, or any trial that errored). A run with both an errored trial
and a regression exits 2: the comparison is not trustworthy when trials are missing from it.

Every subcommand is a handler `(args, client) -> int` registered with `set_defaults(handler=...)` in
`build_parser`, so adding one touches nothing else here.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from scripts.sync_all_siblings import discover_siblings, select_siblings

from evals.routing_eval.baseline import Regression, compare, render_comparison
from evals.routing_eval.cases import CaseError, Home, load_cases_dir
from evals.routing_eval.client import AnthropicClient, ModelClient
from evals.routing_eval.report import RunMeta, render_markdown, write_json
from evals.routing_eval.runner import BASELINE, Trial, Variant, run_all
from evals.routing_eval.score import Verdict, aggregate, score_all
from evals.routing_eval.skeleton import skeleton_from_harmony_dumps, write_skeleton
from evals.routing_eval.snapshot import (
    ServerSnapshot,
    SnapshotError,
    load_snapshots,
    snapshot_sibling,
    unannotated,
    write_snapshot,
)

EXIT_OK, EXIT_REGRESSION, EXIT_ERROR = 0, 1, 2


class UsageError(Exception):
    """Something other than a case file is wrong with the invocation; the message says what to do."""


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _at_least_one(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _tolerance(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.routing_eval",
        description="Measure whether a model routes home theater requests to the right MCP server and tool.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="{snapshot,run,skeleton}")

    snap = commands.add_parser(
        "snapshot", help="snapshot each sibling's tool list and instructions", description=_snapshot.__doc__
    )
    snap.add_argument("--parent-dir", type=Path, default=Path(".."), help="where the sibling checkouts live (..)")
    snap.add_argument("--only", metavar="NAMES", help="comma-separated siblings to snapshot, e.g. harmony,webos")
    snap.add_argument("--out", type=Path, default=Path("evals/snapshots"), help="output directory (evals/snapshots)")
    snap.set_defaults(handler=_snapshot)

    run = commands.add_parser("run", help="run the cases against a model", description=_run.__doc__)
    run.add_argument(
        "--cases", type=Path, required=True, metavar="DIR", help="case files, e.g. evals/cases/example-home"
    )
    run.add_argument(
        "--model", action="append", required=True, metavar="M", help="model to run (repeatable; no default)"
    )
    run.add_argument(
        "--skill",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="add a variant with this skill file (repeatable)",
    )
    run.add_argument("--trials", type=_at_least_one, default=5, help="trials per case (5)")
    run.add_argument("--max-turns", type=_at_least_one, default=6, help="model calls per trial, at most (6)")
    run.add_argument("--concurrency", type=_at_least_one, default=4, help="trials in flight at once (4)")
    run.add_argument("--with-notes", action="store_true", help="put each home's notes in the system prompt")
    run.add_argument("--case", metavar="GLOB", help="only cases whose id or home/id matches")
    run.add_argument(
        "--snapshots", type=Path, default=Path("evals/snapshots"), help="snapshot directory (evals/snapshots)"
    )
    run.add_argument("--results-dir", type=Path, default=Path("results"), help="where the JSON goes (results)")
    run.add_argument("--baseline", type=Path, metavar="FILE", help="results JSON of an earlier run to compare with")
    run.add_argument(
        "--tolerance", type=_tolerance, default=0.2, help="largest drop in pass rate that is not a regression (0.2)"
    )
    run.set_defaults(handler=_run)

    skel = commands.add_parser(
        "skeleton", help="write a case-file skeleton from Harmony dumps", description=_skeleton.__doc__
    )
    skel.add_argument(
        "--harmony-dump",
        nargs="+",
        required=True,
        type=Path,
        metavar="FILE",
        help="`mcp-server-harmony doctor --dump` output, one file per hub",
    )
    skel.add_argument(
        "--out", type=Path, required=True, metavar="PATH", help="the case file to write (outside this repo)"
    )
    skel.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    skel.set_defaults(handler=_skeleton)
    return parser


def main(argv: Sequence[str] | None = None, *, client: ModelClient | None = None) -> int:
    """Run the CLI. `client` replaces the Anthropic client (tests inject a fake: no network)."""
    args = build_parser().parse_args(argv)
    code: int = args.handler(args, client)
    return code


# -- snapshot ---------------------------------------------------------------------------------------


def _snapshot(args: argparse.Namespace, client: ModelClient | None) -> int:
    """Snapshot each sibling checkout's tools and instructions into --out (needs each sibling's .venv)."""
    siblings = select_siblings(discover_siblings(args.parent_dir), args.only)
    if not siblings:
        _err(f"no sibling checkouts found in {args.parent_dir} (looking for mcp-server-*/shared.lock)")
        return EXIT_ERROR
    failed = False
    for sibling in siblings:
        try:
            snap = snapshot_sibling(sibling)
        except SnapshotError as exc:
            _err(str(exc))
            failed = True
            continue
        print(write_snapshot(snap, args.out))
        if missing := unannotated(snap):
            _err(f"warning: {snap.server}: tools without annotations (treated as state-changing): {', '.join(missing)}")
    return EXIT_ERROR if failed else EXIT_OK


# -- run --------------------------------------------------------------------------------------------


def _load_snapshots(directory: Path) -> dict[str, ServerSnapshot]:
    try:
        snapshots = load_snapshots(directory)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"{directory}: cannot read the snapshots: {exc!r}") from exc
    if not snapshots:
        raise UsageError(f"{directory}: no snapshots; run `python -m evals.routing_eval snapshot` first")
    return snapshots


def _select(homes: list[Home], pattern: str | None) -> list[Home]:
    """The homes with only the cases matching `pattern` (against `id` and `home/id`); homes left empty drop out."""
    if pattern is None:
        return homes
    chosen = [
        replace(home, cases=cases)
        for home in homes
        if (
            cases := tuple(
                c
                for c in home.cases
                if fnmatch.fnmatchcase(c.id, pattern) or fnmatch.fnmatchcase(f"{home.name}/{c.id}", pattern)
            )
        )
    ]
    if not chosen:
        raise UsageError(f"--case {pattern}: no case matches")
    return chosen


def _variants(skills: Sequence[Path]) -> list[Variant]:
    """The baseline, then one variant per skill file, named by its file stem."""
    variants = [BASELINE]
    for path in skills:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"{path}: cannot read the skill file: {exc}") from exc
        if path.stem in {v.name for v in variants}:
            raise UsageError(f"{path}: a variant named '{path.stem}' already exists (rename the file)")
        variants.append(Variant(path.stem, text))
    return variants


def _load_baseline(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"{path}: cannot read the baseline: {exc}") from exc
    try:
        compare(data, [], 0.0)  # a malformed baseline is an error now, not after the model calls
    except ValueError as exc:
        raise UsageError(f"{path}: {exc}") from exc
    return dict(data)


def _make_client() -> ModelClient:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise UsageError("ANTHROPIC_API_KEY is not set")
    try:
        return AnthropicClient()
    except ImportError as exc:
        raise UsageError(f'the [eval] extra is not installed ({exc}); run: pip install -e ".[eval]"') from exc


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _results_path(results_dir: Path) -> Path:
    """`<timestamp>.json`, or `<timestamp>-2.json` and up when two runs start in the same second."""
    stamp = _timestamp()
    path = results_dir / f"{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = results_dir / f"{stamp}-{n}.json"
    return path


def _run_meta(
    homes: Sequence[Home], trials: Sequence[Trial], models: list[str], variants: list[Variant], notes: bool
) -> RunMeta:
    overridden: dict[str, list[str]] = {}
    treat_as_read: dict[str, list[str]] = {}
    for home in homes:
        for case in home.cases:
            key = f"{home.name}/{case.id}"
            if case.instructions:
                overridden[key] = sorted(case.instructions)
            # `mcp__<server>__<tool>`: only the home's overrides for servers this case installs matter
            if tools := sorted(t for t in home.treat_as_read if t.split("__")[1] in case.servers):
                treat_as_read[key] = tools
    unlisted: dict[str, list[str]] = {}
    for trial in trials:
        tools = unlisted.setdefault(f"{trial.home}/{trial.case_id}", [])
        tools += [tool for tool, _ in trial.unlisted if tool not in tools]
    return RunMeta(
        started=datetime.now(UTC).isoformat(timespec="seconds"),
        models=models,
        variants=[v.name for v in variants],
        with_notes=notes,
        overridden_instructions=overridden,
        treat_as_read=treat_as_read,
        unlisted={key: sorted(tools) for key, tools in unlisted.items() if tools},
    )


def _run(args: argparse.Namespace, client: ModelClient | None) -> int:
    """Run every case against each model and variant, print the markdown report and write the results JSON."""
    try:
        # Everything that can be wrong is checked before the first model call, so a typo costs nothing.
        snapshots = _load_snapshots(args.snapshots)
        homes = load_cases_dir(args.cases, snapshots)
        if not homes:
            raise UsageError(f"{args.cases}: no case files (*.toml) found")
        homes = _select(homes, args.case)
        variants = _variants(args.skill)
        baseline = _load_baseline(args.baseline)
        if client is None:
            client = _make_client()
    except (CaseError, UsageError) as exc:
        _err(str(exc))
        return EXIT_ERROR

    models = list(dict.fromkeys(args.model))
    live = client

    async def go() -> list[Trial]:
        return await run_all(
            live,
            models,
            variants,
            homes,
            snapshots,
            trials=args.trials,
            max_turns=args.max_turns,
            concurrency=args.concurrency,
            with_notes=args.with_notes,
        )

    trials = anyio.run(go)
    scored = score_all(homes, trials)
    results = aggregate(scored)
    meta = _run_meta(homes, trials, models, variants, args.with_notes)
    path = _results_path(args.results_dir)
    write_json(path, scored, results, meta)

    markdown = render_markdown(results, meta)
    regressions: list[Regression] = []
    if baseline is not None:
        regressions, new = compare(baseline, results, args.tolerance)
        markdown += "\n" + render_comparison(regressions, new, args.tolerance, str(args.baseline))
    sys.stdout.write(markdown)
    _err(f"results: {path}")

    errored = [t for t, verdict in scored if verdict is Verdict.ERROR]
    if errored:
        first = errored[0]
        _err(
            f"{len(errored)} trial(s) errored and are left out of the rates "
            f"(first: {first.home}/{first.case_id} {first.model}: {first.error})"
        )
        return EXIT_ERROR
    return EXIT_REGRESSION if regressions else EXIT_OK


# -- skeleton ---------------------------------------------------------------------------------------


def _skeleton(args: argparse.Namespace, client: ModelClient | None) -> int:
    """Write a case-file skeleton (the home's notes and Harmony reads) from `doctor --dump` files, to --out."""
    try:
        text = skeleton_from_harmony_dumps(args.harmony_dump, name=args.out.stem)
        write_skeleton(text, args.out, force=args.force)
    except ImportError as exc:
        _err(f'the [eval] extra is not installed ({exc}); run: pip install -e ".[eval]"')
        return EXIT_ERROR
    except (ValueError, OSError) as exc:
        _err(str(exc))
        return EXIT_ERROR
    print(args.out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
