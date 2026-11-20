"""Tool snapshots: each sibling's real tool list and instructions, pulled once and committed.

The extractor runs inside the sibling's own virtualenv (it needs the sibling's dependencies),
imports `<pkg>.server`, and prints the tool list as JSON. Later routing-eval tasks read the
committed JSON, so a run needs no sibling checkout and no device.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.sync_all_siblings import read_sibling_info

SERVER_PREFIX = "mcp-server-"

# Run as `<sibling>/.venv/bin/python -c EXTRACTOR <pkg>` from the sibling's directory. Every
# sibling's server.py exposes a module-level `mcp`; listing tools needs no device and no config.
EXTRACTOR = """\
import asyncio
import importlib
import json
import sys

mcp = importlib.import_module(sys.argv[1] + ".server").mcp
tools = asyncio.run(mcp.list_tools())
print(json.dumps({
    "instructions": mcp.instructions,
    "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
}))
"""


class SnapshotError(RuntimeError):
    """A sibling's tools could not be snapshotted; the message names the sibling and why."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None

    @property
    def is_read(self) -> bool:
        """True only for an explicit `readOnlyHint: true`; no annotation means state-changing (MCP default)."""
        return (self.annotations or {}).get("readOnlyHint") is True

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.output_schema is not None:
            data["outputSchema"] = self.output_schema
        if self.annotations is not None:
            data["annotations"] = self.annotations
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ToolSpec:
        return cls(
            name=data["name"],
            description=data.get("description") or "",
            input_schema=data.get("inputSchema") or {"type": "object"},
            output_schema=data.get("outputSchema"),
            annotations=data.get("annotations"),
        )


@dataclass(frozen=True)
class ServerSnapshot:
    server: str
    package: str
    commit: str
    instructions: str | None
    tools: tuple[ToolSpec, ...]

    def namespaced(self, tool: str) -> str:
        """The name a client presents the tool under (Claude Code's `mcp__<server>__<tool>`)."""
        return f"mcp__{self.server}__{tool}"


def server_name(sibling: Path) -> str:
    """`mcp-server-steam-backlog` -> `steam-backlog` (hyphens stay)."""
    return sibling.name.removeprefix(SERVER_PREFIX)


def _head_commit(sibling: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=sibling, capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotError(f"{sibling.name}: cannot read the git commit: {exc}") from exc
    return result.stdout.strip()


def _tail(text: str, lines: int = 10) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def snapshot_sibling(sibling: Path) -> ServerSnapshot:
    python = sibling / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SnapshotError(
            f"{sibling.name}: no {python.relative_to(sibling)}; run "
            "`scripts/sync_all_siblings.py update --setup-venvs` or create the virtualenv"
        )
    package = read_sibling_info(sibling).package
    try:
        proc = subprocess.run(
            [str(python), "-c", EXTRACTOR, package],
            cwd=sibling,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotError(f"{sibling.name}: extractor did not run: {exc}") from exc
    if proc.returncode != 0:
        raise SnapshotError(f"{sibling.name}: extractor failed ({proc.returncode}): {_tail(proc.stderr)}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        # The payload is the last line: an import may print its own noise before it.
        data = json.loads(lines[-1])
        tools = tuple(ToolSpec.from_json(t) for t in data["tools"])
        instructions = data["instructions"]
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"{sibling.name}: extractor output is not a tool list: {exc!r}") from exc
    return ServerSnapshot(
        server=server_name(sibling),
        package=package,
        commit=_head_commit(sibling),
        instructions=instructions,
        tools=tools,
    )


def write_snapshot(snap: ServerSnapshot, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "server": snap.server,
        "package": snap.package,
        "commit": snap.commit,
        "instructions": snap.instructions,
        "tools": [t.to_json() for t in sorted(snap.tools, key=lambda t: t.name)],
    }
    path = out_dir / f"{snap.server}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_snapshots(dir: Path) -> dict[str, ServerSnapshot]:  # noqa: A002 - the plan's interface name
    snapshots: dict[str, ServerSnapshot] = {}
    for path in sorted(dir.glob("*.json")) if dir.is_dir() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        snap = ServerSnapshot(
            server=data["server"],
            package=data["package"],
            commit=data["commit"],
            instructions=data["instructions"],
            tools=tuple(ToolSpec.from_json(t) for t in data["tools"]),
        )
        snapshots[snap.server] = snap
    return snapshots


def unannotated(snap: ServerSnapshot) -> list[str]:
    """Tool names with no annotations at all (treated as state-changing; the snapshot step warns)."""
    return [t.name for t in snap.tools if t.annotations is None]
