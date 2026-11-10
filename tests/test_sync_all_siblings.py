"""Tests for scripts/sync_all_siblings.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_all_siblings.py"
SYNC_SHARED_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_shared.py"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40

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
"""


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_all_siblings", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_all_siblings"] = module
    spec.loader.exec_module(module)
    return module


def make_sibling(parent: Path, name: str, package: str, commit: str = COMMIT_A) -> Path:
    repo = parent / name
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "sync_shared.py").write_bytes(SYNC_SHARED_SCRIPT.read_bytes())
    (repo / "shared.lock").write_text(
        f'repo = "SDNick484/mcp-server-shared"\ncommit = "{commit}"\npackage = "{package}"\n',
        encoding="utf-8",
    )
    return repo


def make_source_checkout(parent: Path, version: str = "1") -> Path:
    shared = parent / "mcp-server-shared"
    (shared / "src" / "mcp_shared").mkdir(parents=True, exist_ok=True)
    (shared / "templates" / "tests").mkdir(parents=True, exist_ok=True)
    (shared / "shared.toml").write_bytes(MANIFEST)
    (shared / "src" / "mcp_shared" / "thing.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    (shared / "templates" / "tests" / "test_thing.py").write_text(
        f'from __PKG__.thing import VERSION\n\n\ndef test_version():\n    assert VERSION == "{version}"\n',
        encoding="utf-8",
    )
    return shared


def test_discover_siblings_filters_shared_and_unlocked_dirs(tmp_path: Path):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-webos", "webos_mcp")
    make_sibling(tmp_path, "mcp-server-onkyo", "onkyo_mcp")

    # mcp-server-shared itself must be ignored even if a shared.lock exists inside it.
    shared = tmp_path / "mcp-server-shared"
    shared.mkdir()
    (shared / "shared.lock").write_text(f'commit = "{COMMIT_A}"\npackage = "mcp_shared"\n', encoding="utf-8")

    # Directories without shared.lock, non-mcp-server dirs, and files are ignored.
    (tmp_path / "mcp-server-nolock").mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "shared.lock").write_text(f'commit = "{COMMIT_A}"\npackage = "other"\n', encoding="utf-8")
    (tmp_path / "mcp-server-file").write_text("not a dir\n", encoding="utf-8")

    found = mod.discover_siblings(tmp_path)
    assert [p.name for p in found] == ["mcp-server-onkyo", "mcp-server-webos"]
    assert mod.discover_siblings(tmp_path / "nonexistent") == []


def test_status_default_and_latest_summary_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp", COMMIT_A)
    make_sibling(tmp_path, "mcp-server-beta", "beta_mcp", COMMIT_B)

    recorded: list[tuple[str, list[str]]] = []

    def fake_run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        recorded.append((cwd.name, list(cmd)))
        return subprocess.CompletedProcess(cmd, 0, stdout="shared files: all 2 match\n", stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

    # Default subcommand is `status`.
    rc = mod.main(["--parent-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcp-server-alpha" in out and "alpha_mcp" in out and COMMIT_A[:7] in out and "in sync" in out
    assert "mcp-server-beta" in out and "beta_mcp" in out and COMMIT_B[:7] in out
    assert [name for name, _ in recorded] == ["mcp-server-alpha", "mcp-server-beta"]
    assert all("--check" in cmd and "--latest" not in cmd for _, cmd in recorded)

    # `status --latest` forwards `--latest` and exits 1 when any sibling differs.
    recorded.clear()

    def fake_run_cmd_drift(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        recorded.append((cwd.name, list(cmd)))
        code = 1 if cwd.name == "mcp-server-beta" else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd_drift)
    rc_latest = mod.main(["status", "--parent-dir", str(tmp_path), "--latest"])
    assert rc_latest == 1
    out_latest = capsys.readouterr().out
    assert "differs" in out_latest
    assert all("--check" in cmd and "--latest" in cmd for _, cmd in recorded)


def test_status_shows_the_locks_version_next_to_the_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    tagged = make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp", COMMIT_A)
    with (tagged / "shared.lock").open("a", encoding="utf-8") as lock:
        lock.write('version = "v0.1.0"\n')
    make_sibling(tmp_path, "mcp-server-beta", "beta_mcp", COMMIT_B)
    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(mod, "_run_cmd", lambda cmd, *, cwd: ok)
    assert mod.main(["status", "--parent-dir", str(tmp_path)]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[2:]}
    assert f"{COMMIT_A[:7]} v0.1.0" in rows["mcp-server-alpha"]
    assert "v0." not in rows["mcp-server-beta"]


def test_status_reports_error_on_invalid_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    bad = tmp_path / "mcp-server-broken"
    bad.mkdir()
    (bad / "shared.lock").write_text("not = [valid toml", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "_run_cmd",
        lambda cmd, *, cwd: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="invalid TOML"),
    )
    assert mod.main(["status", "--parent-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "mcp-server-broken" in out and "invalid" in out and "error" in out


def test_update_with_local_source_and_dry_run_end_to_end(tmp_path: Path, capsys):
    mod = load_script()
    shared = make_source_checkout(tmp_path, version="1")
    alpha = make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    beta = make_sibling(tmp_path, "mcp-server-beta", "beta_mcp")

    # Dry-run reports files that would be added without writing them.
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--source", str(shared), "--dry-run"]) == 0
    dry_out = capsys.readouterr().out
    assert "mcp-server-alpha (dry-run): src/alpha_mcp/thing.py (added), tests/test_thing.py (added)" in dry_out
    assert "mcp-server-beta (dry-run): src/beta_mcp/thing.py (added), tests/test_thing.py (added)" in dry_out
    assert not (alpha / "src" / "alpha_mcp" / "thing.py").exists()
    assert not (beta / "src" / "beta_mcp" / "thing.py").exists()

    # Real update writes the rendered files into each sibling.
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--source", str(shared)]) == 0
    up_out = capsys.readouterr().out
    assert "mcp-server-alpha: src/alpha_mcp/thing.py (added), tests/test_thing.py (added)" in up_out
    assert "mcp-server-beta: src/beta_mcp/thing.py (added), tests/test_thing.py (added)" in up_out
    assert (alpha / "src" / "alpha_mcp" / "thing.py").read_text(encoding="utf-8") == 'VERSION = "1"\n'
    assert "from alpha_mcp.thing import VERSION" in (alpha / "tests" / "test_thing.py").read_text(encoding="utf-8")

    # Second update when already in sync reports nothing to change.
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--source", str(shared)]) == 0
    noop_out = capsys.readouterr().out
    assert "mcp-server-alpha: nothing to change" in noop_out
    assert "mcp-server-beta: nothing to change" in noop_out


def _is_probe(cmd: Sequence[str]) -> bool:
    return "-c" in cmd and "import mypy, pytest, ruff" in cmd


def _check_label(cmd: Sequence[str]) -> str | None:
    """Which CI check a command is (by its `-m` module), or None for anything else."""
    if "-m" not in cmd:
        return None
    module = cmd[list(cmd).index("-m") + 1]
    if module == "ruff":
        return "ruff format" if "format" in cmd else "ruff check"
    return module if module in ("mypy", "pytest") else None


def test_update_runs_ci_checks_in_updated_siblings_and_propagates_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    make_sibling(tmp_path, "mcp-server-beta", "beta_mcp")

    checks_run: list[tuple[str, str]] = []
    failing: dict[str, str] = {}  # sibling -> check that fails there

    def fake_run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if _is_probe(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        label = _check_label(cmd)
        if label is not None:
            checks_run.append((cwd.name, label))
            code = 1 if failing.get(cwd.name) == label else 0
            return subprocess.CompletedProcess(cmd, code, stdout=f"{label} output\n", stderr="")
        assert "--update" in cmd
        if cwd.name == "mcp-server-alpha" or failing:
            out = f"shared.lock: {COMMIT_A[:7]} -> {COMMIT_B[:7]}; src/pkg/thing.py (updated)\n"
        else:
            out = f"shared.lock: already at {COMMIT_B[:7]}; no files changed\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

    # Only alpha was updated, so its checks run, all four, in CI's order; beta's don't.
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 0
    assert checks_run == [("mcp-server-alpha", c) for c in ("ruff check", "ruff format", "mypy", "pytest")]
    out = capsys.readouterr().out
    assert "mcp-server-alpha: src/pkg/thing.py (updated)" in out
    assert "mcp-server-alpha: checks passed (ruff check, ruff format, mypy, pytest)" in out
    assert "mcp-server-beta: nothing to change" in out

    # Both update, beta fails ruff (a long line, say): exit 1, and its checks stop there.
    checks_run.clear()
    failing["mcp-server-beta"] = "ruff check"
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 1
    assert ("mcp-server-beta", "ruff check") in checks_run
    assert ("mcp-server-beta", "mypy") not in checks_run
    assert "mcp-server-beta: ruff check failed" in capsys.readouterr().err


def test_update_test_reports_missing_tools_before_syncing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    synced: list[str] = []

    def fake_run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if _is_probe(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named 'ruff'")
        synced.append(cwd.name)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 1
    assert synced == []
    assert "ruff/mypy/pytest not installed" in capsys.readouterr().err


def test_update_exits_nonzero_when_sync_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")

    def fake_run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if _is_probe(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="sync_shared.py: network error\n")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 1
    assert "mcp-server-alpha: sync failed" in capsys.readouterr().err


# --- clone and update --pr/--commit/--push: real git repos, fake sync, checks and gh -----


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate git from the developer's config (signing, hooks, identity) for real repos."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_git_sibling(tmp_path: Path, name: str, package: str) -> Path:
    """A sibling checkout in tmp_path/src, cloned from a bare origin in tmp_path/remotes."""
    seed = make_sibling(tmp_path / "seed", name, package)
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial")
    (tmp_path / "remotes").mkdir(exist_ok=True)
    git(tmp_path / "remotes", "clone", "-q", "--bare", str(seed), f"{name}.git")
    (tmp_path / "src").mkdir(exist_ok=True)
    git(tmp_path / "src", "clone", "-q", str(tmp_path / "remotes" / f"{name}.git"), name)
    return tmp_path / "src" / name


def remote_branches(tmp_path: Path, name: str) -> list[str]:
    return git(tmp_path / "remotes" / f"{name}.git", "branch", "--format=%(refname:short)").split()


class FakeTools:
    """Stands in for `sync_shared.py --update` (adds a file, moves the lock) and the CI checks."""

    def __init__(self) -> None:
        self.failing: dict[str, str] = {}  # sibling -> the check that fails there
        self.synced: list[str] = []

    def __call__(self, cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if _is_probe(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        label = _check_label(cmd)
        if label is not None:
            failed = self.failing.get(cwd.name) == label
            out = "E501 Line too long\n" if failed else "3 passed in 0.10s\n"
            return subprocess.CompletedProcess(cmd, 1 if failed else 0, stdout=out, stderr="")
        assert "--update" in cmd
        self.synced.append(cwd.name)
        lock = (cwd / "shared.lock").read_text(encoding="utf-8")
        package = lock.split('package = "')[1].split('"')[0]
        (cwd / "src" / package).mkdir(parents=True, exist_ok=True)
        (cwd / "src" / package / "thing.py").write_text('VERSION = "2"\n', encoding="utf-8")
        (cwd / "shared.lock").write_text(lock.replace(COMMIT_A, COMMIT_B), encoding="utf-8")
        out = f"shared.lock: {COMMIT_A[:7]} -> {COMMIT_B[:7]}; src/{package}/thing.py (added)\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


class FakeGh:
    """Stands in for the GitHub CLI: open PRs keyed by (sibling, head branch)."""

    def __init__(self) -> None:
        self.open: dict[tuple[str, str], str] = {}
        self.created: list[list[str]] = []
        self.repos: list[str] = []
        self.logged_in = True

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        a = list(args)
        ok = subprocess.CompletedProcess(a, 0, stdout="", stderr="")
        if a[:2] == ["auth", "status"]:
            return ok if self.logged_in else subprocess.CompletedProcess(a, 1, stdout="", stderr="not logged in")
        if a[:2] == ["repo", "list"]:
            return subprocess.CompletedProcess(a, 0, stdout="\n".join(self.repos) + "\n", stderr="")
        assert cwd is not None
        if a[:2] == ["pr", "list"] and "--head" in a:
            url = self.open.get((cwd.name, a[a.index("--head") + 1]), "")
            return subprocess.CompletedProcess(a, 0, stdout=url + "\n" if url else "", stderr="")
        if a[:2] == ["pr", "list"]:
            current = a[-1].split('!= "')[1].split('"')[0]
            urls = [
                u for (n, b), u in self.open.items() if n == cwd.name and b.startswith("sync-shared-") and b != current
            ]
            return subprocess.CompletedProcess(a, 0, stdout="".join(u + "\n" for u in urls), stderr="")
        if a[:2] == ["pr", "create"]:
            self.created.append(a)
            url = f"https://github.example/{cwd.name}/pull/{len(self.created)}"
            self.open[(cwd.name, a[a.index("--head") + 1])] = url
            return subprocess.CompletedProcess(a, 0, stdout=url + "\n", stderr="")
        raise AssertionError(f"unexpected gh call: {a}")


@pytest.fixture
def portfolio(tmp_path: Path, git_env: None, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, FakeTools, FakeGh]:
    mod = load_script()
    tools, gh = FakeTools(), FakeGh()
    monkeypatch.setattr(mod, "_run_cmd", tools)
    monkeypatch.setattr(mod, "_gh", gh)
    monkeypatch.setattr(mod, "resolve_shared_main", lambda: COMMIT_B)
    return mod, tools, gh


def test_update_pr_opens_a_pr_only_for_siblings_that_pass(tmp_path: Path, portfolio, capsys):
    mod, tools, gh = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")  # passes everything
    beta = make_git_sibling(tmp_path, "mcp-server-beta", "beta_mcp")  # fails ruff check
    make_git_sibling(tmp_path, "mcp-server-gamma", "gamma_mcp")  # this sync's PR already open
    delta = make_git_sibling(tmp_path, "mcp-server-delta", "delta_mcp")  # someone's work in progress
    tools.failing["mcp-server-beta"] = "ruff check"
    branch = f"sync-shared-{COMMIT_B[:7]}"
    gh.open[("mcp-server-gamma", branch)] = "https://github.example/mcp-server-gamma/pull/7"
    (delta / "notes.txt").write_text("wip\n", encoding="utf-8")

    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr"]) == 1
    out = capsys.readouterr().out

    # alpha: one commit on the sync branch, pushed, with a PR; main untouched everywhere.
    assert remote_branches(tmp_path, "mcp-server-alpha") == ["main", branch]
    remote = tmp_path / "remotes" / "mcp-server-alpha.git"
    assert (
        git(remote, "log", "-1", "--format=%s|%ae", branch).strip()
        == f"Sync shared to {COMMIT_B[:7]}|{mod.COMMIT_EMAIL}"
    )
    assert git(remote, "log", "-1", "--format=%s", "main").strip() == "initial"
    assert git(alpha, "branch", "--show-current").strip() == "main"
    assert git(alpha, "status", "--porcelain") == ""
    [create] = gh.created
    assert create[create.index("--head") + 1] == branch and create[create.index("--base") + 1] == "main"
    body = create[create.index("--body") + 1]
    assert f"compare/{COMMIT_A}...{COMMIT_B}" in body
    assert "ruff check, ruff format, mypy and pytest pass (`3 passed in 0.10s`)" in body
    assert "- `src/alpha_mcp/thing.py`" in body and "- `shared.lock`" in body

    # beta: no branch, no PR, and the sync undone, including the file it added.
    assert remote_branches(tmp_path, "mcp-server-beta") == ["main"]
    assert git(beta, "status", "--porcelain") == ""
    assert not (beta / "src" / "beta_mcp" / "thing.py").exists()

    # gamma and delta never synced: one already proposed, one not clean.
    assert tools.synced == ["mcp-server-alpha", "mcp-server-beta"]
    assert (delta / "notes.txt").exists()

    table = out[out.index("SIBLING") :]
    assert "mcp-server-alpha" in table and "PR https://github.example/mcp-server-alpha/pull/1" in table
    assert "ruff check failed; no PR, sync undone, tree clean" in table
    assert "PR already open: https://github.example/mcp-server-gamma/pull/7" in table
    assert "working tree dirty" in table


def test_update_pr_keep_failed_leaves_the_sync_in_place(tmp_path: Path, portfolio, capsys):
    mod, tools, _ = portfolio
    beta = make_git_sibling(tmp_path, "mcp-server-beta", "beta_mcp")
    tools.failing["mcp-server-beta"] = "pytest"
    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr", "--keep-failed"]) == 1
    assert sorted(git(beta, "status", "--porcelain").splitlines()) == [" M shared.lock", "?? src/"]
    assert "(--keep-failed)" in capsys.readouterr().out


def test_update_pr_redoes_stranded_sync_commits_and_flags_superseded_prs(tmp_path: Path, portfolio, capsys):
    mod, _, gh = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    # An earlier --commit run's commit, never pushed.
    (alpha / "old.txt").write_text("old sync\n", encoding="utf-8")
    git(alpha, "add", "-A")
    git(alpha, "commit", "-q", "-m", "Sync shared to 1234567")
    stale = git(alpha, "rev-parse", "--short", "HEAD").strip()
    gh.open[("mcp-server-alpha", "sync-shared-1234567")] = "https://github.example/mcp-server-alpha/pull/3"

    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr"]) == 0
    out = capsys.readouterr().out
    assert f"dropped 1 unpushed sync commit(s) (was {stale}; undo: git reset --hard {stale})" in out
    assert not (alpha / "old.txt").exists()
    assert "(supersedes open https://github.example/mcp-server-alpha/pull/3)" in out
    log = git(tmp_path / "remotes" / "mcp-server-alpha.git", "log", "--format=%s", f"sync-shared-{COMMIT_B[:7]}")
    assert log.splitlines() == [f"Sync shared to {COMMIT_B[:7]}", "initial"]


def test_update_pr_refuses_a_branch_with_other_local_commits(tmp_path: Path, portfolio, capsys):
    mod, tools, _ = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    (alpha / "feature.txt").write_text("mine\n", encoding="utf-8")
    git(alpha, "add", "-A")
    git(alpha, "commit", "-q", "-m", "Add a feature")
    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr"]) == 1
    assert "main has 1 local commit(s) not on origin" in capsys.readouterr().out
    assert tools.synced == []
    assert git(alpha, "log", "-1", "--format=%s").strip() == "Add a feature"


def test_update_commit_and_push_modes(tmp_path: Path, portfolio, capsys):
    mod, _, gh = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    beta = make_git_sibling(tmp_path, "mcp-server-beta", "beta_mcp")

    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--commit", "--only", "alpha"]) == 0
    assert git(alpha, "log", "-1", "--format=%s").strip() == f"Sync shared to {COMMIT_B[:7]}"
    assert git(alpha, "rev-list", "--count", "origin/main..main").strip() == "1"  # not pushed
    assert "committed on main (not pushed)" in capsys.readouterr().out

    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--push", "--only", "mcp-server-beta"]) == 0
    remote = tmp_path / "remotes" / "mcp-server-beta.git"
    assert git(remote, "log", "-1", "--format=%s", "main").strip() == f"Sync shared to {COMMIT_B[:7]}"
    assert git(beta, "status", "--porcelain") == ""
    assert gh.created == []


def test_update_pr_needs_gh_and_checks_tools_before_syncing(tmp_path: Path, portfolio, monkeypatch, capsys):
    mod, tools, gh = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")

    gh.logged_in = False
    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr"]) == 2
    assert "gh auth login" in capsys.readouterr().err

    gh.logged_in = True

    def no_tools(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if _is_probe(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named 'mypy'")
        return tools(cmd, cwd=cwd)

    monkeypatch.setattr(mod, "_run_cmd", no_tools)
    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr"]) == 1
    assert "use --setup-venvs); nothing changed" in capsys.readouterr().out
    assert tools.synced == [] and git(alpha, "status", "--porcelain") == ""

    # --no-test skips the probe and the checks; the PR says so.
    assert mod.main(["update", "--parent-dir", str(tmp_path / "src"), "--pr", "--no-test"]) == 0
    body = gh.created[-1][gh.created[-1].index("--body") + 1]
    assert "not run locally (--no-test); CI runs them" in body


def test_clone_clones_missing_siblings_and_fast_forwards_the_rest(tmp_path: Path, portfolio, capsys):
    mod, _, gh = portfolio
    alpha = make_git_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    make_git_sibling(tmp_path, "mcp-server-beta", "beta_mcp")
    beta_checkout = tmp_path / "src" / "mcp-server-beta"
    subprocess.run(["rm", "-rf", str(beta_checkout)], check=True)  # beta: not cloned yet
    # alpha: origin moved on since it was cloned.
    pusher = tmp_path / "pusher"
    git(tmp_path, "clone", "-q", str(tmp_path / "remotes" / "mcp-server-alpha.git"), "pusher")
    (pusher / "new.txt").write_text("new\n", encoding="utf-8")
    git(pusher, "add", "-A")
    git(pusher, "commit", "-q", "-m", "upstream change")
    git(pusher, "push", "-q", "origin", "main")
    gh.repos = ["mcp-server-alpha", "mcp-server-beta", "mcp-server-shared", "unrelated-repo"]

    rc = mod.main(["clone", "--parent-dir", str(tmp_path / "src"), "--remote-base", str(tmp_path / "remotes")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcp-server-alpha: fast-forwarded" in out and "mcp-server-beta: cloned" in out
    assert "mcp-server-shared" not in out and "unrelated-repo" not in out
    assert (alpha / "new.txt").exists() and (beta_checkout / "shared.lock").exists()

    assert mod.main(["clone", "--parent-dir", str(tmp_path / "src"), "--remote-base", str(tmp_path / "remotes")]) == 0
    assert "mcp-server-alpha: up to date" in capsys.readouterr().out


def test_clone_falls_back_to_the_known_sibling_list(portfolio, capsys):
    mod, _, gh = portfolio
    gh.repos = []
    assert mod.list_remote_siblings() == list(mod.KNOWN_SIBLINGS)
    assert "built-in sibling list" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["update", "--pr", "--source", "."], "--source can't"),
        (["update", "--pr", "--dry-run"], "--dry-run doesn't go with"),
        (["update", "--no-test"], "only go with --pr/--commit/--push"),
        (["update", "--keep-failed"], "only go with --pr/--commit/--push"),
        (["update", "--pr", "--push"], "not allowed with argument"),
    ],
)
def test_update_rejects_contradictory_flags(argv: list[str], message: str, capsys):
    mod = load_script()
    with pytest.raises(SystemExit) as exc:
        mod.parse_args(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_select_siblings_accepts_short_and_full_names(tmp_path: Path):
    mod = load_script()
    paths = [tmp_path / n for n in ("mcp-server-alpha", "mcp-server-beta", "mcp-server-gamma")]
    assert [p.name for p in mod.select_siblings(paths, "alpha, mcp-server-gamma")] == [
        "mcp-server-alpha",
        "mcp-server-gamma",
    ]
    assert mod.select_siblings(paths, None) == paths
