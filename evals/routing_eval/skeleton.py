"""A case-file skeleton from `mcp-server-harmony doctor --dump` output.

Hand-writing a home's wiring is the slow part of adding cases, and the hub already knows which devices
and inputs each activity uses. This turns one dump per hub into the `[home]` notes and the two scripted
Harmony reads (`harmony_get_status`, `harmony_list_activities`) that most cases need; the other servers
and the `[[case]]` entries are added by hand.

The result is a private home: it names real rooms and devices. `write_skeleton` therefore refuses to
write inside this repository (design spec, section 9); put it in a private repository or a local directory.

The dump shape (see mcp-server-harmony's `catalog.py`): top-level `name` and `current_activity` (the running
activity's id, `-1` when the hub is off; an int in real dumps, a string is accepted too), and
`config.activity[]` (`id`, `label`, `fixit`: device id -> `{Power: "On"|"Off", Input?, isManualPower?}`) and
`config.device[]` (`id`, `label`). Entries this module cannot use are skipped rather than fatal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_TOOL = "mcp__harmony__harmony_get_status"
LIST_TOOL = "mcp__harmony__harmony_list_activities"
START_TOOL = "mcp__harmony__harmony_start_activity"

OFF_ID = "-1"  # the hub's own "everything off" pseudo-activity; also `current_activity` when off

REPO_ROOT = Path(__file__).resolve().parents[2]

HEADER = f"""\
# Case-file skeleton generated from a Harmony dump (`mcp-server-harmony doctor --dump`).
# Add the other servers' reads (and their names to `servers` below) and your [[case]] entries; see
# evals/cases/example-home/home.toml. This file names your real devices: keep it out of the public repo.
#
# A case, to copy and fill in (replace the placeholders; `expect.allow` lists the acting calls that pass):
#
# [[case]]
# id = "switch-activity"
# request = "Put the game console on."
# trials = 5
# expect.allow = [{{ tool = "{START_TOOL}", args = {{ activity = "ACTIVITY NAME" }} }}]
# expect.forbid = []

"""


@dataclass(frozen=True)
class _Hub:
    name: str
    current: str  # an activity id, or OFF_ID
    activities: tuple[tuple[str, str], ...]  # (id, label), the off pseudo-activity left out
    powers_on: dict[str, tuple[str, ...]]  # activity id -> "Device (input X)" entries


def _fail(path: Path, problem: str) -> ValueError:
    return ValueError(f"{path}: {problem}")


def _load_hub(path: Path) -> _Hub:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _fail(path, f"cannot read the dump: {exc}") from exc
    if not isinstance(data, dict):
        raise _fail(path, "a dump is a JSON object")
    config = data.get("config")
    if not isinstance(config, dict):
        raise _fail(path, "no 'config' object (is this a `doctor --dump` file?)")
    raw_activities = config.get("activity")
    if not isinstance(raw_activities, list):
        raise _fail(path, "config.activity must be a list")
    raw_devices = config.get("device", [])
    if not isinstance(raw_devices, list):
        raise _fail(path, "config.device must be a list")
    current = data.get("current_activity")
    if not isinstance(current, (int, str)) or isinstance(current, bool):
        raise _fail(path, "no 'current_activity' (an activity id, -1 when the hub is off)")

    labels = {
        str(d["id"]): str(d["label"])
        for d in raw_devices
        if isinstance(d, dict) and "id" in d and isinstance(d.get("label"), str)
    }
    activities: list[tuple[str, str]] = []
    powers_on: dict[str, tuple[str, ...]] = {}
    for raw in raw_activities:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        activity_id = str(raw["id"])
        if activity_id == OFF_ID:
            continue
        activities.append((activity_id, str(raw.get("label", activity_id))))
        fixit = raw.get("fixit")
        entries: list[str] = []
        for key, entry in fixit.items() if isinstance(fixit, dict) else ():
            if not isinstance(entry, dict) or entry.get("Power") != "On":
                continue
            device_id = str(entry.get("id", key))
            entry_text = labels.get(device_id, f"Device {device_id}")
            if isinstance(entry.get("Input"), str) and entry["Input"].strip():
                entry_text += f" (input {entry['Input'].strip()})"
            entries.append(entry_text)
        powers_on[activity_id] = tuple(entries)

    if str(current) != OFF_ID and str(current) not in {a for a, _ in activities}:
        raise _fail(path, f"current_activity {current} is not an activity in config.activity")
    name = data.get("name")
    return _Hub(
        name=name if isinstance(name, str) and name.strip() else path.stem,
        current=str(current),
        activities=tuple(activities),
        powers_on=powers_on,
    )


def _notes(hubs: Sequence[_Hub]) -> str:
    lines = ["Harmony hubs, their activities, and what each activity powers on (from the hubs' own config):"]
    for hub in hubs:
        lines.append(f"Hub {hub.name}:")
        for activity_id, label in hub.activities:
            powered = ", ".join(hub.powers_on[activity_id]) or "nothing the hub switches"
            lines.append(f"- {label}: {powered}")
    return "\n".join(lines) + "\n"


def skeleton_from_harmony_dumps(dumps: Sequence[Path], *, name: str | None = None) -> str:
    """The TOML text of a case file for the hubs in `dumps` (one `doctor --dump` file per hub).

    `name` becomes `[home] name`; left out, the case loader names the home after the file's stem.
    """
    if not dumps:
        raise ValueError("no dump files given")
    hubs = [_load_hub(path) for path in dumps]
    seen: set[str] = set()
    for hub, path in zip(hubs, dumps, strict=True):
        if hub.name in seen:
            raise _fail(path, f"hub '{hub.name}' appears in more than one dump")
        seen.add(hub.name)

    status_hubs: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    for hub in hubs:
        entry: dict[str, Any] = {"hub": hub.name, "reachable": True, "power": "off" if hub.current == OFF_ID else "on"}
        labels = dict(hub.activities)
        if hub.current != OFF_ID:
            # the real tool reports the id as an integer; keep a non-numeric id as it is
            running_id: int | str = int(hub.current) if hub.current.lstrip("-").isdigit() else hub.current
            entry["current_activity"] = {"id": running_id, "name": labels[hub.current]}
        status_hubs.append(entry)
        activities += [
            {"hub": hub.name, "name": label, "ref": f"{hub.name}/{label}", "running": activity_id == hub.current}
            for activity_id, label in hub.activities
        ]

    home: dict[str, Any] = {"servers": ["harmony"], "notes": _notes(hubs)}
    if name is not None:
        home = {"name": name, **home}
    document = {
        "home": home,
        "reads": {
            STATUS_TOOL: [{"response": {"hubs": status_hubs}}],
            LIST_TOOL: [{"response": {"result": activities}}],
        },
    }
    import tomli_w  # the [eval] extra; imported here so the other subcommands work without it

    return HEADER + tomli_w.dumps(document)


def write_skeleton(text: str, out: Path, *, force: bool) -> None:
    """Write `text` to `out`, which must be outside this repository and (without `force`) not yet exist."""
    if out.resolve().is_relative_to(REPO_ROOT):
        raise ValueError(
            f"{out}: inside this repository ({REPO_ROOT}); a skeleton names your real devices, "
            "so write it to a private repository or a local directory"
        )
    if out.exists() and not force:
        raise ValueError(f"{out}: already exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
