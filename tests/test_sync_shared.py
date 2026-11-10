"""scripts/sync_shared.py, against a fake GitHub (a dict of commits), so no network is needed.

This file tests the script itself and stays here; it isn't one of the shared
tests the siblings get.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_shared.py"
OLD = "a" * 40
NEW = "b" * 40

# Two modules, so excluding one leaves the other: "thing" (code and its test) and "extra".
MANIFEST = b"""\
format = 2

[[file]]
source = "src/mcp_shared/thing.py"
module = "thing"
target = "src/__PKG__/thing.py"

[[file]]
source = "templates/tests/test_thing.py"
module = "thing"
target = "tests/test_thing.py"
render = true

[[file]]
source = "src/mcp_shared/extra.py"
module = "extra"
target = "src/__PKG__/extra.py"
"""

# What a sibling still locked to a commit from before modules existed reads.
MANIFEST_V1 = b"""\
format = 1

[[file]]
source = "src/mcp_shared/thing.py"
target = "src/__PKG__/thing.py"
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
        "src/mcp_shared/extra.py": b"EXTRA = True\n",
        "scripts/sync_shared.py": SCRIPT.read_bytes(),
    }


@pytest.fixture
def github(monkeypatch):
    """Two commits (OLD, NEW = main) served through the script's one network function."""
    sync = load_script()
    commits = {OLD: snapshot("1"), NEW: snapshot("2")}
    # main's history is OLD -> NEW. `compare` answers how a commit relates to main, like GitHub's
    # endpoint; "history" is the SHAs on main, "on_main_check" counts those calls.
    # "tags" is name -> SHA, served like GitHub's /tags endpoint (untagged unless a test adds some).
    state = {"main": NEW, "requests": 0, "history": [OLD, NEW], "on_main_check": 0, "tags": {}}

    def fake_get(url: str, accept: str | None = None) -> bytes:
        state["requests"] += 1
        if url.endswith("/commits/main"):
            assert accept == "application/vnd.github.sha"
            return str(state["main"]).encode()
        if "/tags?" in url:
            if state["tags"] is None:
                raise sync.SyncError(f"GET {url}: HTTP 403 Forbidden (rate limited? set GITHUB_TOKEN)")
            tags = dict(state["tags"])
            return json.dumps([{"name": name, "commit": {"sha": sha}} for name, sha in tags.items()]).encode()
        if "/compare/main..." in url:
            state["on_main_check"] += 1
            sha = url.rsplit("...", 1)[1]
            if sha not in commits:
                raise sync.SyncError(f"GET {url}: HTTP 404 Not Found")
            history = list(state["history"])
            status = "identical" if sha == state["main"] else "behind" if sha in history else "ahead"
            return f'{{"status": "{status}"}}'.encode()
        prefix = "https://raw.githubusercontent.com/SDNick484/mcp-server-shared/"
        assert url.startswith(prefix), url
        commit, _, path = url.removeprefix(prefix).partition("/")
        try:
            return commits[commit][path]
        except KeyError:
            raise sync.SyncError(f"GET {url}: HTTP 404 Not Found") from None

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(sync, "_GH_CLI_TOKEN", None)
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
    commits[NEW]["shared.toml"] = MANIFEST.replace(b"format = 2", f"format = {sync.FORMAT + 1}".encode())
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
        (f'repo = "../x"\ncommit = "{OLD}"\npackage = "demo_mcp"\n', "owner/name"),
        (f'repo = "owner/.."\ncommit = "{OLD}"\npackage = "demo_mcp"\n', "owner/name"),
    ],
)
def test_a_bad_lock_is_explained(github, sibling, capsys, lock, message):
    sync, _, _ = github
    (sibling / "shared.lock").write_text(lock)
    assert run(sync, sibling, "--check") == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["--latest"], ["--update", "--check"], ["--source", ".", "--update"], ["--dry-run", "--check"]],
)
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


def test_github_token_falls_back_to_gh_cli_and_caches(monkeypatch):
    sync = load_script()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = 0

    def fake_check_output(cmd, *, text, stderr, timeout):
        nonlocal calls
        calls += 1
        assert cmd == ["gh", "auth", "token"]
        assert text is True
        assert timeout == 5
        return "gh-cli-token\n"

    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(sync.subprocess, "check_output", fake_check_output)
    assert sync._GH_CLI_TOKEN is False
    assert sync._github_token() == "gh-cli-token"
    assert sync._GH_CLI_TOKEN == "gh-cli-token"
    assert sync._github_token() == "gh-cli-token"
    assert calls == 1  # cached on second call

    # Env var takes precedence over cached CLI token.
    monkeypatch.setenv("GH_TOKEN", "env-token")
    assert sync._github_token() == "env-token"


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("no gh"),
        TimeoutError("timed out"),
        RuntimeError("called process error"),
    ],
)
def test_github_token_handles_gh_cli_failure_gracefully(monkeypatch, exc):
    import subprocess

    sync = load_script()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/gh")

    def fail(*_args, **_kwargs):
        if isinstance(exc, TimeoutError):
            raise subprocess.TimeoutExpired(["gh", "auth", "token"], 5)
        if isinstance(exc, RuntimeError):
            raise subprocess.CalledProcessError(1, ["gh", "auth", "token"])
        raise exc

    monkeypatch.setattr(sync.subprocess, "check_output", fail)
    assert sync._github_token() is None
    assert sync._GH_CLI_TOKEN is None


def test_the_lock_round_trips(github, sibling):
    sync, _, _ = github
    lock = sync.read_lock(sibling)
    (sibling / "shared.lock").write_text(lock.dump())
    assert sync.read_lock(sibling) == lock
    assert (sibling / "shared.lock").read_text().startswith("# Which mcp-server-shared commit")


# --- excluding modules ----------------------------------------------------------------------------
def exclude(root: Path, *names: str) -> None:
    """Add `exclude = [...]` to the sibling's lock, as a person would by hand."""
    listed = ", ".join(f'"{name}"' for name in names)
    (root / "shared.lock").write_text((root / "shared.lock").read_text() + f"exclude = [{listed}]\n")


def test_an_excluded_module_is_neither_written_nor_checked(github, sibling, capsys):
    sync, _, _ = github
    exclude(sibling, "thing")
    assert run(sync, sibling) == 0
    # Both of the module's files stay out (code and test), and the other module still comes in.
    assert not (sibling / "src/demo_mcp/thing.py").exists()
    assert not (sibling / "tests/test_thing.py").exists()
    assert (sibling / "src/demo_mcp/extra.py").exists()
    capsys.readouterr()
    assert run(sync, sibling, "--check") == 0  # "missing" doesn't apply to what's excluded
    out = capsys.readouterr().out
    assert "all 1 match" in out
    assert "(excluding thing, per shared.lock)" in out  # so a reader knows why it's 1, not 3


def test_files_of_an_excluded_module_already_here_are_left_alone(github, sibling):
    # Excluding means "not managed", not "delete": the script never removes a sibling's files.
    sync, _, _ = github
    run(sync, sibling)
    (sibling / "src/demo_mcp/thing.py").write_text("now this repo's own\n")
    exclude(sibling, "thing")
    assert run(sync, sibling) == 0 and run(sync, sibling, "--check") == 0
    assert (sibling / "src/demo_mcp/thing.py").read_text() == "now this repo's own\n"


def test_update_keeps_the_exclude_list(github, sibling):
    # The bug the format bump exists for: --update rebuilds the lock, and must not drop exclude.
    sync, _, _ = github
    exclude(sibling, "thing")
    assert run(sync, sibling, "--update") == 0
    lock = sync.read_lock(sibling)
    assert lock.commit == NEW and lock.exclude == ("thing",)
    assert 'exclude = ["thing"]' in (sibling / "shared.lock").read_text()
    assert not (sibling / "src/demo_mcp/thing.py").exists()
    assert run(sync, sibling, "--check", "--latest") == 0


def test_excluding_a_module_the_manifest_lacks_is_an_error(github, sibling, capsys):
    # A typo would otherwise exclude nothing, silently, and the unwanted files would arrive.
    sync, _, _ = github
    exclude(sibling, "thnig")
    assert run(sync, sibling) == 2
    err = capsys.readouterr().err
    assert "'thnig'" in err and "it has: extra, thing" in err
    assert sorted(p.name for p in sibling.rglob("*") if p.is_file()) == ["shared.lock"]  # nothing written


def test_a_format_1_manifest_still_syncs(github, sibling):
    # A sibling locked to a commit from before modules existed keeps working until it updates.
    sync, commits, _ = github
    commits[OLD]["shared.toml"] = MANIFEST_V1
    assert run(sync, sibling) == 0 and run(sync, sibling, "--check") == 0
    assert (sibling / "src/demo_mcp/thing.py").exists()


def test_source_honors_the_siblings_exclude(github, sibling, tmp_path):
    sync, commits, state = github
    shared = tmp_path / "mcp-server-shared"
    for path, content in commits[NEW].items():
        (shared / path).parent.mkdir(parents=True, exist_ok=True)
        (shared / path).write_bytes(content)
    exclude(sibling, "extra")
    assert run(sync, sibling, "--source", str(shared)) == 0
    assert (sibling / "src/demo_mcp/thing.py").exists()
    assert not (sibling / "src/demo_mcp/extra.py").exists()
    assert state["requests"] == 0


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('exclude = "thing"\n', "list of module names"),
        ("exclude = [1]\n", "list of module names"),
        ('exclude = ["no-dashes"]\n', 'module names like "remote"'),
    ],
)
def test_a_bad_exclude_is_explained(github, sibling, capsys, line, message):
    sync, _, _ = github
    (sibling / "shared.lock").write_text((sibling / "shared.lock").read_text() + line)
    assert run(sync, sibling, "--check") == 2
    assert message in capsys.readouterr().err


def test_a_lock_with_exclude_round_trips(github, sibling):
    sync, _, _ = github
    exclude(sibling, "thing", "extra", "thing")
    lock = sync.read_lock(sibling)
    assert lock.exclude == ("thing", "extra")  # repeats dropped, order kept
    (sibling / "shared.lock").write_text(lock.dump())
    assert sync.read_lock(sibling) == lock


def test_a_bad_module_name_in_the_manifest_is_refused(github, sibling, capsys):
    sync, commits, _ = github
    commits[OLD]["shared.toml"] = MANIFEST.replace(b'module = "extra"', b"module = 3")
    assert run(sync, sibling) == 2
    assert 'module must be a name like "remote"' in capsys.readouterr().err


# --- orphan detection -----------------------------------------------------------------------------
def test_orphaned_shared_files_are_reported_while_sibling_and_excluded_files_are_ignored(github, sibling, capsys):
    sync, commits, _ = github
    marker = "Shared by " + "the sibling servers"
    commits[OLD]["src/mcp_shared/extra.py"] = f'"""{marker}."""\nEXTRA = True\n'.encode()
    assert run(sync, sibling) == 0

    # Sibling-owned files in src/<pkg>/ and tests/ (without the shared header marker).
    (sibling / "src/demo_mcp/own_module.py").write_text('"""Sibling-owned module."""\n')
    (sibling / "tests/test_own_module.py").write_text('"""Sibling-owned test."""\n')
    # Previously synced shared test that will also be dropped from the manifest.
    (sibling / "tests/test_old_shared.py").write_text(f'"""{marker}."""\n')

    # Drop "extra" from NEW's manifest so src/demo_mcp/extra.py becomes an orphan on --update.
    manifest_without_extra = MANIFEST.split(b'[[file]]\nsource = "src/mcp_shared/extra.py"')[0]
    commits[NEW]["shared.toml"] = manifest_without_extra
    capsys.readouterr()

    assert run(sync, sibling, "--update") == 0
    out = capsys.readouterr().out
    assert "(shared files: src/demo_mcp/extra.py is no longer in shared.toml; remove manually if unused)" in out
    assert "(shared files: tests/test_old_shared.py is no longer in shared.toml; remove manually if unused)" in out
    assert "own_module" not in out

    # Excluding a module that IS in the manifest must not flag its existing files as orphans.
    commits[NEW]["shared.toml"] = MANIFEST
    (sibling / "src/demo_mcp/thing.py").write_text(f'"""{marker}."""\n')
    (sibling / "tests/test_thing.py").write_text(f'"""{marker}."""\n')
    (sibling / "src/demo_mcp/extra.py").unlink()
    (sibling / "tests/test_old_shared.py").unlink()
    exclude(sibling, "thing")
    capsys.readouterr()

    assert run(sync, sibling) == 0
    out = capsys.readouterr().out
    assert "is no longer in shared.toml" not in out
    assert sync.find_orphans(sibling, "demo_mcp", {"src/demo_mcp/extra.py": b""}, ("thing",)) == []


# --- dry-run --------------------------------------------------------------------------------------
def test_dry_run_sync_and_update_report_changes_and_never_write(github, sibling, capsys):
    sync, commits, _ = github

    # Default sync --dry-run when files are missing: exits 1 and writes nothing.
    assert run(sync, sibling, "--dry-run") == 1
    out = capsys.readouterr().out
    assert "src/demo_mcp/thing.py (added)" in out
    assert "tests/test_thing.py (added)" in out
    assert sorted(p.name for p in sibling.rglob("*") if p.is_file()) == ["shared.lock"]

    # After syncing, --dry-run exits 0.
    assert run(sync, sibling) == 0
    capsys.readouterr()
    assert run(sync, sibling, "--dry-run") == 0
    assert "nothing to change" in capsys.readouterr().out

    # Local edit: --dry-run exits 1, reports (updated), and leaves the file untouched.
    (sibling / "src/demo_mcp/thing.py").write_text("edited locally\n")
    assert run(sync, sibling, "--dry-run") == 1
    assert "src/demo_mcp/thing.py (updated)" in capsys.readouterr().out
    assert (sibling / "src/demo_mcp/thing.py").read_text() == "edited locally\n"

    # Restore clean state at OLD, then test --update --dry-run against NEW.
    assert run(sync, sibling) == 0
    capsys.readouterr()
    assert run(sync, sibling, "--update", "--dry-run") == 1
    out = capsys.readouterr().out
    assert f"{OLD[:7]} -> {NEW[:7]}" in out
    assert "src/demo_mcp/thing.py (updated)" in out
    assert f'commit = "{OLD}"' in (sibling / "shared.lock").read_text()
    assert (sibling / "src/demo_mcp/thing.py").read_text().endswith('VERSION = "1"\n')

    # After actual --update, --update --dry-run exits 0.
    assert run(sync, sibling, "--update") == 0
    capsys.readouterr()
    assert run(sync, sibling, "--update", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"already at {NEW[:7]}" in out
    assert "no files changed" in out

    # Format bump with --dry-run never writes scripts/sync_shared.py.
    commits[NEW]["shared.toml"] = MANIFEST.replace(b"format = 2", f"format = {sync.FORMAT + 1}".encode())
    commits[NEW]["scripts/sync_shared.py"] = b"# newer script\n"
    assert run(sync, sibling, "--update", "--dry-run") == 2
    assert not (sibling / "scripts/sync_shared.py").exists()


def test_dry_run_update_exits_1_when_only_lock_would_move(github, sibling, capsys):
    sync, commits, _ = github
    commits[NEW] = dict(commits[OLD])
    assert run(sync, sibling) == 0
    capsys.readouterr()
    assert run(sync, sibling, "--update", "--dry-run") == 1
    out = capsys.readouterr().out
    assert f"{OLD[:7]} -> {NEW[:7]}" in out
    assert "no files changed" in out
    assert f'commit = "{OLD}"' in (sibling / "shared.lock").read_text()


def test_local_dir_and_write_files_refuse_traversal_outside_root(tmp_path):
    import scripts.sync_shared as sync

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    # Symlink pointing outside
    (root / "escape.txt").symlink_to(outside)
    ld = sync.LocalDir(root)
    with pytest.raises(sync.SyncError, match="resolves outside"):
        ld.read("escape.txt")

    # write_files with target symlink pointing outside
    with pytest.raises(sync.SyncError, match="resolves outside"):
        sync.write_files(root, {"escape.txt": b"overwrite"})


def test_self_update_syntax_check(github, sibling):
    sync, commits, _ = github
    commits[NEW]["shared.toml"] = MANIFEST.replace(b"format = 2", f"format = {sync.FORMAT + 1}".encode())
    commits[NEW]["scripts/sync_shared.py"] = b"def unclosed_syntax(:\n"
    assert run(sync, sibling, "--update") == 2
    assert not (sibling / "scripts/sync_shared.py").exists()


def test_gha_annotation_escaping(monkeypatch, capsys, tmp_path):
    import scripts.sync_shared as sync

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    files = {"target%name\r\n.py": b"content"}
    found = [("target%name\r\n.py", "differs")]
    sync._print_report(tmp_path, files, found, "test", "fix")
    out = capsys.readouterr().out
    assert "::error file=target%25name%0D%0A.py::target%25name%0D%0A.py differs from test. Fix: fix" in out


# --- the lock should point at a commit on main -------------------------------------------------
def test_a_lock_on_mains_history_passes_quietly(github, sibling, capsys):
    sync, _, state = github
    assert run(sync, sibling) == 0  # OLD: an ancestor of main
    assert run(sync, sibling, "--check") == 0
    assert "warning" not in capsys.readouterr().err
    assert state["on_main_check"] == 2  # once per run; --check --latest and --update don't ask


def test_a_lock_on_a_squash_merged_branch_head_is_warned_about_but_still_syncs(github, sibling, capsys):
    """The case that happened: three siblings pinned a PR branch's head, which the squash merge replaced and
    the branch deletion orphaned. raw.githubusercontent.com still served it, so nothing noticed."""
    sync, commits, state = github
    branch_head = "c" * 40
    commits[branch_head] = snapshot("2")  # same bytes as main, so --check passes on content
    state["history"] = [OLD, NEW]  # ...but main never pointed at it
    (sibling / "shared.lock").write_text(
        f'repo = "SDNick484/mcp-server-shared"\ncommit = "{branch_head}"\npackage = "demo_mcp"\n'
    )
    assert run(sync, sibling) == 0  # the sync itself still works: the files exist
    err = capsys.readouterr().err
    assert f"warning: the locked commit {branch_head[:7]} is not on SDNick484/mcp-server-shared main (ahead)" in err
    assert "--update" in err
    assert run(sync, sibling, "--check") == 0  # advisory: the content matches, so --check still passes
    assert "not on SDNick484/mcp-server-shared main" in capsys.readouterr().err


def test_a_lock_github_no_longer_has_is_explained(github, sibling, capsys):
    sync, _, _ = github
    gone = "d" * 40
    (sibling / "shared.lock").write_text(
        f'repo = "SDNick484/mcp-server-shared"\ncommit = "{gone}"\npackage = "demo_mcp"\n'
    )
    assert run(sync, sibling) == 2  # the files can't be fetched at all
    # The fetch failure is the error; the ancestry warning isn't reached (nothing to add to a 404)
    assert "HTTP 404" in capsys.readouterr().err


def test_the_on_main_check_is_advisory_when_the_api_is_unavailable(github, sibling, capsys, monkeypatch):
    sync, _, _ = github
    real_get = sync._get

    def rate_limited(url: str, accept: str | None = None) -> bytes:
        if "/compare/" in url:
            raise sync.SyncError(f"GET {url}: HTTP 403 rate limit exceeded (rate limited? set GITHUB_TOKEN)")
        return real_get(url, accept)

    monkeypatch.setattr(sync, "_get", rate_limited)
    assert run(sync, sibling, "--check") == 1  # nothing synced yet: that's the real answer
    assert run(sync, sibling) == 0
    err = capsys.readouterr().err
    assert "couldn't check whether the locked commit is on main" in err and "rate limit" in err


def test_lock_is_on_main_handles_odd_answers(github, monkeypatch):
    sync, _, _ = github
    monkeypatch.setattr(sync, "_get", lambda url, accept=None: b"<html>")
    assert "isn't JSON" in sync.lock_is_on_main("o/r", OLD)
    monkeypatch.setattr(sync, "_get", lambda url, accept=None: b'{"status": "weird"}')
    assert "unexpected compare status 'weird'" in sync.lock_is_on_main("o/r", OLD)
    monkeypatch.setattr(sync, "_get", lambda url, accept=None: b'{"status": "diverged"}')
    assert "(diverged)" in sync.lock_is_on_main("o/r", OLD)


# --- unknown lock keys ---------------------------------------------------------------------------
def test_unknown_lock_keys_are_warned_about(github, sibling, capsys):
    """`excludes = [...]` (a typo) excluded nothing, silently; now it says so."""
    sync, _, _ = github
    (sibling / "shared.lock").write_text(
        f'repo = "SDNick484/mcp-server-shared"\ncommit = "{OLD}"\npackage = "demo_mcp"\nexcludes = ["extra"]\n'
    )
    assert run(sync, sibling) == 0
    err = capsys.readouterr().err
    assert "shared.lock has keys this script doesn't use: excludes" in err
    assert "it reads repo, commit, version, package, exclude" in err
    assert (sibling / "src/demo_mcp/extra.py").exists()  # the typo didn't exclude anything, as warned


# --- atomic writes -----------------------------------------------------------------------------
def test_writes_are_atomic_and_leave_no_temp_file(github, sibling):
    sync, _, _ = github
    assert run(sync, sibling) == 0
    assert not [p for p in sibling.rglob(".*.tmp-*")], "temporary files left behind"
    assert run(sync, sibling, "--update") == 0  # the lock is written the same way
    assert not [p for p in sibling.rglob(".*.tmp-*")]


def test_a_failed_write_leaves_the_old_file_and_cleans_up(github, sibling, monkeypatch):
    sync, _, _ = github
    assert run(sync, sibling) == 0
    target = sibling / "src/demo_mcp/thing.py"
    before = target.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(sync.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        sync.write_atomic(target, b"new content")
    assert target.read_bytes() == before  # the old file is intact...
    assert not [p for p in sibling.rglob(".*.tmp-*")]  # ...and the temp file was removed


# --- uncommitted local changes to a copy are not silently overwritten --------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_sibling(github, sibling):
    """A synced sibling that is a git checkout with everything committed."""
    sync, _, _ = github
    assert run(sync, sibling) == 0
    git(sibling, "init", "-q")
    git(sibling, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".")
    git(sibling, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "synced")
    return sibling


def test_update_refuses_to_overwrite_an_uncommitted_local_edit(github, git_sibling, capsys):
    sync, _, _ = github
    copy = git_sibling / "src/demo_mcp/thing.py"
    copy.write_text(copy.read_text() + "# debugging\n")  # the edit nobody should make, but people do
    assert run(sync, git_sibling, "--update") == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite 1 shared copy with uncommitted local changes: src/demo_mcp/thing.py" in err
    assert "--force" in err and "git stash" in err
    assert copy.read_text().endswith("# debugging\n")  # the edit is still there
    assert f'commit = "{OLD}"' in (git_sibling / "shared.lock").read_text()  # and the lock didn't move


def test_force_overwrites_and_says_so(github, git_sibling, capsys):
    sync, _, _ = github
    copy = git_sibling / "src/demo_mcp/thing.py"
    copy.write_text(copy.read_text() + "# debugging\n")
    assert run(sync, git_sibling, "--update", "--force") == 0
    out = capsys.readouterr().out
    assert "src/demo_mcp/thing.py (updated, had uncommitted local changes; overwritten (--force))" in out
    assert copy.read_text().endswith('VERSION = "2"\n')


def test_dry_run_reports_the_local_edit_instead_of_refusing(github, git_sibling, capsys):
    sync, _, _ = github
    copy = git_sibling / "src/demo_mcp/thing.py"
    copy.write_text(copy.read_text() + "# debugging\n")
    assert run(sync, git_sibling, "--update", "--dry-run") == 1
    assert "thing.py (updated, had uncommitted local changes)" in capsys.readouterr().out
    assert copy.read_text().endswith("# debugging\n")


def test_a_committed_local_edit_is_overwritten_as_before(github, git_sibling):
    """Committed means recoverable from history, and the drift job would have flagged it; the sync's job
    is to bring the copy back in line."""
    sync, _, _ = github
    copy = git_sibling / "src/demo_mcp/thing.py"
    copy.write_text(copy.read_text() + "# committed edit\n")
    git(git_sibling, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "edit")
    assert run(sync, git_sibling, "--update") == 0
    assert copy.read_text().endswith('VERSION = "2"\n')


def test_a_staged_edit_counts_as_uncommitted(github, git_sibling):
    sync, _, _ = github
    copy = git_sibling / "src/demo_mcp/thing.py"
    copy.write_text(copy.read_text() + "# staged\n")
    git(git_sibling, "add", "src/demo_mcp/thing.py")
    assert run(sync, git_sibling, "--update") == 2


def test_an_unchanged_or_missing_copy_is_never_dirty(github, git_sibling):
    """Only copies that both differ from upstream *and* have local changes are refused: a missing file has
    nothing to lose, and a modified file that happens to match upstream exactly needs no write."""
    sync, _, _ = github
    (git_sibling / "src/demo_mcp/extra.py").unlink()  # missing: deleted in the work tree, uncommitted
    assert run(sync, git_sibling) == 0  # written back, no refusal
    assert (git_sibling / "src/demo_mcp/extra.py").exists()


def test_not_a_git_repo_syncs_as_before(github, sibling):
    sync, _, _ = github
    assert run(sync, sibling) == 0
    copy = sibling / "src/demo_mcp/thing.py"
    copy.write_text("hand edited\n")  # no git here, so nothing to compare the edit against
    assert run(sync, sibling, "--update") == 0
    assert copy.read_text().endswith('VERSION = "2"\n')


def test_without_git_installed_syncs_as_before(github, git_sibling, monkeypatch):
    sync, _, _ = github
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)
    (git_sibling / "src/demo_mcp/thing.py").write_text("hand edited\n")
    assert run(sync, git_sibling, "--update") == 0


@pytest.mark.parametrize("args", [("--force", "--check"), ("--force", "--dry-run")])
def test_force_with_a_non_writing_mode_is_refused(github, sibling, args):
    sync, _, _ = github
    with pytest.raises(SystemExit):
        run(sync, sibling, *args)


# --- version tags ----------------------------------------------------------------------------------
def test_update_records_the_tag_on_main_and_check_shows_it(github, sibling, capsys):
    sync, _, state = github
    state["tags"] = {"v0.1.0": NEW, "v0.0.9": OLD, "not-a-version": NEW}
    assert run(sync, sibling, "--update") == 0
    lock = (sibling / "shared.lock").read_text()
    assert f'commit = "{NEW}"\nversion = "v0.1.0"\n' in lock
    assert f"shared.lock: {OLD[:7]} -> {NEW[:7]} (v0.1.0);" in capsys.readouterr().out
    assert run(sync, sibling, "--check") == 0
    assert f"all 3 match mcp-server-shared at {NEW[:7]} v0.1.0 (locked)" in capsys.readouterr().out


def test_an_untagged_main_writes_no_version_and_drops_a_stale_one(github, sibling):
    sync, _, state = github
    (sibling / "shared.lock").write_text(
        f'repo = "SDNick484/mcp-server-shared"\ncommit = "{OLD}"\nversion = "v0.0.9"\npackage = "demo_mcp"\n'
    )
    assert run(sync, sibling, "--update") == 0
    lock = (sibling / "shared.lock").read_text()
    assert "version" not in lock.replace("# ", "").split("repo =", 1)[1]  # the header may mention it


def test_tagging_the_locked_commit_later_is_picked_up_by_update(github, sibling, capsys):
    sync, _, state = github
    run(sync, sibling, "--update")
    state["tags"] = {"v0.1.0": NEW}
    assert run(sync, sibling, "--update") == 0
    assert f"shared.lock: already at {NEW[:7]} (v0.1.0)" in capsys.readouterr().out
    assert 'version = "v0.1.0"' in (sibling / "shared.lock").read_text()


def test_of_two_tags_on_one_commit_the_highest_release_wins(github, sibling):
    sync, _, state = github
    state["tags"] = {"v1.2.0-rc1": NEW, "v1.10.0": NEW, "v1.9.0": NEW, "v1.10.0-rc2": NEW}
    run(sync, sibling, "--update")
    assert 'version = "v1.10.0"' in (sibling / "shared.lock").read_text()


def test_a_failed_tag_lookup_warns_and_keeps_what_it_can(github, sibling, capsys):
    sync, _, state = github
    state["tags"] = {"v0.1.0": NEW}
    run(sync, sibling, "--update")
    state["tags"] = None  # rate limited from here on
    assert run(sync, sibling, "--update") == 0  # same commit: the version it had is kept
    assert 'version = "v0.1.0"' in (sibling / "shared.lock").read_text()
    assert "couldn't look up a version tag" in capsys.readouterr().err
    state["main"] = OLD  # a different commit: its tag is unknown, so none is written
    state["history"] = [NEW, OLD]
    assert run(sync, sibling, "--update") == 0
    assert "version" not in (sibling / "shared.lock").read_text().split("repo =", 1)[1]


def test_a_lock_with_version_round_trips_and_a_bad_one_is_refused(github, sibling, capsys):
    sync, _, _ = github
    text = f'repo = "SDNick484/mcp-server-shared"\ncommit = "{OLD}"\nversion = "v0.1.0"\npackage = "demo_mcp"\n'
    (sibling / "shared.lock").write_text(text)
    lock = sync.read_lock(sibling)
    assert lock.version == "v0.1.0"
    (sibling / "shared.lock").write_text(lock.dump())
    assert sync.read_lock(sibling) == lock
    assert "doesn't use" not in capsys.readouterr().err  # a known key: no warning
    for bad in ['"0.1.0"', '"v1\\" injected = \\"x"', "1"]:
        (sibling / "shared.lock").write_text(text.replace('"v0.1.0"', bad))
        assert run(sync, sibling, "--check") == 2
        assert "version must be a tag like" in capsys.readouterr().err


def test_version_tag_handles_odd_answers(github, monkeypatch):
    sync, _, _ = github
    monkeypatch.setattr(sync, "_get", lambda url, accept=None: b"<html>")
    with pytest.raises(sync.SyncError, match="isn't JSON"):
        sync.version_tag("SDNick484/mcp-server-shared", NEW)
    monkeypatch.setattr(sync, "_get", lambda url, accept=None: b'{"message": "Not Found"}')
    assert sync.version_tag("SDNick484/mcp-server-shared", NEW) is None
