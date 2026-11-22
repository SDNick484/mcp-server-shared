"""deploy/alpine/commit.sh, run with sh and a fake git on PATH, so no network or pip is needed.

This file tests the script itself and stays here; the siblings get only the script.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_shared import remote

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "alpine" / "commit.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "SDNick484/mcp-server-demo"
DEFAULT = f"git+https://github.com/{REPO}"


@pytest.fixture
def git(tmp_path):
    """A fake git: logs its arguments to git.log, then prints $FAKE_GIT_OUT and exits $FAKE_GIT_STATUS."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$FAKE_GIT_LOG"\n'
        'printf \'%s\' "${FAKE_GIT_OUT-}"\nexit "${FAKE_GIT_STATUS:-0}"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "git.log"

    def run(
        call: str, out: str = "", status: int = 0, *, real_git: bool = False, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": os.environ["PATH"] if real_git else f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GIT_LOG": str(log),
            "FAKE_GIT_OUT": out,
            "FAKE_GIT_STATUS": str(status),
        }
        script = f"set -eu; . {shlex.quote(str(SCRIPT))}; {call}"
        return subprocess.run(["sh", "-c", script], env=env, cwd=cwd, capture_output=True, text=True, check=False)

    run.log = log  # type: ignore[attr-defined]
    return run


def calls(git) -> list[str]:
    return git.log.read_text().splitlines() if git.log.exists() else []


@pytest.mark.parametrize("src", [DEFAULT, f"{DEFAULT}.git"])
def test_default_source_pins_main(git, src):
    res = git(f"commit_source {shlex.quote(src)} {REPO}", out=f"{SHA}\trefs/heads/main\n")
    assert (res.returncode, res.stdout) == (0, f"{DEFAULT}@{SHA}\n{SHA}\n"), res.stderr
    assert calls(git) == [f"ls-remote https://github.com/{REPO} refs/heads/main"]


def test_ls_remote_failure_stops(git):
    res = git(f"commit_source {DEFAULT} {REPO}; echo still-here", status=128)
    assert res.returncode == 1 and "still-here" not in res.stdout
    assert "ls-remote" in res.stderr


@pytest.mark.parametrize("out", ["nope\n", "", f"{SHA[:7]}\trefs/heads/main\n", f"{SHA.upper()}\trefs/heads/main\n"])
def test_ls_remote_garbage_stops(git, out):
    res = git(f"commit_source {DEFAULT} {REPO}", out=out)
    assert res.returncode == 1 and res.stdout == ""
    assert calls(git) == [f"ls-remote https://github.com/{REPO} refs/heads/main"]
    assert "main" in res.stderr


def git_repo(path: Path) -> str:
    """A real repo with one commit; its HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(path)]
    subprocess.run([*g, "init", "-q"], check=True)
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    return subprocess.run([*g, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def test_checkout_uses_its_head(git, tmp_path):
    head = git_repo(tmp_path / "checkout")
    src = tmp_path / "checkout"
    res = git(f"commit_source {shlex.quote(str(src))} {REPO}", real_git=True)
    assert (res.returncode, res.stdout) == (0, f"{src}\n{head}\n"), res.stderr


def test_relative_checkout_uses_its_head(git, tmp_path):
    head = git_repo(tmp_path / "checkout")
    res = git(f"commit_source . {REPO}", real_git=True, cwd=tmp_path / "checkout")
    assert (res.returncode, res.stdout) == (0, f".\n{head}\n"), res.stderr


def test_a_plain_directory_inside_another_repo_gives_no_commit(git, tmp_path):
    git_repo(tmp_path / "outer")
    src = tmp_path / "outer" / "sub"
    src.mkdir()
    res = git(f"commit_source {shlex.quote(str(src))} {REPO}", real_git=True)
    assert (res.returncode, res.stdout) == (0, f"{src}\n\n"), res.stderr


def test_checkout_without_git_gives_no_commit(git, tmp_path):
    res = git(f"commit_source {shlex.quote(str(tmp_path))} {REPO}", status=128)
    assert (res.returncode, res.stdout) == (0, f"{tmp_path}\n\n")


@pytest.mark.parametrize(
    "src", ["mcp-server-demo==1.0", f"{DEFAULT}@v1.0", "git+https://github.com/someone-else/mcp-server-demo"]
)
def test_other_spec_gives_no_commit(git, src):
    res = git(f"commit_source {shlex.quote(src)} {REPO}")
    assert (res.returncode, res.stdout) == (0, f"{src}\n\n")
    assert calls(git) == []


def test_write_commit_stamps_sha_and_repo(git, tmp_path):
    res = git(f"write_commit {shlex.quote(str(tmp_path))} {SHA} {REPO}")
    assert res.returncode == 0, res.stderr
    stamp = tmp_path / "COMMIT"
    assert stamp.read_text() == f"{SHA}\n{REPO}\n"
    assert stat.S_IMODE(stamp.stat().st_mode) == 0o644
    assert not (tmp_path / "COMMIT.tmp").exists()


def test_write_commit_without_sha_removes_a_stale_stamp(git, tmp_path):
    (tmp_path / "COMMIT").write_text(f"{SHA}\n{REPO}\n")
    res = git(f"write_commit {shlex.quote(str(tmp_path))} '' {REPO}")
    assert res.returncode == 0 and not (tmp_path / "COMMIT").exists()


@pytest.mark.parametrize("sha", [SHA[:7], SHA.upper(), f"{SHA} x"])
def test_write_commit_rejects_anything_but_40_hex(git, tmp_path, sha):
    res = git(f"write_commit {shlex.quote(str(tmp_path))} {shlex.quote(sha)} {REPO}")
    assert res.returncode == 0 and not (tmp_path / "COMMIT").exists()


def test_write_commit_leaves_out_a_bad_repo(git, tmp_path):
    git(f"write_commit {shlex.quote(str(tmp_path))} {SHA} 'not a repo'")
    assert (tmp_path / "COMMIT").read_text() == f"{SHA}\n"


def test_stamp_round_trips_through_running_commit(git, tmp_path):
    git(f"write_commit {shlex.quote(str(tmp_path))} {SHA} {REPO}")
    assert remote.running_commit(env={remote.COMMIT_FILE_ENV: str(tmp_path / "COMMIT")}) == {
        "commit": SHA,
        "repo": REPO,
    }


def test_the_manifest_syncs_commit_sh(monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_shared", ROOT / "scripts" / "sync_shared.py")
    assert spec and spec.loader
    sync = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "sync_shared", sync)  # dataclasses look their module up here
    spec.loader.exec_module(sync)
    _, entries = sync.parse_manifest((ROOT / "shared.toml").read_bytes())
    entry = next(e for e in entries if e.source == "deploy/alpine/commit.sh")
    assert (entry.target, entry.module, entry.render) == ("deploy/alpine/commit.sh", "deploy", False)
