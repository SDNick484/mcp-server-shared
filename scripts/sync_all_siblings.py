"""Check, update, or propose shared-file syncs across all sibling MCP server checkouts at once.

Lives only in mcp-server-shared (not listed in shared.toml). Discovers sibling
directories matching ``mcp-server-*`` (excluding ``mcp-server-shared``) that
contain a ``shared.lock`` file under ``--parent-dir`` (default: the parent of
this repo's root):

    python scripts/sync_all_siblings.py [status] [--latest]
    python scripts/sync_all_siblings.py clone [--https]
    python scripts/sync_all_siblings.py update [--source DIR] [--test] [--dry-run]
    python scripts/sync_all_siblings.py update --pr [--setup-venvs] [--only NAMES]

``update --pr`` runs the whole loop for each sibling: fetch and fast-forward its clean
default branch, ``sync_shared.py --update``, the checks its CI runs (ruff check, ruff
format --check, mypy, pytest), then one commit on a ``sync-shared-<sha>`` branch and a
pull request (needs the GitHub CLI, ``gh``). Only a sibling that passes every check gets
a PR; a sync that fails its checks is undone so the next run starts from a clean tree.
``--commit`` stops after the local commit and ``--push`` pushes it to the default branch
instead of opening a PR.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARENT_DIR = REPO_ROOT.parent
OWN_SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_shared.py"
SHARED_REPO_NAME = "mcp-server-shared"
LOCK_FILE = "shared.lock"

# The GitHub account the siblings live under. Its noreply address is the commit e-mail
# the portfolio uses, so sync commits carry it whatever the local git config says.
OWNER = "SDNick484"
COMMIT_EMAIL = f"{OWNER}@users.noreply.github.com"
SSH_REMOTE_BASE = f"git@github.com:{OWNER}"
HTTPS_REMOTE_BASE = f"https://github.com/{OWNER}"
# Used by `clone` when `gh repo list` isn't available (not installed, logged out, offline).
KNOWN_SIBLINGS = (
    "mcp-server-batocera",
    "mcp-server-harmony",
    "mcp-server-misterfpga",
    "mcp-server-onkyo",
    "mcp-server-pixelcade",
    "mcp-server-shieldtv",
    "mcp-server-sofabaton",
    "mcp-server-steam-backlog",
    "mcp-server-webos",
)

SYNC_BRANCH_PREFIX = "sync-shared-"
SYNC_SUBJECT_PREFIX = "Sync shared to "
# The steps of every sibling's CI test job, in its order. `python -B` keeps them from
# writing bytecode into the tree; `-p no:cacheprovider` does the same for .pytest_cache.
CI_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff check", ("-m", "ruff", "check", ".")),
    ("ruff format", ("-m", "ruff", "format", "--check", ".")),
    ("mypy", ("-m", "mypy")),
    ("pytest", ("-m", "pytest", "-q", "-p", "no:cacheprovider")),
)


@dataclass(frozen=True)
class SiblingInfo:
    path: Path
    package: str
    commit: str
    repo: str = f"{OWNER}/{SHARED_REPO_NAME}"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def short_commit(self) -> str:
        return self.commit[:7] if len(self.commit) >= 7 else self.commit


@dataclass(frozen=True)
class StatusRow:
    sibling: str
    package: str
    commit: str
    status: str
    ok: bool


class GitError(RuntimeError):
    """A git command failed; the message is git's own."""


class SiblingError(RuntimeError):
    """A sibling can't go through `update --pr` as it stands; the message says why."""


def discover_siblings(parent_dir: Path) -> list[Path]:
    """Return sorted sibling repo directories (`mcp-server-*` with `shared.lock`, excluding `mcp-server-shared`)."""
    if not parent_dir.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(parent_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("mcp-server-") or entry.name == SHARED_REPO_NAME:
            continue
        if (entry / LOCK_FILE).is_file():
            found.append(entry)
    return found


def select_siblings(siblings: Sequence[Path], only: str | None) -> list[Path]:
    """Keep the siblings named in `only` (comma-separated, `webos` or `mcp-server-webos`)."""
    if not only:
        return list(siblings)
    wanted = {
        n if n.startswith("mcp-server-") else f"mcp-server-{n}" for n in (s.strip() for s in only.split(",")) if n
    }
    return [s for s in siblings if s.name in wanted]


def read_sibling_info(sibling: Path) -> SiblingInfo:
    """Read package and commit metadata from a sibling's `shared.lock`."""
    lock_path = sibling / LOCK_FILE
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        package = str(data.get("package") or "?")
        commit = str(data.get("commit") or "?")
        repo = str(data.get("repo") or f"{OWNER}/{SHARED_REPO_NAME}")
    except (OSError, tomllib.TOMLDecodeError):
        package = "?"
        commit = "invalid"
        repo = f"{OWNER}/{SHARED_REPO_NAME}"
    return SiblingInfo(path=sibling, package=package, commit=commit, repo=repo)


def _run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Run git in `repo` and return its stdout; raise GitError with git's message on failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise GitError(f"git {args[0]}: {detail}")
    return proc.stdout


def _gh(args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the GitHub CLI (it picks the repo from `cwd`'s origin remote)."""
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["gh", *args], 127, stdout="", stderr="gh: command not found")


def _sync_cmd(sibling: Path, extra_args: Sequence[str]) -> list[str]:
    sibling_script = sibling / "scripts" / "sync_shared.py"
    if sibling_script.is_file():
        return [sys.executable, "scripts/sync_shared.py", *extra_args]
    return [sys.executable, str(OWN_SYNC_SCRIPT), "--root", str(sibling), *extra_args]


def _extract_modified_files(output: str) -> list[str]:
    """Extract modified file entries like `src/pkg/remote.py (updated)` from `sync_shared.py` output."""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("(shared files:"):
            continue
        if ";" in line:
            _, _, tail = line.partition(";")
            tail = tail.strip()
            if tail and tail != "no files changed":
                return [item.strip() for item in tail.split(",") if item.strip()]
            return []
        if ":" in line:
            _, _, tail = line.partition(":")
            tail = tail.strip()
            if tail and tail != "nothing to change":
                return [item.strip() for item in tail.split(",") if item.strip()]
            return []
    return []


def _sibling_was_updated(output: str, modified: list[str]) -> bool:
    if modified:
        return True
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "->" in line and "shared.lock" in line:
            return True
    return False


def format_status_table(rows: Sequence[StatusRow]) -> str:
    headers = ("SIBLING", "PACKAGE", "COMMIT", "STATUS")
    widths = [
        max(len(headers[0]), *(len(r.sibling) for r in rows)),
        max(len(headers[1]), *(len(r.package) for r in rows)),
        max(len(headers[2]), *(len(r.commit) for r in rows)),
        max(len(headers[3]), *(len(r.status) for r in rows)),
    ]
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()
    sep_line = "  ".join("-" * w for w in widths)
    body_lines = [
        "  ".join(
            (
                r.sibling.ljust(widths[0]),
                r.package.ljust(widths[1]),
                r.commit.ljust(widths[2]),
                r.status,
            )
        ).rstrip()
        for r in rows
    ]
    return "\n".join([header_line, sep_line, *body_lines])


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.rstrip().splitlines())


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


# --- status -------------------------------------------------------------------------------


def run_status(parent_dir: Path, *, latest: bool = False, only: str | None = None) -> int:
    siblings = select_siblings(discover_siblings(parent_dir), only)
    if not siblings:
        print(f"No sibling repos with {LOCK_FILE} found in {parent_dir}")
        return 0

    check_args = ["--check", *(["--latest"] if latest else [])]
    rows: list[StatusRow] = []
    for sibling in siblings:
        info = read_sibling_info(sibling)
        proc = _run_cmd(_sync_cmd(sibling, check_args), cwd=sibling)
        if proc.returncode == 0:
            status = "in sync"
            ok = True
        elif proc.returncode == 1:
            status = "differs"
            ok = False
        else:
            status = "error"
            ok = False
        rows.append(
            StatusRow(
                sibling=info.name,
                package=info.package,
                commit=info.short_commit,
                status=status,
                ok=ok,
            )
        )

    print(format_status_table(rows))
    return 0 if all(r.ok for r in rows) else 1


# --- git plumbing for clone and update --pr -------------------------------------------------


def default_branch(repo: Path) -> str:
    """The branch origin/HEAD points at (as cloned), else `main`."""
    try:
        ref = _git(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").strip()
    except GitError:
        return "main"
    return ref.removeprefix("origin/") or "main"


def changed_paths(repo: Path) -> list[str]:
    """Every modified or untracked (non-ignored) path in the working tree."""
    lines = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    return [line[3:] for line in lines if line.strip()]


def refresh_checkout(repo: Path, *, drop_stranded: bool) -> tuple[str, str]:
    """Fetch, then fast-forward `repo`'s clean default branch. Returns (branch, what happened).

    A sync commit must hold nothing but sync output, so anything that isn't a clean default
    branch is refused (SiblingError) rather than stashed or rebased: that's someone's work.
    The one exception is local commits that are all earlier sync commits (a `--commit` run, a
    failed push): with `drop_stranded` they're reset away, since the coming sync recreates
    the same change from current main.
    """
    _git(repo, "fetch", "--quiet", "--prune", "origin")
    branch = default_branch(repo)
    if changed_paths(repo):
        raise SiblingError(f"working tree dirty; commit, or set it aside with: git -C {repo} stash -u")
    try:
        current = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
    except GitError:
        current = "(detached HEAD)"
    if current != branch:
        raise SiblingError(f"on {current}, not {branch}")

    note = ""
    ahead = int(_git(repo, "rev-list", "--count", f"origin/{branch}..HEAD").strip())
    if ahead:
        subjects = _git(repo, "log", "--format=%s", f"origin/{branch}..HEAD").splitlines()
        if any(not s.startswith(SYNC_SUBJECT_PREFIX) for s in subjects):
            raise SiblingError(f"{branch} has {ahead} local commit(s) not on origin; push or move them first")
        tip = _git(repo, "rev-parse", "--short", "HEAD").strip()
        if not drop_stranded:
            return branch, f"{ahead} unpushed sync commit(s) at {tip}; `update --pr` redoes them"
        _git(repo, "reset", "--quiet", "--hard", f"origin/{branch}")
        note = f"dropped {ahead} unpushed sync commit(s) (was {tip}; undo: git reset --hard {tip}); "

    before = _git(repo, "rev-parse", "HEAD").strip()
    try:
        _git(repo, "merge", "--ff-only", "--quiet", f"origin/{branch}")
    except GitError:
        raise SiblingError(f"{branch} has diverged from origin/{branch}") from None
    after = _git(repo, "rev-parse", "HEAD").strip()
    return branch, note + ("fast-forwarded" if before != after else "up to date")


def discard_sync(repo: Path, *, keep_failed: bool) -> str:
    """Undo an uncommitted sync, given refresh_checkout() left the tree clean before it.

    Tracked files go back to HEAD and untracked, non-ignored ones are removed; .venv and
    other ignored files stay. Returns the clause the summary uses.
    """
    if keep_failed:
        return f"changes left in {repo} (--keep-failed)"
    _git(repo, "restore", "--staged", "--worktree", "--", ".")
    _git(repo, "clean", "-fdq")
    return "sync undone, tree clean"


def resolve_shared_main() -> str | None:
    """The SHA mcp-server-shared main points at on origin, or None when it can't be read."""
    try:
        out = _git(REPO_ROOT, "ls-remote", "origin", "refs/heads/main")
    except GitError:
        return None
    sha = out.split()[0] if out.split() else ""
    return sha if len(sha) == 40 else None


# --- clone ----------------------------------------------------------------------------------


def list_remote_siblings() -> list[str]:
    """The owner's non-archived `mcp-server-*` repos (minus this one), via gh; else KNOWN_SIBLINGS."""
    proc = _gh(["repo", "list", OWNER, "--limit", "200", "--no-archived", "--json", "name", "-q", ".[].name"])
    names = (
        sorted(n for n in proc.stdout.split() if n.startswith("mcp-server-") and n != SHARED_REPO_NAME)
        if proc.returncode == 0
        else []
    )
    if not names:
        print("(gh repo list unavailable; using the built-in sibling list)", file=sys.stderr)
        return list(KNOWN_SIBLINGS)
    return names


def run_clone(parent_dir: Path, *, remote_base: str, only: str | None = None) -> int:
    """Clone missing siblings into `parent_dir` and fast-forward the ones already there."""
    names = list_remote_siblings()
    if only:
        names = [p.name for p in select_siblings([parent_dir / n for n in names], only)]
    parent_dir.mkdir(parents=True, exist_ok=True)
    any_failed = False
    for name in names:
        repo = parent_dir / name
        if not (repo / ".git").exists():
            proc = subprocess.run(
                ["git", "clone", "--quiet", f"{remote_base}/{name}.git", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                print(f"{name}: cloned")
            else:
                any_failed = True
                print(f"{name}: clone failed ({(proc.stderr or proc.stdout).strip()})", file=sys.stderr)
            continue
        try:
            _, what = refresh_checkout(repo, drop_stranded=False)
            print(f"{name}: {what}")
        except (GitError, SiblingError) as exc:
            any_failed = True
            print(f"{name}: {exc}", file=sys.stderr)
    return 1 if any_failed else 0


# --- checks ---------------------------------------------------------------------------------


def check_python(sibling: Path, *, setup_venvs: bool) -> str:
    """The python that runs `sibling`'s checks: its .venv (created with `setup_venvs`), else ours.

    Raises SiblingError when that python lacks ruff, mypy or pytest, so a missing tool is
    reported before the sync writes anything.
    """
    venv_python = sibling / ".venv" / "bin" / "python"
    if not venv_python.exists() and setup_venvs:
        print(f"{sibling.name}: creating .venv (pip install -e '.[dev]', as its CI does)")
        for cmd in (
            [sys.executable, "-m", "venv", ".venv"],
            [str(venv_python), "-m", "pip", "install", "-q", "--upgrade", "pip"],
            [str(venv_python), "-m", "pip", "install", "-q", "-e", ".[dev]"],
        ):
            proc = _run_cmd(cmd, cwd=sibling)
            if proc.returncode != 0:
                print(_indent(_tail(proc.stderr or proc.stdout, 5)), file=sys.stderr)
                raise SiblingError("creating .venv failed")
    python = str(venv_python) if venv_python.exists() else sys.executable
    probe = _run_cmd([python, "-c", "import mypy, pytest, ruff"], cwd=sibling)
    if probe.returncode != 0:
        raise SiblingError(f"ruff/mypy/pytest not installed for {python} (use --setup-venvs)")
    return python


@dataclass(frozen=True)
class CheckResult:
    failed: str | None  # the label of the first failing check, None when all passed
    output: str  # that check's output, or pytest's when all passed


def run_checks(sibling: Path, python: str) -> CheckResult:
    """Run CI_CHECKS in `sibling` in order, stopping at the first that fails."""
    output = ""
    for label, args in CI_CHECKS:
        proc = _run_cmd([python, "-B", *args], cwd=sibling)
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            return CheckResult(failed=label, output=output)
    return CheckResult(failed=None, output=output)


# --- update ---------------------------------------------------------------------------------


def pr_body(info: SiblingInfo, new_commit: str, checks: str, paths: Sequence[str]) -> str:
    """The pull-request description for one sibling's sync."""
    slug = info.repo
    old, new = info.commit, new_commit
    files = "\n".join(f"- `{p}`" for p in paths)
    return (
        f"Moves the shared modules to [`{slug.split('/')[-1]}@{new[:7]}`](https://github.com/{slug}/commit/{new}) "
        "with `scripts/sync_shared.py --update`.\n\n"
        f"- **shared.lock:** `{old[:7]}` → `{new[:7]}` "
        f"([what changed](https://github.com/{slug}/compare/{old}...{new}))\n"
        f"- **Local checks:** {checks}\n\n"
        f"Files updated:\n{files}\n"
    )


def _commit_env(repo: Path) -> dict[str, str]:
    env = {"GIT_AUTHOR_EMAIL": COMMIT_EMAIL, "GIT_COMMITTER_EMAIL": COMMIT_EMAIL}
    try:
        _git(repo, "config", "user.name")
    except GitError:
        env |= {"GIT_AUTHOR_NAME": OWNER, "GIT_COMMITTER_NAME": OWNER}
    return env


def release_sibling(
    sibling: Path,
    *,
    mode: str,
    test: bool,
    setup_venvs: bool,
    keep_failed: bool,
    shared_main: str | None,
    auto_merge: bool = False,
) -> StatusRow:
    """Refresh, sync, check, commit and (per `mode`) push or open a PR for one sibling."""
    info = read_sibling_info(sibling)

    def row(status: str, ok: bool, commit: str | None = None) -> StatusRow:
        return StatusRow(info.name, info.package, (commit or info.commit)[:7], status, ok)

    try:
        default, what = refresh_checkout(sibling, drop_stranded=True)
    except (GitError, SiblingError) as exc:
        return row(str(exc), False)
    if what != "up to date":
        print(f"{info.name}: {what}")
    info = read_sibling_info(sibling)  # the fast-forward may have moved the lock

    # This exact sync already proposed? Then nothing to do until that PR merges.
    if mode == "pr" and shared_main:
        proc = _gh(
            ["pr", "list", "--head", SYNC_BRANCH_PREFIX + shared_main[:7], "--state", "open"]
            + ["--json", "url", "-q", ".[0].url"],
            cwd=sibling,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return row(f"PR already open: {proc.stdout.strip()}", True)

    python = ""
    if test:  # before the sync, so a missing tool can't leave a half-done tree behind
        try:
            python = check_python(sibling, setup_venvs=setup_venvs)
        except SiblingError as exc:
            return row(f"{exc}; nothing changed", False)

    proc = _run_cmd(_sync_cmd(sibling, ["--update"]), cwd=sibling)
    if proc.stdout.strip():
        print(f"{info.name}:\n{_indent(proc.stdout)}")
    if proc.returncode != 0:
        print(_indent(_tail(proc.stderr or proc.stdout)), file=sys.stderr)
        return row(f"sync failed (exit {proc.returncode}); {discard_sync(sibling, keep_failed=keep_failed)}", False)

    paths = changed_paths(sibling)  # before the checks, which may leave files of their own
    if not paths:
        return row("up to date", True)
    new = read_sibling_info(sibling).commit
    if shared_main and new != shared_main:
        # sync_shared.py resolves main itself; if main moved mid-run, siblings land on
        # different SHAs. Each commit is still valid; a rerun converges them.
        print(f"{info.name}: warning: locked {new[:7]}, but {SHARED_REPO_NAME} main was {shared_main[:7]} at start")

    checks = "not run locally (--no-test); CI runs them"
    if test:
        result = run_checks(sibling, python)
        if result.failed:
            print(f"{info.name}: {result.failed} failed\n{_indent(_tail(result.output), '    | ')}", file=sys.stderr)
            clause = discard_sync(sibling, keep_failed=keep_failed)
            return row(f"{result.failed} failed; no {'PR' if mode == 'pr' else 'commit'}, {clause}", False, new)
        summary = _tail(result.output, 1).strip("= \n")
        checks = f"ruff check, ruff format, mypy and pytest pass (`{summary}`)"

    branch = default
    try:
        if mode == "pr":
            # sync-shared-* branches belong to this script: -C replaces a leftover local one,
            # and the push below is forced over a leftover remote one (an open PR on it was
            # ruled out above).
            branch = SYNC_BRANCH_PREFIX + new[:7]
            _git(sibling, "switch", "--quiet", "-C", branch)
        _git(sibling, "add", "-A", "--", *paths)
        subject = SYNC_SUBJECT_PREFIX + new[:7]
        body = f"shared.lock: {info.short_commit} -> {new[:7]} (scripts/sync_shared.py --update)\n\n"
        body += "\n".join(f"- {p}" for p in paths)
        _git(sibling, "commit", "--quiet", "-m", subject, "-m", body, env=_commit_env(sibling))
    except GitError as exc:
        if branch != default:
            _git(sibling, "switch", "--quiet", default)  # the uncommitted changes come along
        return row(f"commit failed ({exc}); changes left in {sibling}", False, new)

    try:
        if mode == "commit":
            return row(f"committed on {branch} (not pushed)", True, new)
        if mode == "push":
            _git(sibling, "push", "--quiet", "origin", default)
            return row(f"pushed to {default}", True, new)
        _git(sibling, "push", "--quiet", "--force", "-u", "origin", branch)
        proc = _gh(
            ["pr", "create", "--base", default, "--head", branch, "--title", subject]
            + ["--body", pr_body(info, new, checks, paths)],
            cwd=sibling,
        )
        if proc.returncode != 0:
            return row(f"PR failed ({(proc.stderr or proc.stdout).strip()}); branch {branch} pushed", False, new)
        url = proc.stdout.strip()
        status = f"PR {url}"
        if auto_merge:
            status += "; " + enable_auto_merge(sibling, url, default)
        # Older sync PRs still open are now stale: flag them rather than close them unasked.
        others = _gh(
            ["pr", "list", "--state", "open", "--json", "headRefName,url", "-q"]
            + [
                f'.[] | select(.headRefName | startswith("{SYNC_BRANCH_PREFIX}")) '
                f'| select(.headRefName != "{branch}") | .url'
            ],
            cwd=sibling,
        )
        if others.returncode == 0 and others.stdout.split():
            status += f" (supersedes open {' '.join(others.stdout.split())})"
        return row(status, True, new)
    except GitError as exc:
        return row(f"push failed ({exc}); the commit is on local {branch}", False, new)
    finally:
        if branch != default:
            _git(sibling, "switch", "--quiet", default)  # ready for the next run


def required_checks(sibling: Path, branch: str) -> list[str]:
    """The status checks `branch` requires, from classic branch protection and from rulesets.

    Raises SiblingError when GitHub can't say (a 404 from branch protection only means "not
    protected"). gh fills in {owner}/{repo} from the sibling's origin remote.
    """
    checks: list[str] = []
    protection = _gh(
        ["api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection/required_status_checks"]
        + ["--jq", ".contexts[]"],
        cwd=sibling,
    )
    if protection.returncode == 0:
        checks += protection.stdout.splitlines()
    elif "HTTP 404" not in protection.stderr:
        raise SiblingError(f"branch protection: {(protection.stderr or protection.stdout).strip()}")
    rules = _gh(
        ["api", f"repos/{{owner}}/{{repo}}/rules/branches/{branch}", "--jq"]
        + ['.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context'],
        cwd=sibling,
    )
    if rules.returncode != 0:
        raise SiblingError(f"rulesets: {(rules.stderr or rules.stdout).strip()}")
    checks += rules.stdout.splitlines()
    return list(dict.fromkeys(c.strip() for c in checks if c.strip()))


def enable_auto_merge(sibling: Path, pr_url: str, base: str) -> str:
    """Turn on squash auto-merge for a sync PR, or say why not: a clause for the status row.

    Never a failure: the PR is open either way, and a person can merge it. Without required
    checks GitHub's auto-merge merges at once, before CI has run, so that case is skipped.
    """
    allowed = _gh(["api", "repos/{owner}/{repo}", "--jq", ".allow_auto_merge"], cwd=sibling)
    if allowed.returncode == 0 and allowed.stdout.strip() == "false":
        return "auto-merge skipped: turned off for the repo (Settings > General > Allow auto-merge)"
    try:
        checks = required_checks(sibling, base)
    except SiblingError as exc:
        return f"auto-merge skipped: couldn't read {base}'s required checks ({exc})"
    if not checks:
        return f"auto-merge skipped: {base} requires no status checks, so it would merge before CI ran"
    proc = _gh(["pr", "merge", pr_url, "--auto", "--squash"], cwd=sibling)
    if proc.returncode != 0:
        return f"auto-merge failed ({(proc.stderr or proc.stdout).strip()})"
    return f"auto-merge on (squash, once {', '.join(checks)} pass)"


def run_update(
    parent_dir: Path,
    *,
    source: Path | None = None,
    test: bool = False,
    dry_run: bool = False,
    mode: str | None = None,
    setup_venvs: bool = False,
    keep_failed: bool = False,
    only: str | None = None,
    auto_merge: bool = False,
) -> int:
    siblings = select_siblings(discover_siblings(parent_dir), only)
    if not siblings:
        print(f"No sibling repos with {LOCK_FILE} found in {parent_dir}")
        return 0

    if mode is not None:
        if mode == "pr" and _gh(["auth", "status"]).returncode != 0:
            print(
                "update --pr needs the GitHub CLI, logged in (gh auth login); or use --commit/--push", file=sys.stderr
            )
            return 2
        shared_main = resolve_shared_main()
        print(f"{SHARED_REPO_NAME} main: {shared_main[:7] if shared_main else '(unknown: ls-remote failed)'}")
        rows = [
            release_sibling(
                sibling,
                mode=mode,
                test=test,
                setup_venvs=setup_venvs,
                keep_failed=keep_failed,
                shared_main=shared_main,
                auto_merge=auto_merge,
            )
            for sibling in siblings
        ]
        print()
        print(format_status_table(rows))
        return 0 if all(r.ok for r in rows) else 1

    sync_args: list[str] = ["--source", str(source.resolve())] if source is not None else ["--update"]
    if dry_run:
        sync_args.append("--dry-run")

    any_failed = False
    for sibling in siblings:
        python = ""
        if test and not dry_run:
            try:
                python = check_python(sibling, setup_venvs=setup_venvs)
            except SiblingError as exc:
                any_failed = True
                print(f"{sibling.name}: {exc}; not synced", file=sys.stderr)
                continue

        proc = _run_cmd(_sync_cmd(sibling, sync_args), cwd=sibling)
        sync_ok = proc.returncode in (0, 1) if dry_run else proc.returncode == 0
        if not sync_ok:
            any_failed = True
            err_msg = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
            print(f"{sibling.name}: sync failed ({err_msg})", file=sys.stderr)
            continue

        modified = _extract_modified_files(proc.stdout)
        updated = _sibling_was_updated(proc.stdout, modified)
        prefix = f"{sibling.name} (dry-run)" if dry_run else sibling.name
        if modified:
            print(f"{prefix}: {', '.join(modified)}")
        elif updated:
            print(f"{prefix}: lock updated (no files changed)")
        else:
            print(f"{prefix}: nothing to change")

        if test and not dry_run and updated:
            result = run_checks(sibling, python)
            if result.failed is None:
                print(f"{sibling.name}: checks passed (ruff check, ruff format, mypy, pytest)")
            else:
                any_failed = True
                print(f"{sibling.name}: {result.failed} failed", file=sys.stderr)
                if result.output.strip():
                    print(result.output.rstrip(), file=sys.stderr)

    return 1 if any_failed else 0


# --- CLI ------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    shared_parent = argparse.ArgumentParser(add_help=False)
    shared_parent.add_argument(
        "--parent-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"directory containing sibling mcp-server-* repos (default: {DEFAULT_PARENT_DIR})",
    )
    shared_parent.add_argument(
        "--only",
        default=argparse.SUPPRESS,
        help="comma-separated siblings to act on, short names allowed (webos,harmony)",
    )

    parser = argparse.ArgumentParser(
        prog="sync_all_siblings.py",
        description="Check, update, or propose shared-file syncs across all sibling MCP server repos.",
    )
    parser.add_argument(
        "--parent-dir",
        type=Path,
        default=DEFAULT_PARENT_DIR,
        help=f"directory containing sibling mcp-server-* repos (default: {DEFAULT_PARENT_DIR})",
    )
    parser.add_argument("--only", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--latest",
        action="store_true",
        default=False,
        help="with default status command: compare against mcp-server-shared main instead of shared.lock",
    )

    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser(
        "status",
        parents=[shared_parent],
        help="check whether each sibling matches its shared.lock (or main with --latest)",
    )
    status_parser.add_argument(
        "--latest",
        action="store_true",
        default=False,
        help="compare against mcp-server-shared main instead of shared.lock",
    )

    clone_parser = subparsers.add_parser(
        "clone",
        parents=[shared_parent],
        help="clone missing sibling repos and fast-forward the ones already checked out",
    )
    clone_parser.add_argument(
        "--https",
        dest="remote_base",
        action="store_const",
        const=HTTPS_REMOTE_BASE,
        default=SSH_REMOTE_BASE,
        help=f"clone over HTTPS ({HTTPS_REMOTE_BASE}) instead of SSH ({SSH_REMOTE_BASE})",
    )
    clone_parser.add_argument(
        "--remote-base",
        dest="remote_base",
        help="clone from <REMOTE_BASE>/<repo>.git (a fork or mirror)",
    )

    update_parser = subparsers.add_parser(
        "update",
        parents=[shared_parent],
        help="update shared files in each sibling; optionally run its CI checks, commit, push or open PRs",
    )
    update_parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="sync from this local mcp-server-shared checkout instead of GitHub main",
    )
    update_parser.add_argument(
        "--test",
        action="store_true",
        default=False,
        help="run each updated sibling's CI checks: ruff check, ruff format --check, mypy, pytest",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="report what would change without modifying files or running checks",
    )
    modes = update_parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--pr",
        dest="mode",
        action="store_const",
        const="pr",
        help="commit each passing sibling's sync on a sync-shared-<sha> branch and open a PR (implies --test)",
    )
    modes.add_argument(
        "--commit",
        dest="mode",
        action="store_const",
        const="commit",
        help="commit each passing sibling's sync on its default branch; don't push (implies --test)",
    )
    modes.add_argument(
        "--push",
        dest="mode",
        action="store_const",
        const="push",
        help="commit and push each passing sibling's sync straight to its default branch (implies --test)",
    )
    update_parser.add_argument(
        "--no-test",
        action="store_true",
        default=False,
        help="with --pr/--commit/--push: skip the local checks (CI still runs them)",
    )
    update_parser.add_argument(
        "--setup-venvs",
        action="store_true",
        default=False,
        help="create a missing .venv in each sibling (pip install -e '.[dev]') to run the checks in",
    )
    update_parser.add_argument(
        "--keep-failed",
        action="store_true",
        default=False,
        help="with --pr/--commit/--push: leave a sync whose checks failed in the tree (default: undo it)",
    )
    update_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="with --pr: turn on squash auto-merge for each new PR; skipped (and said why) where the repo "
        "has auto-merge off or its default branch requires no checks, which would merge before CI ran",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        args.command = "status"
    if args.command == "update":
        if args.mode is not None and args.source is not None:
            update_parser.error("--pr/--commit/--push move shared.lock, which --source can't do")
        if args.mode is not None and args.dry_run:
            update_parser.error("--dry-run doesn't go with --pr/--commit/--push; use it alone to preview")
        if args.mode is None and (args.no_test or args.keep_failed):
            update_parser.error("--no-test and --keep-failed only go with --pr/--commit/--push")
        if args.auto_merge and args.mode != "pr":
            update_parser.error("--auto-merge only goes with --pr")
        if args.mode is not None and args.test and args.no_test:
            update_parser.error("--test and --no-test contradict each other")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    parent_dir: Path = args.parent_dir
    only: str | None = args.only
    if args.command == "clone":
        return run_clone(parent_dir, remote_base=str(args.remote_base), only=only)
    if args.command == "update":
        mode: str | None = args.mode
        return run_update(
            parent_dir,
            source=args.source,
            test=bool(args.test) or (mode is not None and not args.no_test),
            dry_run=bool(args.dry_run),
            mode=mode,
            setup_venvs=bool(args.setup_venvs),
            keep_failed=bool(args.keep_failed),
            only=only,
            auto_merge=bool(args.auto_merge),
        )
    return run_status(parent_dir, latest=bool(args.latest), only=only)


if __name__ == "__main__":
    sys.exit(main())
