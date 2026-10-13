"""scripts/sync_shared.py, against a fake GitHub (a dict of commits), so no network is needed.

This file tests the script itself and stays here; it isn't one of the shared
tests the siblings get.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_shared.py"
OLD = "a" * 40
NEW = "b" * 40

MANIFEST = b"""\
format = 1

[[file]]
source = "src/mcp_shared/thing.py"
target = "src/__PKG__/thing.py"

[[file]]
source = "templates/tests/test_thing.py"
target = "tests/test_thing.py"
render = true
"""


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_shared", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_shared"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def snapshot(version: str, manifest: bytes = MANIFEST) -> dict[str, bytes]:
    """One commit of mcp-server-shared."""
    return {
        "shared.toml": manifest,
        "src/mcp_shared/thing.py": f'"""Shared. No __PKG__ here is replaced."""\nVERSION = "{version}"\n'.encode(),
        "templates/tests/test_thing.py": b"from __PKG__.thing import VERSION\n",
        "scripts/sync_shared.py": SCRIPT.read_bytes(),
    }


@pytest.fixture
def github(monkeypatch):
    """Two commits (OLD, NEW = main) served through the script's one network function."""
    sync = load_script()
    commits = {OLD: snapshot("1"), NEW: snapshot("2")}
    state = {"main": NEW, "requests": 0}

    def fake_get(url: str, accept: str | None = None) -> bytes:
        state["requests"] += 1
        if url.endswith("/commits/main"):
            assert accept == "application/vnd.github.sha"
            return str(state["main"]).encode()
        prefix = "https://raw.githubusercontent.com/SDNick484/mcp-server-shared/"
        assert url.startswith(prefix), url
        commit, _, path = url.removeprefix(prefix).partition("/")
        try:
            return commits[commit][path]
        except KeyError:
            raise sync.SyncError(f"GET {url}: HTTP 404 Not Found") from None

    monkeypatch.setattr(sync, "_get", fake_get)
    return sync, commits, state


@pytest.fixture
def sibling(tmp_path: Path) -> Path:
    root = tmp_path / "mcp-server-demo"
    root.mkdir()
    (root / "shared.lock").write_text(f'repo = "SDNick484/mcp-server-shared"\ncommit = "{OLD}"\npackage = "demo_mcp"\n')
    return root


def run(sync: ModuleType, root: Path, *args: str) -> int:
    return int(sync.main([*args, "--root", str(root)]))


def test_sync_writes_the_locked_commit_and_fills_in_the_package(github, sibling):
    sync, _, _ = github
    assert run(sync, sibling) == 0
    # The target path gets the package name; a plain file is copied byte for byte (its __PKG__ kept)...
    assert (sibling / "src/demo_mcp/thing.py").read_text().endswith('VERSION = "1"\n')
    assert "__PKG__" in (sibling / "src/demo_mcp/thing.py").read_text()
    # ...and a render = true file has __PKG__ replaced in its contents.
    assert (sibling / "tests/test_thing.py").read_text() == "from demo_mcp.thing import VERSION\n"


def test_check_passes_when_in_sync_and_names_each_edited_or_missing_file(github, sibling, capsys):
    sync, _, _ = github
    run(sync, sibling)
    assert run(sync, sibling, "--check") == 0
    (sibling / "src/demo_mcp/thing.py").write_text("edited here\n")
    (sibling / "tests/test_thing.py").unlink()
    assert run(sync, sibling, "--check") == 1
    out = capsys.readouterr().out
    assert "src/demo_mcp/thing.py: differs  -> fix: python scripts/sync_shared.py" in out
    assert "tests/test_thing.py: missing  -> fix: python scripts/sync_shared.py" in out
    assert "+edited here" in out  # the diff, so CI's log shows what changed
    assert run(sync, sibling) == 0 and run(sync, sibling, "--check") == 0


def test_check_still_fails_when_its_output_is_cut_short(github, sibling, monkeypatch):
    # `--check | head` closes the pipe early; that used to end in a BrokenPipeError traceback.
    sync, _, _ = github
    run(sync, sibling)
    (sibling / "src/demo_mcp/thing.py").write_text("edited here\n")

    class ClosedPipe:
        def write(self, text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert run(sync, sibling, "--check") == 1


def test_check_writes_nothing(github, sibling):
    sync, _, _ = github
    assert run(sync, sibling, "--check") == 1
    assert sorted(p.name for p in sibling.rglob("*") if p.is_file()) == ["shared.lock"]


def test_update_moves_the_lock_to_main_and_check_latest_follows_main(github, sibling, capsys):
    sync, _, _ = github
    run(sync, sibling)
    assert run(sync, sibling, "--check", "--latest") == 1  # main changed thing.py
    assert "fix: python scripts/sync_shared.py --update" in capsys.readouterr().out
    assert run(sync, sibling, "--update") == 0
    assert f'commit = "{NEW}"' in (sibling / "shared.lock").read_text()
    assert (sibling / "src/demo_mcp/thing.py").read_text().endswith('VERSION = "2"\n')
    assert run(sync, sibling, "--check") == 0 and run(sync, sibling, "--check", "--latest") == 0


def test_check_latest_passes_when_main_moved_but_no_shared_file_did(github, sibling, capsys):
    sync, commits, _ = github
    commits[NEW] = dict(commits[OLD])  # e.g. only mcp-server-shared's README changed
    run(sync, sibling)
    assert run(sync, sibling, "--check", "--latest") == 0
    assert "no shared file changed" in capsys.readouterr().out


def test_a_failed_update_leaves_the_lock_alone(github, sibling):
    sync, commits, _ = github
    run(sync, sibling)
    del commits[NEW]["templates/tests/test_thing.py"]  # main is broken: a listed file is missing
    assert run(sync, sibling, "--update") == 2
    assert f'commit = "{OLD}"' in (sibling / "shared.lock").read_text()
    assert (sibling / "src/demo_mcp/thing.py").read_text().endswith('VERSION = "1"\n')  # nothing half-written


def test_a_newer_manifest_format_brings_in_the_newer_script_first(github, sibling, capsys):
    sync, commits, _ = github
    commits[NEW]["shared.toml"] = MANIFEST.replace(b"format = 1", b"format = 2")
    commits[NEW]["scripts/sync_shared.py"] = b"# the newer script\n"
    run(sync, sibling)
    assert run(sync, sibling, "--update") == 2
    assert (sibling / "scripts/sync_shared.py").read_bytes() == b"# the newer script\n"
    assert "Run the same command again" in capsys.readouterr().err
    assert f'commit = "{OLD}"' in (sibling / "shared.lock").read_text()


def test_source_reads_a_local_checkout(github, sibling, tmp_path):
    sync, commits, state = github
    shared = tmp_path / "mcp-server-shared"
    for path, content in commits[NEW].items():
        (shared / path).parent.mkdir(parents=True, exist_ok=True)
        (shared / path).write_bytes(content)
    assert run(sync, sibling, "--source", str(shared), "--package", "other_mcp") == 0
    assert (sibling / "tests/test_thing.py").read_text() == "from other_mcp.thing import VERSION\n"
    assert run(sync, sibling, "--source", str(shared), "--package", "other_mcp", "--check") == 0
    assert state["requests"] == 0  # never touched the network


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", ""])
def test_manifest_paths_must_stay_inside_the_repo(github, sibling, bad):
    sync, commits, _ = github
    commits[OLD]["shared.toml"] = MANIFEST.replace(b'"tests/test_thing.py"', f'"{bad}"'.encode())
    assert run(sync, sibling) == 2
    assert not (sibling.parent / "escape.py").exists()


@pytest.mark.parametrize(
    ("lock", "message"),
    [
        ('commit = "abc"\npackage = "demo_mcp"\n', "full 40-character SHA"),
        (f'commit = "{OLD}"\npackage = "demo-mcp"\n', "Python identifier"),
        (f'commit = "{OLD}"\n', "missing 'package'"),
    ],
)
def test_a_bad_lock_is_explained(github, sibling, capsys, lock, message):
    sync, _, _ = github
    (sibling / "shared.lock").write_text(lock)
    assert run(sync, sibling, "--check") == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--latest"], ["--update", "--check"], ["--source", ".", "--update"]])
def test_contradictory_flags_are_refused(github, sibling, args):
    sync, _, _ = github
    with pytest.raises(SystemExit) as exc:
        run(sync, sibling, *args)
    assert exc.value.code == 2


def test_the_token_goes_to_the_api_only(monkeypatch):
    # raw.githubusercontent.com answers 404 to a token that can't see the repo, even a public one.
    sync = load_script()
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    sent: dict[str, str | None] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def fake_urlopen(request, timeout):  # noqa: ARG001
        sent[request.full_url] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    sync._get("https://api.github.com/repos/o/r/commits/main")
    sync._get("https://raw.githubusercontent.com/o/r/abc/f.py")
    assert sent == {
        "https://api.github.com/repos/o/r/commits/main": "Bearer t0ken",
        "https://raw.githubusercontent.com/o/r/abc/f.py": None,
    }


def test_the_lock_round_trips(github, sibling):
    sync, _, _ = github
    lock = sync.read_lock(sibling)
    (sibling / "shared.lock").write_text(lock.dump())
    assert sync.read_lock(sibling) == lock
    assert (sibling / "shared.lock").read_text().startswith("# Which mcp-server-shared commit")
