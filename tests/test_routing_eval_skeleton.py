"""Tests for the Harmony dump to case skeleton converter (evals/routing_eval/skeleton.py). No network, no hub."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from evals.routing_eval import __main__ as cli
from evals.routing_eval import skeleton
from evals.routing_eval.cases import load_home
from evals.routing_eval.snapshot import ServerSnapshot, ToolSpec

FIXTURES = Path(__file__).resolve().parent / "routing_eval_fixtures"
DUMP = FIXTURES / "dumps" / "harmony-dump.json"  # in a subdirectory: load_snapshots globs the fixtures folder

STATUS = "mcp__harmony__harmony_get_status"
LIST = "mcp__harmony__harmony_list_activities"
START = "mcp__harmony__harmony_start_activity"


def _spec(name: str, properties: dict[str, Any], *, read: bool) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        input_schema={"type": "object", "properties": properties},
        output_schema=None,
        annotations={"readOnlyHint": read},
    )


@pytest.fixture
def snapshots() -> dict[str, ServerSnapshot]:
    """A minimal harmony snapshot: the fixtures directory has none (it would change the other tests' defaults)."""
    tools = (
        _spec("harmony_get_status", {}, read=True),
        _spec("harmony_list_activities", {"hub": {"type": "string"}}, read=True),
        _spec("harmony_start_activity", {"activity": {"type": "string"}, "hub": {"type": "string"}}, read=False),
    )
    return {"harmony": ServerSnapshot("harmony", "harmony_mcp", "0" * 40, None, tools)}


def dump_with(tmp_path: Path, filename: str, **changes: Any) -> Path:
    """A copy of the fixture dump with the top-level keys in `changes` replaced (None removes one)."""
    data = json.loads(DUMP.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = tmp_path / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def parsed(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def status_hubs(text: str) -> list[dict[str, Any]]:
    hubs: list[dict[str, Any]] = parsed(text)["reads"][STATUS][0]["response"]["hubs"]
    return hubs


def test_skeleton_parses_as_toml_and_loads_as_home(tmp_path: Path, snapshots: dict[str, ServerSnapshot]) -> None:
    text = skeleton.skeleton_from_harmony_dumps([DUMP], name="my-home")
    case = f"""
[[case]]
id = "play-console"
request = "Put the game console on."
expect.allow = [{{ tool = "{START}", args = {{ activity = "Watch Console" }} }}]
"""
    out = tmp_path / "my-home.toml"
    out.write_text(text + case, encoding="utf-8")

    home = load_home(out, snapshots)

    assert home.name == "my-home"
    assert [c.id for c in home.cases] == ["play-console"]
    assert set(home.cases[0].reads) == {STATUS, LIST}
    assert parsed(text)["home"]["servers"] == ["harmony"]


def test_skeleton_alone_loads_with_no_cases(tmp_path: Path, snapshots: dict[str, ServerSnapshot]) -> None:
    out = tmp_path / "bare.toml"
    out.write_text(skeleton.skeleton_from_harmony_dumps([DUMP]), encoding="utf-8")
    home = load_home(out, snapshots)
    assert home.cases == ()
    assert home.name == "bare"  # no name written: the loader's default, the file's stem


def test_header_and_case_template_are_comments() -> None:
    text = skeleton.skeleton_from_harmony_dumps([DUMP])
    assert text.startswith("# ")
    assert "generated from a Harmony dump" in text.splitlines()[0] + text.splitlines()[1]
    assert "# [[case]]" in text
    assert "[[case]]" not in parsed(text)  # only the commented template mentions it


def test_notes_list_devices_and_inputs() -> None:
    notes = parsed(skeleton.skeleton_from_harmony_dumps([DUMP]))["home"]["notes"]
    assert "hub-1" in notes
    assert "Watch Console: Device A, Device B (input HDMI 2)" in notes
    assert "Watch Streamer: Device A (input HDMI 1), Device B (input HDMI 3)" in notes
    assert "PowerOff" not in notes  # activity -1 is the hub's own off
    assert "Device C" not in notes  # off in one activity, powered by hand in the other


def test_status_and_activities_reads() -> None:
    data = parsed(skeleton.skeleton_from_harmony_dumps([DUMP]))
    assert data["reads"][STATUS][0]["response"]["hubs"] == [
        {"hub": "hub-1", "reachable": True, "power": "on", "current_activity": {"id": 1001, "name": "Watch Console"}}
    ]
    assert data["reads"][LIST][0]["response"]["result"] == [
        {"hub": "hub-1", "name": "Watch Console", "ref": "hub-1/Watch Console", "running": True},
        {"hub": "hub-1", "name": "Watch Streamer", "ref": "hub-1/Watch Streamer", "running": False},
    ]


@pytest.mark.parametrize("off", [-1, "-1"])
def test_off_hub_has_no_current_activity(tmp_path: Path, off: int | str) -> None:
    dump = dump_with(tmp_path, "off.json", current_activity=off)
    text = skeleton.skeleton_from_harmony_dumps([dump])
    (hub,) = status_hubs(text)
    assert hub == {"hub": "hub-1", "reachable": True, "power": "off"}
    activities = parsed(text)["reads"][LIST][0]["response"]["result"]
    assert [a["running"] for a in activities] == [False, False]


def test_string_activity_id_marks_the_running_one(tmp_path: Path) -> None:
    dump = dump_with(tmp_path, "str.json", current_activity="1002")
    (hub,) = status_hubs(skeleton.skeleton_from_harmony_dumps([dump]))
    assert hub["current_activity"] == {"id": 1002, "name": "Watch Streamer"}


def test_two_dumps_two_hubs(tmp_path: Path) -> None:
    second = dump_with(tmp_path, "second.json", name="hub-2", current_activity="-1")
    text = skeleton.skeleton_from_harmony_dumps([DUMP, second])
    data = parsed(text)
    assert [h["hub"] for h in status_hubs(text)] == ["hub-1", "hub-2"]
    refs = [a["ref"] for a in data["reads"][LIST][0]["response"]["result"]]
    assert refs == ["hub-1/Watch Console", "hub-1/Watch Streamer", "hub-2/Watch Console", "hub-2/Watch Streamer"]
    assert "hub-1" in data["home"]["notes"] and "hub-2" in data["home"]["notes"]


def test_hub_name_falls_back_to_file_stem(tmp_path: Path) -> None:
    dump = dump_with(tmp_path, "den-hub.json", name=None)
    (hub,) = status_hubs(skeleton.skeleton_from_harmony_dumps([dump]))
    assert hub["hub"] == "den-hub"


def test_same_hub_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hub-1.*more than one"):
        skeleton.skeleton_from_harmony_dumps([DUMP, dump_with(tmp_path, "copy.json")])


def test_no_dumps_is_refused() -> None:
    with pytest.raises(ValueError, match="no dump"):
        skeleton.skeleton_from_harmony_dumps([])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"config": []}, "config"),
        ({"config": {"activity": "x"}}, "activity"),
        ({"config": {"activity": [], "device": "x"}}, "device"),
        ({"current_activity": None}, "current_activity"),
        ({"current_activity": 4242}, "4242"),
    ],
)
def test_malformed_dump_names_the_file(tmp_path: Path, changes: dict[str, Any], message: str) -> None:
    dump = dump_with(tmp_path, "bad.json", **changes)
    with pytest.raises(ValueError, match=f"bad.json.*{message}"):
        skeleton.skeleton_from_harmony_dumps([dump])


def test_unreadable_and_non_json_dumps_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing.json"):
        skeleton.skeleton_from_harmony_dumps([tmp_path / "missing.json"])
    junk = tmp_path / "junk.json"
    junk.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="junk.json"):
        skeleton.skeleton_from_harmony_dumps([junk])
    top = tmp_path / "list.json"
    top.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="list.json.*object"):
        skeleton.skeleton_from_harmony_dumps([top])


def test_tolerates_odd_entries(tmp_path: Path) -> None:
    """Entries the real hub might emit that the skeleton can't use are skipped, not fatal."""
    data = json.loads(DUMP.read_text(encoding="utf-8"))
    data["config"]["activity"] += ["junk", {"label": "No Id"}, {"id": "1003"}]
    data["config"]["activity"][1]["fixit"]["2004"] = {"id": "2004", "Power": "On"}  # a device the config lacks
    data["config"]["activity"][2]["fixit"]["2005"] = "junk"
    data["config"]["device"] += ["junk", {"label": "No Id"}]
    path = tmp_path / "odd.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    text = skeleton.skeleton_from_harmony_dumps([path])
    names = [a["name"] for a in parsed(text)["reads"][LIST][0]["response"]["result"]]
    assert names == ["Watch Console", "Watch Streamer", "1003"]
    assert "Device 2004" in parsed(text)["home"]["notes"]


def test_names_are_escaped_not_interpolated(tmp_path: Path) -> None:
    data = json.loads(DUMP.read_text(encoding="utf-8"))
    data["name"] = 'hub "one"\nx = 1'
    path = tmp_path / "quote.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    (hub,) = status_hubs(skeleton.skeleton_from_harmony_dumps([path]))
    assert hub["hub"] == 'hub "one"\nx = 1'


def test_writes_outside_the_repo(tmp_path: Path) -> None:
    out = tmp_path / "private" / "home.toml"
    skeleton.write_skeleton("a = 1\n", out, force=False)
    assert out.read_text(encoding="utf-8") == "a = 1\n"


@pytest.mark.parametrize("relative", ["home.toml", "evals/cases/private/home.toml", "evals/../home.toml"])
def test_refuses_path_inside_repo(relative: str) -> None:
    repo = Path(skeleton.__file__).resolve().parents[2]
    out = repo / relative
    with pytest.raises(ValueError, match="inside this repository"):
        skeleton.write_skeleton("a = 1\n", out, force=True)
    assert not (repo / "evals" / "cases" / "private").exists()


def test_refuses_symlink_into_repo(tmp_path: Path) -> None:
    repo = Path(skeleton.__file__).resolve().parents[2]
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError, match="inside this repository"):
        skeleton.write_skeleton("a = 1\n", link / "home.toml", force=True)


def test_refuses_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "home.toml"
    out.write_text("keep me\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists.*--force"):
        skeleton.write_skeleton("new\n", out, force=False)
    assert out.read_text(encoding="utf-8") == "keep me\n"
    skeleton.write_skeleton("new\n", out, force=True)
    assert out.read_text(encoding="utf-8") == "new\n"


def test_cli_skeleton_subcommand(tmp_path: Path, snapshots: dict[str, ServerSnapshot]) -> None:
    out = tmp_path / "private" / "my-home.toml"
    assert cli.main(["skeleton", "--harmony-dump", str(DUMP), "--out", str(out)]) == 0
    home = load_home(out, snapshots)
    assert home.name == "my-home"  # from the output file's stem
    assert parsed(out.read_text(encoding="utf-8"))["home"]["name"] == "my-home"


def test_cli_skeleton_two_dumps_and_force(tmp_path: Path) -> None:
    second = dump_with(tmp_path, "second.json", name="hub-2")
    out = tmp_path / "home.toml"
    argv = ["skeleton", "--harmony-dump", str(DUMP), str(second), "--out", str(out)]
    assert cli.main(argv) == 0
    assert [h["hub"] for h in status_hubs(out.read_text(encoding="utf-8"))] == ["hub-1", "hub-2"]
    assert cli.main(argv) == 2  # exists
    assert cli.main([*argv, "--force"]) == 0


def test_cli_skeleton_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = Path(skeleton.__file__).resolve().parents[2]
    bad = ["skeleton", "--harmony-dump", str(tmp_path / "missing.json"), "--out", str(tmp_path / "o.toml")]
    assert cli.main(bad) == 2
    assert "missing.json" in capsys.readouterr().err
    inside = ["skeleton", "--harmony-dump", str(DUMP), "--out", str(repo / "private.toml")]
    assert cli.main(inside) == 2
    assert "inside this repository" in capsys.readouterr().err
    assert not (repo / "private.toml").exists()
    unwritable = ["skeleton", "--harmony-dump", str(DUMP), "--out", str(tmp_path)]  # a directory
    assert cli.main([*unwritable, "--force"]) == 2


def test_cli_skeleton_without_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "tomli_w", None)  # `import tomli_w` now raises ImportError
    out = tmp_path / "home.toml"
    assert cli.main(["skeleton", "--harmony-dump", str(DUMP), "--out", str(out)]) == 2
    assert 'pip install -e ".[eval]"' in capsys.readouterr().err
    assert not out.exists()
