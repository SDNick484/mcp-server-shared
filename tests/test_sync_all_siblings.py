"""Tests for scripts/sync_all_siblings.py."""

from __future__ import annotations

import importlib.util
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


def test_update_runs_pytest_in_updated_siblings_and_propagates_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")
    make_sibling(tmp_path, "mcp-server-beta", "beta_mcp")

    pytest_runs: list[str] = []
    fail_beta_pytest = False

    def fake_run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            pytest_runs.append(cwd.name)
            if fail_beta_pytest and cwd.name == "mcp-server-beta":
                return subprocess.CompletedProcess(cmd, 1, stdout="1 failed\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")
        assert "--update" in cmd
        if cwd.name == "mcp-server-alpha":
            out = f"shared.lock: {COMMIT_A[:7]} -> {COMMIT_B[:7]}; src/alpha_mcp/thing.py (updated)\n"
        else:
            out = f"shared.lock: already at {COMMIT_B[:7]}; no files changed\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

    # Only alpha was updated, so pytest runs only in alpha.
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 0
    assert pytest_runs == ["mcp-server-alpha"]
    out = capsys.readouterr().out
    assert "mcp-server-alpha: src/alpha_mcp/thing.py (updated)" in out
    assert "mcp-server-alpha: pytest passed" in out
    assert "mcp-server-beta: nothing to change" in out

    # When beta also updates and its pytest fails, update exits non-zero.
    pytest_runs.clear()
    fail_beta_pytest = True

    def fake_run_both_updated(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            return fake_run_cmd(cmd, cwd=cwd)
        out = f"shared.lock: {COMMIT_A[:7]} -> {COMMIT_B[:7]}; src/pkg/thing.py (updated)\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_both_updated)
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 1
    assert pytest_runs == ["mcp-server-alpha", "mcp-server-beta"]
    err = capsys.readouterr().err
    assert "mcp-server-beta: pytest failed" in err


def test_update_exits_nonzero_when_sync_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    mod = load_script()
    make_sibling(tmp_path, "mcp-server-alpha", "alpha_mcp")

    monkeypatch.setattr(
        mod,
        "_run_cmd",
        lambda cmd, *, cwd: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="sync_shared.py: network error\n"),
    )
    assert mod.main(["update", "--parent-dir", str(tmp_path), "--test"]) == 1
    assert "mcp-server-alpha: sync failed" in capsys.readouterr().err
