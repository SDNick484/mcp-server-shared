"""Tests for the routing eval's tool snapshots (evals/routing_eval/snapshot.py)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from evals.routing_eval.snapshot import (
    ServerSnapshot,
    SnapshotError,
    ToolSpec,
    load_snapshots,
    server_name,
    snapshot_sibling,
    unannotated,
    write_snapshot,
)

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "evals" / "snapshots"


def tool(name: str, annotations: dict[str, object] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        output_schema=None,
        annotations=annotations,
    )


def make_snapshot() -> ServerSnapshot:
    return ServerSnapshot(
        server="hubx",
        package="hubx_mcp",
        commit="a" * 40,
        instructions="Prefer activities.",
        tools=(tool("hubx_b", {"readOnlyHint": True}), tool("hubx_a", None)),
    )


def test_server_name_strips_prefix() -> None:
    assert server_name(Path("/src/mcp-server-harmony")) == "harmony"
    assert server_name(Path("/src/mcp-server-steam-backlog")) == "steam-backlog"


def test_snapshot_round_trip(tmp_path: Path) -> None:
    snap = make_snapshot()
    path = write_snapshot(snap, tmp_path)
    assert path == tmp_path / "hubx.json"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert [t["name"] for t in json.loads(text)["tools"]] == ["hubx_a", "hubx_b"]
    loaded = load_snapshots(tmp_path)["hubx"]
    assert loaded == ServerSnapshot(
        server=snap.server,
        package=snap.package,
        commit=snap.commit,
        instructions=snap.instructions,
        tools=tuple(sorted(snap.tools, key=lambda t: t.name)),
    )


def test_load_snapshots_missing_dir_is_empty(tmp_path: Path) -> None:
    assert load_snapshots(tmp_path / "nope") == {}


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [({"readOnlyHint": True}, True), ({"readOnlyHint": False}, False), ({}, False), (None, False)],
)
def test_is_read_only_when_read_only_hint_true(annotations: dict[str, object] | None, expected: bool) -> None:
    assert tool("t", annotations).is_read is expected


def test_namespaced() -> None:
    assert make_snapshot().namespaced("hubx_a") == "mcp__hubx__hubx_a"


def test_unannotated_lists_missing() -> None:
    assert unannotated(make_snapshot()) == ["hubx_a"]


def test_namespaced_names_fit_api_limit() -> None:
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not files:
        pytest.skip("no committed snapshots yet")
    for snap in load_snapshots(SNAPSHOT_DIR).values():
        for spec in snap.tools:
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", snap.namespaced(spec.name)), spec.name


def git_repo(path: Path) -> str:
    path.mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=path, check=True, env=env)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def make_sibling(tmp_path: Path, script_body: str) -> tuple[Path, str]:
    sibling = tmp_path / "mcp-server-hubx"
    commit = git_repo(sibling)
    (sibling / "shared.lock").write_text('package = "hubx_mcp"\ncommit = "deadbeef"\n', encoding="utf-8")
    python = sibling / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\n{script_body}\n", encoding="utf-8")
    python.chmod(0o755)
    return sibling, commit


def test_snapshot_sibling_runs_extractor(tmp_path: Path) -> None:
    payload = {
        "instructions": "Prefer activities.",
        "tools": [
            {
                "name": "hubx_get_status",
                "description": "Status.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
            {"name": "hubx_noop", "description": "No annotations.", "inputSchema": {"type": "object"}},
        ],
    }
    sibling, commit = make_sibling(tmp_path, f"cat <<'EOF'\n{json.dumps(payload)}\nEOF")
    snap = snapshot_sibling(sibling)
    assert snap.server == "hubx"
    assert snap.package == "hubx_mcp"
    assert snap.commit == commit
    assert snap.instructions == "Prefer activities."
    assert [t.name for t in snap.tools] == ["hubx_get_status", "hubx_noop"]
    assert snap.tools[0].is_read
    assert snap.tools[0].annotations == {"readOnlyHint": True}
    assert snap.tools[1].annotations is None
    assert snap.tools[1].output_schema is None


def test_snapshot_sibling_passes_package_to_extractor(tmp_path: Path) -> None:
    sibling, _ = make_sibling(tmp_path, 'echo "{\\"instructions\\": null, \\"tools\\": []}"; test "$3" = hubx_mcp')
    snap = snapshot_sibling(sibling)
    assert snap.instructions is None
    assert snap.tools == ()


def test_snapshot_sibling_without_venv_raises(tmp_path: Path) -> None:
    sibling = tmp_path / "mcp-server-hubx"
    git_repo(sibling)
    (sibling / "shared.lock").write_text('package = "hubx_mcp"\ncommit = "x"\n', encoding="utf-8")
    with pytest.raises(SnapshotError, match=r"mcp-server-hubx.*--setup-venvs"):
        snapshot_sibling(sibling)


def test_snapshot_sibling_extractor_failure_names_sibling_and_stderr(tmp_path: Path) -> None:
    sibling, _ = make_sibling(tmp_path, "echo 'ImportError: boom' >&2; exit 1")
    with pytest.raises(SnapshotError, match=r"mcp-server-hubx.*ImportError: boom"):
        snapshot_sibling(sibling)


def test_snapshot_sibling_bad_json_raises(tmp_path: Path) -> None:
    sibling, _ = make_sibling(tmp_path, "echo not-json")
    with pytest.raises(SnapshotError, match="mcp-server-hubx"):
        snapshot_sibling(sibling)


def test_output_schema_round_trips(tmp_path: Path) -> None:
    spec = ToolSpec("hubx_a", "d", {"type": "object"}, {"type": "object", "properties": {}}, {"readOnlyHint": True})
    snap = ServerSnapshot("hubx", "hubx_mcp", "c" * 40, None, (spec,))
    write_snapshot(snap, tmp_path)
    assert load_snapshots(tmp_path)["hubx"] == snap


def test_snapshot_sibling_unrunnable_python_raises(tmp_path: Path) -> None:
    sibling, _ = make_sibling(tmp_path, "true")
    (sibling / ".venv" / "bin" / "python").chmod(0o644)
    with pytest.raises(SnapshotError, match="mcp-server-hubx.*did not run"):
        snapshot_sibling(sibling)


def test_snapshot_sibling_not_a_git_repo_raises(tmp_path: Path) -> None:
    sibling = tmp_path / "mcp-server-hubx"
    python = sibling / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho \'{"instructions": null, "tools": []}\'\n', encoding="utf-8")
    python.chmod(0o755)
    (sibling / "shared.lock").write_text('package = "hubx_mcp"\ncommit = "x"\n', encoding="utf-8")
    with pytest.raises(SnapshotError, match="mcp-server-hubx.*git commit"):
        snapshot_sibling(sibling)
