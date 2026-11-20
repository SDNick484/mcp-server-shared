"""Tests for the routing eval's command line (evals/routing_eval/__main__.py). No network: a FakeClient is injected."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.routing_eval import __main__ as cli
from evals.routing_eval.client import ModelClient
from evals.routing_eval.snapshot import ServerSnapshot, ToolSpec, load_snapshots

from tests.routing_eval_fake import FakeClient, tool_response

FIXTURES = Path(__file__).resolve().parent / "routing_eval_fixtures"

HUB_START = "mcp__hubx__hubx_start_activity"
TV_POWER = "mcp__tvx__tvx_set_power"
HUB_STATUS = "mcp__hubx__hubx_get_status"

HOME_TOML = f"""\
[home]
name = "fixture-home"
servers = ["hubx", "tvx"]

[[reads."{HUB_STATUS}"]]
response = {{ hubs = [{{ hub = "hub-1" }}] }}

[[case]]
id = "switch"
request = "Switch to the game console."
trials = 2
expect.allow = [{{ tool = "{HUB_START}", args = {{ activity = "Play Console" }} }}]
expect.forbid = ["{TV_POWER}"]

[[case]]
id = "power-on"
request = "Turn the TV on."
trials = 2
expect.allow = [{{ tool = "{TV_POWER}" }}]
"""

GOOD_SWITCH = tool_response((HUB_START, {"hub": "hub-1", "activity": "Play Console"}))
BAD_SWITCH = tool_response((TV_POWER, {"tv": "tv-1", "on": True}))
GOOD_POWER = tool_response((TV_POWER, {"tv": "tv-1", "on": True}))


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cases"
    path.mkdir()
    (path / "home.toml").write_text(HOME_TOML, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from an empty directory, so a default like `results` can never touch the repo."""
    monkeypatch.chdir(tmp_path)


def scripted(*responses: Any) -> FakeClient:
    return FakeClient(list(responses))


def run(
    cases_dir: Path, results_dir: Path, client: ModelClient | None, *extra: str, models: tuple[str, ...] = ("model-a",)
) -> int:
    argv = ["run", "--cases", str(cases_dir), "--snapshots", str(FIXTURES), "--results-dir", str(results_dir)]
    for model in models:
        argv += ["--model", model]
    return cli.main([*argv, *extra], client=client)


def only_result(results_dir: Path) -> dict[str, Any]:
    (path,) = results_dir.glob("*.json")
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_run_writes_results_and_prints_markdown(
    cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results_dir = tmp_path / "out" / "results"
    client = scripted(GOOD_SWITCH, GOOD_SWITCH, GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, results_dir, client, "--concurrency", "1") == 0

    out, err = capsys.readouterr()
    assert out.startswith("# Routing eval\n")
    assert "| fixture-home/switch | 2/2 |" in out
    assert "| fixture-home/power-on | 2/2 |" in out
    assert "## Baseline comparison" not in out
    (path,) = results_dir.glob("*.json")
    assert str(path) in err
    data = json.loads(path.read_text())
    assert data["meta"]["models"] == ["model-a"]
    assert data["meta"]["variants"] == ["baseline"]
    assert [t["verdict"] for t in data["trials"]] == ["pass"] * 4
    assert [(r["case_id"], r["passes"], r["scored"]) for r in data["results"]] == [("switch", 2, 2), ("power-on", 2, 2)]


def test_with_notes_flag_puts_notes_in_the_prompt(cases_dir: Path, tmp_path: Path) -> None:
    (cases_dir / "home.toml").write_text(
        HOME_TOML.replace('servers = ["hubx", "tvx"]\n', 'servers = ["hubx", "tvx"]\nnotes = "hub-1 runs tv-1."\n', 1),
        encoding="utf-8",
    )
    for flag, expected in (((), False), (("--with-notes",), True)):
        client = scripted(GOOD_POWER, GOOD_POWER)
        assert run(cases_dir, tmp_path / f"r{expected}", client, "--case", "power-on", *flag) == 0
        assert all(("hub-1 runs tv-1." in r["system"]) is expected for r in client.requests)
        assert only_result(tmp_path / f"r{expected}")["meta"]["with_notes"] is expected


def test_trials_and_max_turns_flags(cases_dir: Path, tmp_path: Path) -> None:
    (cases_dir / "home.toml").write_text(
        HOME_TOML.replace("trials = 2\n", "").replace(
            '[[case]]\nid = "power-on"', '[[case]]\nid = "power-on"\ntrials = 1'
        ),
        encoding="utf-8",
    )
    # switch has no `trials` now, so --trials 3 applies; power-on pins 1
    client = scripted(GOOD_SWITCH, GOOD_SWITCH, GOOD_SWITCH, GOOD_POWER)
    assert run(cases_dir, tmp_path / "r", client, "--trials", "3", "--max-turns", "2", "--concurrency", "1") == 0
    data = only_result(tmp_path / "r")
    assert [(r["case_id"], r["scored"]) for r in data["results"]] == [("switch", 3), ("power-on", 1)]


def test_run_exit_1_on_regression(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = scripted(GOOD_SWITCH, GOOD_SWITCH, GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "first", good, "--concurrency", "1") == 0
    (baseline,) = (tmp_path / "first").glob("*.json")
    capsys.readouterr()

    worse = scripted(BAD_SWITCH, BAD_SWITCH, GOOD_POWER, GOOD_POWER)
    code = run(cases_dir, tmp_path / "second", worse, "--concurrency", "1", "--baseline", str(baseline))
    assert code == 1
    out = capsys.readouterr().out
    assert "## Baseline comparison" in out
    assert "- Regression: `fixture-home/switch model-a/baseline`: 100% -> 0%" in out
    assert "- Regression: `section:routing model-a/baseline`: 100% -> 50%" in out
    assert "power-on" not in out.split("## Baseline comparison")[1]


def test_run_exit_0_when_within_tolerance(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = scripted(GOOD_SWITCH, GOOD_SWITCH, GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "first", good, "--concurrency", "1") == 0
    (baseline,) = (tmp_path / "first").glob("*.json")
    capsys.readouterr()

    worse = scripted(BAD_SWITCH, BAD_SWITCH, GOOD_POWER, GOOD_POWER)
    args = ("--concurrency", "1", "--baseline", str(baseline), "--tolerance", "1.0")
    assert run(cases_dir, tmp_path / "second", worse, *args) == 0
    assert "No regressions." in capsys.readouterr().out


def test_run_exit_2_on_unreadable_baseline(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = scripted()
    for bad in (tmp_path / "missing.json", tmp_path / "bad.json", tmp_path / "wrong.json"):
        if bad.name == "bad.json":
            bad.write_text("{not json", encoding="utf-8")
        elif bad.name == "wrong.json":
            bad.write_text('{"results": 3}', encoding="utf-8")
        assert run(cases_dir, tmp_path / "r", client, "--baseline", str(bad)) == 2
        assert "baseline" in capsys.readouterr().err
    assert client.requests == []  # nothing was spent on a run whose comparison could not be made


def test_run_exit_2_on_error_trial(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Review Focus 5: a 429 mid-run ends only its own trial; the others finish, the results are written, exit 2.
    client = scripted(GOOD_SWITCH, RuntimeError("429 rate limited"), GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "r", client, "--concurrency", "1") == 2
    out, err = capsys.readouterr()
    assert "| fixture-home/switch | 1/1 |" in out
    assert "| fixture-home/power-on | 2/2 |" in out
    assert "1 trial(s) errored" in err
    assert "RuntimeError: 429 rate limited" in err
    data = only_result(tmp_path / "r")
    assert [t["verdict"] for t in data["trials"]] == ["pass", "error", "pass", "pass"]


def test_error_trial_beats_regression(cases_dir: Path, tmp_path: Path) -> None:
    good = scripted(GOOD_SWITCH, GOOD_SWITCH, GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "first", good, "--concurrency", "1") == 0
    (baseline,) = (tmp_path / "first").glob("*.json")
    bad = scripted(BAD_SWITCH, RuntimeError("boom"), GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "second", bad, "--concurrency", "1", "--baseline", str(baseline)) == 2


def test_run_exit_2_on_invalid_case(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cases_dir / "home.toml").write_text(HOME_TOML.replace("expect.forbid", "expect.forbd"), encoding="utf-8")
    client = scripted()
    assert run(cases_dir, tmp_path / "r", client) == 2
    err = capsys.readouterr().err
    assert "home.toml" in err
    assert "case switch" in err
    assert "forbd" in err
    assert client.requests == []
    assert not (tmp_path / "r").exists()


def test_run_exit_2_on_missing_cases_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path / "nope", tmp_path / "r", scripted()) == 2
    assert "not a directory" in capsys.readouterr().err


def test_run_exit_2_when_no_case_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "empty").mkdir()
    assert run(tmp_path / "empty", tmp_path / "r", scripted()) == 2
    assert "no case files" in capsys.readouterr().err


def test_run_exit_2_when_snapshots_missing(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["run", "--cases", str(cases_dir), "--snapshots", str(tmp_path / "none"), "--model", "m"]
    assert cli.main(argv, client=scripted()) == 2
    assert "snapshot" in capsys.readouterr().err


def test_run_exit_2_on_unreadable_snapshot(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    (snaps / "hubx.json").write_text("{}", encoding="utf-8")
    argv = ["run", "--cases", str(cases_dir), "--snapshots", str(snaps), "--model", "m"]
    assert cli.main(argv, client=scripted()) == 2
    assert "cannot read the snapshots" in capsys.readouterr().err


def test_case_glob_filters(cases_dir: Path, tmp_path: Path) -> None:
    client = scripted(GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "r", client, "--case", "power-*") == 0
    assert [r["case_id"] for r in only_result(tmp_path / "r")["results"]] == ["power-on"]
    assert len(client.requests) == 2


def test_case_glob_matches_home_slash_case(cases_dir: Path, tmp_path: Path) -> None:
    client = scripted(GOOD_SWITCH, GOOD_SWITCH)
    assert run(cases_dir, tmp_path / "r", client, "--case", "fixture-home/sw*") == 0
    assert [r["case_id"] for r in only_result(tmp_path / "r")["results"]] == ["switch"]


def test_case_glob_matching_nothing_is_an_error(
    cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = scripted()
    assert run(cases_dir, tmp_path / "r", client, "--case", "typo-*") == 2
    assert "typo-*" in capsys.readouterr().err
    assert client.requests == []


def test_skill_adds_variant_baseline_always_runs(cases_dir: Path, tmp_path: Path) -> None:
    skill = tmp_path / "home-theater.md"
    skill.write_text("SKILL TEXT: prefer the hub.", encoding="utf-8")
    client = scripted(GOOD_POWER, GOOD_POWER, GOOD_POWER, GOOD_POWER)
    assert (
        run(cases_dir, tmp_path / "r", client, "--case", "power-on", "--skill", str(skill), "--concurrency", "1") == 0
    )

    data = only_result(tmp_path / "r")
    assert data["meta"]["variants"] == ["baseline", "home-theater"]
    assert [(r["variant"], r["scored"]) for r in data["results"]] == [("baseline", 2), ("home-theater", 2)]
    with_skill = [r["system"] for r in client.requests if "SKILL TEXT" in r["system"]]
    assert len(with_skill) == 2
    assert len(client.requests) == 4


def test_two_models_each_run(cases_dir: Path, tmp_path: Path) -> None:
    client = scripted(GOOD_POWER, GOOD_POWER, GOOD_POWER, GOOD_POWER)
    assert run(cases_dir, tmp_path / "r", client, "--case", "power-on", "--concurrency", "1", models=("m1", "m2")) == 0
    assert [r["model"] for r in client.requests] == ["m1", "m1", "m2", "m2"]
    assert only_result(tmp_path / "r")["meta"]["models"] == ["m1", "m2"]


def test_skill_errors(cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(cases_dir, tmp_path / "r", scripted(), "--skill", str(tmp_path / "nope.md")) == 2
    assert "nope.md" in capsys.readouterr().err

    first = tmp_path / "a" / "skill.md"
    second = tmp_path / "b" / "skill.md"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text("x", encoding="utf-8")
    assert run(cases_dir, tmp_path / "r", scripted(), "--skill", str(first), "--skill", str(second)) == 2
    assert "skill" in capsys.readouterr().err

    reserved = tmp_path / "baseline.md"
    reserved.write_text("x", encoding="utf-8")
    assert run(cases_dir, tmp_path / "r", scripted(), "--skill", str(reserved)) == 2
    assert "baseline" in capsys.readouterr().err


def test_model_has_no_default(cases_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--cases", str(cases_dir)], client=scripted())
    assert exc.value.code == 2
    assert "--model" in capsys.readouterr().err


def test_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--trials", "--max-turns", "--concurrency"])
def test_counts_must_be_positive(cases_dir: Path, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--cases", str(cases_dir), "--model", "m", flag, "0"], client=scripted())
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_defaults_match_the_spec() -> None:
    args = cli.build_parser().parse_args(["run", "--cases", "c", "--model", "m"])
    assert (args.trials, args.max_turns, args.concurrency, args.tolerance) == (5, 6, 4, 0.2)
    assert (args.snapshots, args.results_dir, args.baseline, args.case, args.skill) == (
        Path("evals/snapshots"),
        Path("results"),
        None,
        None,
        [],
    )
    assert args.with_notes is False


def test_run_exit_2_without_api_key(
    cases_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "AnthropicClient", lambda: pytest.fail("must not build a real client"))
    assert run(cases_dir, tmp_path / "r", None) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


def test_run_exit_2_without_eval_extra(
    cases_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> ModelClient:
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "AnthropicClient", missing)
    assert run(cases_dir, tmp_path / "r", None) == 2
    assert 'pip install -e ".[eval]"' in capsys.readouterr().err


def test_run_builds_the_anthropic_client_when_none_injected(
    cases_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = scripted(GOOD_POWER, GOOD_POWER)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "AnthropicClient", lambda: fake)
    assert run(cases_dir, tmp_path / "r", None, "--case", "power-on") == 0
    assert len(fake.requests) == 2


def test_results_files_do_not_overwrite_each_other(
    cases_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_timestamp", lambda: "20260101T000000Z")
    for _ in range(2):
        assert run(cases_dir, tmp_path / "r", scripted(GOOD_POWER, GOOD_POWER), "--case", "power-on") == 0
    assert sorted(p.name for p in (tmp_path / "r").glob("*.json")) == [
        "20260101T000000Z-2.json",
        "20260101T000000Z.json",
    ]


def test_unscripted_reads_and_overrides_reach_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = tmp_path / "c"
    cases.mkdir()
    (cases / "home.toml").write_text(
        f"""\
[home]
name = "fixture-home"
servers = ["hubx", "tvx"]
treat_as_read = ["mcp__tvx__tvx_insert_text"]

[[case]]
id = "switch"
request = "Switch."
trials = 1
instructions.hubx.text = "Override."
expect.allow = [{{ tool = "{HUB_START}" }}]
""",
        encoding="utf-8",
    )
    client = scripted(
        tool_response((HUB_STATUS, {})),  # a read with no scripted response
        GOOD_SWITCH,
    )
    assert run(cases, tmp_path / "r", client) == 0
    out = capsys.readouterr().out
    assert "- `fixture-home/switch`: instructions replaced for hubx" in out
    assert "- `fixture-home/switch`: treated as read: mcp__tvx__tvx_insert_text" in out
    meta = only_result(tmp_path / "r")["meta"]
    assert meta["overridden_instructions"] == {"fixture-home/switch": ["hubx"]}
    assert meta["treat_as_read"] == {"fixture-home/switch": ["mcp__tvx__tvx_insert_text"]}


# -- snapshot ---------------------------------------------------------------------------------------


def make_sibling(parent: Path, name: str) -> Path:
    sibling = parent / f"mcp-server-{name}"
    sibling.mkdir(parents=True)
    (sibling / "shared.lock").write_text(f'package = "{name}_mcp"\n', encoding="utf-8")
    return sibling


def fake_snapshot(sibling: Path) -> ServerSnapshot:
    name = sibling.name.removeprefix("mcp-server-")
    tools = (
        ToolSpec(f"{name}_get", "Read it.", {"type": "object"}, None, {"readOnlyHint": True}),
        ToolSpec(f"{name}_set", "Change it.", {"type": "object"}, None, None),
    )
    return ServerSnapshot(name, f"{name}_mcp", "c" * 40, "Instructions.", tools)


def test_snapshot_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "src"
    for name in ("alpha", "beta"):
        make_sibling(parent, name)
    monkeypatch.setattr(cli, "snapshot_sibling", fake_snapshot)
    out_dir = tmp_path / "snaps"

    assert cli.main(["snapshot", "--parent-dir", str(parent), "--out", str(out_dir)]) == 0

    stdout, stderr = capsys.readouterr()
    assert sorted(p.name for p in out_dir.glob("*.json")) == ["alpha.json", "beta.json"]
    assert set(load_snapshots(out_dir)) == {"alpha", "beta"}
    assert str(out_dir / "alpha.json") in stdout
    # unannotated tools are warned about on stderr, by server and tool
    assert "alpha" in stderr
    assert "alpha_set" in stderr
    assert "alpha_get" not in stderr


def test_snapshot_only_selects_siblings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "src"
    for name in ("alpha", "beta", "gamma"):
        make_sibling(parent, name)
    monkeypatch.setattr(cli, "snapshot_sibling", fake_snapshot)
    out_dir = tmp_path / "snaps"
    assert (
        cli.main(["snapshot", "--parent-dir", str(parent), "--only", "beta,mcp-server-gamma", "--out", str(out_dir)])
        == 0
    )
    assert sorted(p.name for p in out_dir.glob("*.json")) == ["beta.json", "gamma.json"]


def test_snapshot_failure_is_exit_2_but_others_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "src"
    for name in ("alpha", "beta"):
        make_sibling(parent, name)

    def flaky(sibling: Path) -> ServerSnapshot:
        if sibling.name.endswith("alpha"):
            raise cli.SnapshotError("mcp-server-alpha: no .venv/bin/python; create the virtualenv")
        return fake_snapshot(sibling)

    monkeypatch.setattr(cli, "snapshot_sibling", flaky)
    out_dir = tmp_path / "snaps"
    assert cli.main(["snapshot", "--parent-dir", str(parent), "--out", str(out_dir)]) == 2
    assert "no .venv/bin/python" in capsys.readouterr().err
    assert [p.name for p in out_dir.glob("*.json")] == ["beta.json"]


def test_snapshot_exit_2_when_no_siblings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "empty").mkdir()
    assert cli.main(["snapshot", "--parent-dir", str(tmp_path / "empty"), "--out", str(tmp_path / "o")]) == 2
    assert "no sibling" in capsys.readouterr().err


def test_max_turns_flag_bounds_each_trial(cases_dir: Path, tmp_path: Path) -> None:
    client = FakeClient(lambda request: tool_response((HUB_STATUS, {})))  # reads forever, never acts
    code = run(cases_dir, tmp_path / "r", client, "--case", "switch", "--max-turns", "3", "--concurrency", "1")
    assert code == 0  # no baseline, and running out of turns is a verdict (abstain), not an error
    assert len(client.requests) == 2 * 3
    assert only_result(tmp_path / "r")["results"][0]["counts"] == {"abstain": 2}


@pytest.mark.parametrize(
    ("flag", "value", "complaint"),
    [
        ("--trials", "many", "not a whole number"),
        ("--tolerance", "lots", "not a number"),
        ("--tolerance", "1.5", "between 0 and 1"),
        ("--tolerance", "-0.1", "between 0 and 1"),
    ],
)
def test_bad_numbers_are_usage_errors(
    cases_dir: Path, flag: str, value: str, complaint: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--cases", str(cases_dir), "--model", "m", flag, value], client=scripted())
    assert exc.value.code == 2
    assert complaint in capsys.readouterr().err


def test_snapshot_silent_when_every_tool_is_annotated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sibling(tmp_path / "src", "alpha")

    def annotated(sibling: Path) -> ServerSnapshot:
        tool = ToolSpec("alpha_get", "Read it.", {"type": "object"}, None, {"readOnlyHint": True})
        return ServerSnapshot("alpha", "alpha_mcp", "c" * 40, None, (tool,))

    monkeypatch.setattr(cli, "snapshot_sibling", annotated)
    assert cli.main(["snapshot", "--parent-dir", str(tmp_path / "src"), "--out", str(tmp_path / "o")]) == 0
    assert capsys.readouterr().err == ""
