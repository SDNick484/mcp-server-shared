"""Check or update shared files across all sibling MCP server checkouts at once.

Lives only in mcp-server-shared (not listed in shared.toml). Discovers sibling
directories matching ``mcp-server-*`` (excluding ``mcp-server-shared``) that
contain a ``shared.lock`` file under ``--parent-dir`` (default: the parent of
this repo's root):

    python scripts/sync_all_siblings.py [status] [--latest]
    python scripts/sync_all_siblings.py update [--source DIR] [--test] [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARENT_DIR = REPO_ROOT.parent
OWN_SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_shared.py"
SHARED_REPO_NAME = "mcp-server-shared"
LOCK_FILE = "shared.lock"


@dataclass(frozen=True)
class SiblingInfo:
    path: Path
    package: str
    commit: str

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


def read_sibling_info(sibling: Path) -> SiblingInfo:
    """Read package and commit metadata from a sibling's `shared.lock`."""
    lock_path = sibling / LOCK_FILE
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        package = str(data.get("package") or "?")
        commit = str(data.get("commit") or "?")
    except (OSError, tomllib.TOMLDecodeError):
        package = "?"
        commit = "invalid"
    return SiblingInfo(path=sibling, package=package, commit=commit)


def _run_cmd(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


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


def run_status(parent_dir: Path, *, latest: bool = False) -> int:
    siblings = discover_siblings(parent_dir)
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


def run_update(
    parent_dir: Path,
    *,
    source: Path | None = None,
    test: bool = False,
    dry_run: bool = False,
) -> int:
    siblings = discover_siblings(parent_dir)
    if not siblings:
        print(f"No sibling repos with {LOCK_FILE} found in {parent_dir}")
        return 0

    sync_args: list[str] = ["--source", str(source.resolve())] if source is not None else ["--update"]
    if dry_run:
        sync_args.append("--dry-run")

    any_failed = False
    for sibling in siblings:
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
            test_proc = _run_cmd([sys.executable, "-m", "pytest"], cwd=sibling)
            if test_proc.returncode == 0:
                print(f"{sibling.name}: pytest passed")
            else:
                any_failed = True
                print(f"{sibling.name}: pytest failed (exit {test_proc.returncode})", file=sys.stderr)
                if test_proc.stdout.strip():
                    print(test_proc.stdout.rstrip(), file=sys.stderr)
                if test_proc.stderr.strip():
                    print(test_proc.stderr.rstrip(), file=sys.stderr)

    return 1 if any_failed else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    shared_parent = argparse.ArgumentParser(add_help=False)
    shared_parent.add_argument(
        "--parent-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"directory containing sibling mcp-server-* repos (default: {DEFAULT_PARENT_DIR})",
    )

    parser = argparse.ArgumentParser(
        prog="sync_all_siblings.py",
        description="Check or update shared files across all sibling MCP server repos.",
    )
    parser.add_argument(
        "--parent-dir",
        type=Path,
        default=DEFAULT_PARENT_DIR,
        help=f"directory containing sibling mcp-server-* repos (default: {DEFAULT_PARENT_DIR})",
    )
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

    update_parser = subparsers.add_parser(
        "update",
        parents=[shared_parent],
        help="update shared files in each sibling and optionally run pytest",
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
        help="run pytest in each updated sibling (ignored with --dry-run)",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="report what would change without modifying files or running tests",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        args.command = "status"
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    parent_dir: Path = args.parent_dir
    if args.command == "update":
        return run_update(
            parent_dir,
            source=args.source,
            test=bool(args.test),
            dry_run=bool(args.dry_run),
        )
    return run_status(parent_dir, latest=bool(args.latest))


if __name__ == "__main__":
    sys.exit(main())
