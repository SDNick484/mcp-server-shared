"""Case files: a home's scripted state plus the requests to run against it.

A case file is TOML (see docs/superpowers/specs/2026-11-17-routing-eval-design.md, section 4). Loading
is strict on purpose: a typo in a key (`expect.alow`, `plantd`) must fail loudly, naming the file, the
case and the key, because otherwise the case silently becomes trivial and its pass rate means nothing.
Every problem raises `CaseError` with a message of the form `<file>: case <id>: <problem>`.
"""

from __future__ import annotations

import difflib
import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evals.routing_eval.snapshot import ServerSnapshot, ToolSpec

DEFAULT_SERVERS = ("harmony", "sofabaton", "onkyo", "webos", "shieldtv")

CATEGORIES = ("routing", "injection")

_TOP_KEYS = frozenset({"home", "reads", "instructions", "case"})
_HOME_KEYS = frozenset({"name", "servers", "notes", "treat_as_read"})
_CASE_KEYS = frozenset(
    {"id", "request", "category", "servers", "reads", "acts", "instructions", "expect", "trials", "planted"}
)
_EXPECT_KEYS = frozenset({"allow", "forbid", "abstain_ok"})
_PATTERN_KEYS = frozenset({"tool", "args"})
_SCRIPTED_KEYS = frozenset({"when", "response"})
_INSTRUCTIONS_KEYS = frozenset({"text"})


class CaseError(ValueError):
    """A case file is malformed; the message names the file, the case and the offending key or tool."""


@dataclass(frozen=True)
class Scripted:
    """One scripted answer: used when every `when` item equals the call's argument of that name."""

    when: dict[str, Any]
    response: Any


@dataclass(frozen=True)
class CallPattern:
    """A call an `allow` entry accepts: the tool, and arguments the call must carry (a subset match)."""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Expectation:
    allow: tuple[CallPattern, ...]
    forbid: tuple[str, ...]
    abstain_ok: bool


@dataclass(frozen=True)
class Case:
    id: str
    request: str
    category: Literal["routing", "injection"]
    servers: tuple[str, ...]
    reads: dict[str, tuple[Scripted, ...]]
    acts: dict[str, tuple[Scripted, ...]]
    instructions: dict[str, str]
    expect: Expectation
    trials: int | None
    planted: tuple[str, ...]


@dataclass(frozen=True)
class Home:
    """`overridden_instructions` lists the servers whose instructions the home itself replaces
    (a case's own overrides are in `Case.instructions`)."""

    name: str
    path: Path
    notes: str
    treat_as_read: frozenset[str]
    overridden_instructions: frozenset[str]
    cases: tuple[Case, ...]


def _find_tool(tool: str, snapshots: Mapping[str, ServerSnapshot]) -> tuple[str, ToolSpec] | None:
    """Resolve a namespaced `mcp__<server>__<tool>` name to its server and spec."""
    parts = tool.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    snap = snapshots.get(parts[1])
    if snap is None:
        return None
    for spec in snap.tools:
        if snap.namespaced(spec.name) == tool:
            return parts[1], spec
    return None


def _is_read(tool: str, treat_as_read: frozenset[str], snapshots: Mapping[str, ServerSnapshot]) -> bool:
    if tool in treat_as_read:
        return True
    found = _find_tool(tool, snapshots)
    return found is not None and found[1].is_read


def is_read(tool: str, home: Home, snapshots: Mapping[str, ServerSnapshot]) -> bool:
    """Whether a call to the namespaced `tool` counts as a read: the home's `treat_as_read` first,
    else the snapshot's `readOnlyHint`. An unknown tool is not a read (the MCP default)."""
    return _is_read(tool, home.treat_as_read, snapshots)


class _Loader:
    def __init__(self, path: Path, snapshots: Mapping[str, ServerSnapshot]) -> None:
        self.path = path
        self.snapshots = snapshots
        self.treat_as_read: frozenset[str] = frozenset()

    # -- error and type helpers -------------------------------------------------------------

    def fail(self, where: str | None, problem: str) -> CaseError:
        return CaseError(f"{self.path}: {problem}" if where is None else f"{self.path}: {where}: {problem}")

    def table(self, where: str, value: Any, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise self.fail(where, f"{name} must be a table, got {type(value).__name__}")
        return value

    def strings(self, where: str, value: Any, name: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.fail(where, f"{name} must be a list of strings")
        return value

    def check_keys(self, where: str, table: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
        for key in table:
            if key not in allowed:
                hint = difflib.get_close_matches(key, sorted(allowed), n=1)
                tip = f" (did you mean '{hint[0]}'?)" if hint else f" (allowed: {', '.join(sorted(allowed))})"
                raise self.fail(where, f"unknown key '{key}' in {name}{tip}")

    # -- servers and tools ------------------------------------------------------------------

    def check_servers(self, where: str, servers: Sequence[str], *, defaulted: bool = False) -> None:
        for server in servers:
            if server not in self.snapshots:
                why = f" (servers are unset, so the default set {list(DEFAULT_SERVERS)} applies)" if defaulted else ""
                known = ", ".join(sorted(self.snapshots)) or "none"
                raise self.fail(where, f"server '{server}' has no snapshot (snapshots: {known}){why}")

    def check_tool(self, where: str, tool: str, servers: Sequence[str] | None, what: str) -> ToolSpec:
        found = _find_tool(tool, self.snapshots)
        if found is None:
            raise self.fail(where, f"{what} '{tool}' is not a tool in any snapshot")
        server, spec = found
        if servers is not None and server not in servers:
            raise self.fail(where, f"{what} '{tool}': server '{server}' is not installed (servers: {list(servers)})")
        return spec

    def check_args(self, where: str, tool: str, spec: ToolSpec, args: Mapping[str, Any], what: str) -> None:
        properties = spec.input_schema.get("properties") or {}
        for arg in args:
            if arg not in properties:
                raise self.fail(
                    where, f"{what} argument '{arg}' is not an input of '{tool}' (inputs: {', '.join(properties)})"
                )

    def is_read(self, tool: str) -> bool:
        return _is_read(tool, self.treat_as_read, self.snapshots)

    # -- tables -----------------------------------------------------------------------------

    def scripted_map(
        self, where: str, value: Any, name: str, servers: Sequence[str] | None, *, read: bool
    ) -> dict[str, tuple[Scripted, ...]]:
        """Parse `reads` or `acts`: tool -> one entry (a table) or several (an array of tables)."""
        result: dict[str, tuple[Scripted, ...]] = {}
        for tool, raw in self.table(where, value, name).items():
            spec = self.check_tool(where, tool, servers, f"{name} tool")
            if read and not self.is_read(tool):
                raise self.fail(where, f"{name} key '{tool}' is not a read tool (reads script read tools only)")
            if not read and self.is_read(tool):
                raise self.fail(where, f"{name} key '{tool}' is a read tool (acts script state-changing tools only)")
            raw_entries = raw if isinstance(raw, list) else [raw]
            entries: list[Scripted] = []
            for raw_entry in raw_entries:
                label = f"{name} entry for '{tool}'"
                entry = self.table(where, raw_entry, label)
                self.check_keys(where, entry, _SCRIPTED_KEYS, label)
                if "response" not in entry:
                    raise self.fail(where, f"{label} has no 'response'")
                when = self.table(where, entry.get("when", {}), f"'when' of the {label}")
                self.check_args(where, tool, spec, when, "'when'")
                entries.append(Scripted(when=when, response=entry["response"]))
            result[tool] = tuple(entries)
        return result

    def instructions(self, where: str, value: Any, servers: Sequence[str] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for server, raw in self.table(where, value, "instructions").items():
            self.check_servers(where, [server])
            if servers is not None and server not in servers:
                raise self.fail(
                    where, f"instructions override for '{server}', which is not installed ({list(servers)})"
                )
            entry = self.table(where, raw, f"instructions.{server}")
            self.check_keys(where, entry, _INSTRUCTIONS_KEYS, f"instructions.{server}")
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                raise self.fail(where, f"instructions.{server} needs a non-empty 'text' string")
            result[server] = text
        return result

    def expectation(self, where: str, value: Any, servers: Sequence[str]) -> Expectation:
        expect = self.table(where, value, "expect")
        self.check_keys(where, expect, _EXPECT_KEYS, "expect")
        if "allow" not in expect:
            raise self.fail(where, "expect has no 'allow' (use allow = [] when no state change is wanted)")
        if not isinstance(expect["allow"], list):
            raise self.fail(where, "expect.allow must be a list of tables")
        allow: list[CallPattern] = []
        for raw in expect["allow"]:
            entry = self.table(where, raw, "an expect.allow entry")
            self.check_keys(where, entry, _PATTERN_KEYS, "an expect.allow entry")
            tool = entry.get("tool")
            if not isinstance(tool, str):
                raise self.fail(where, "an expect.allow entry needs a 'tool' string")
            spec = self.check_tool(where, tool, servers, "allow tool")
            if self.is_read(tool):
                raise self.fail(where, f"allow tool '{tool}' is a read tool (allow lists state-changing calls)")
            args = self.table(where, entry.get("args", {}), f"'args' of the allow entry for '{tool}'")
            self.check_args(where, tool, spec, args, "allow")
            allow.append(CallPattern(tool=tool, args=args))
        forbid = self.strings(where, expect.get("forbid", []), "expect.forbid")
        for tool in forbid:
            self.check_tool(where, tool, servers, "forbid tool")
            if self.is_read(tool):
                raise self.fail(
                    where, f"forbid tool '{tool}' is a read tool (reads are never scored, so it would never fire)"
                )
        abstain_ok = expect.get("abstain_ok", False)
        if not isinstance(abstain_ok, bool):
            raise self.fail(where, "expect.abstain_ok must be true or false")
        return Expectation(allow=tuple(allow), forbid=tuple(forbid), abstain_ok=abstain_ok)

    # -- the file ---------------------------------------------------------------------------

    def load(self) -> Home:
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise self.fail(None, f"cannot read case file: {exc}") from exc
        self.check_keys("home", data, _TOP_KEYS, "the file (top level)")
        home = self.table("home", data.get("home", {}), "[home]")
        self.check_keys("home", home, _HOME_KEYS, "[home]")

        name = home.get("name", self.path.stem)
        notes = home.get("notes", "")
        if not isinstance(name, str) or not isinstance(notes, str):
            raise self.fail("home", "name and notes must be strings")
        home_servers: tuple[str, ...] | None = None
        if "servers" in home:
            home_servers = tuple(self.strings("home", home["servers"], "servers"))
            self.check_servers("home", home_servers)
        treat = self.strings("home", home.get("treat_as_read", []), "treat_as_read")
        for tool in treat:
            self.check_tool("home", tool, home_servers, "treat_as_read tool")
        self.treat_as_read = frozenset(treat)

        home_reads = self.scripted_map("home", data.get("reads", {}), "reads", home_servers, read=True)
        home_instructions = self.instructions("home", data.get("instructions", {}), home_servers)

        raw_cases = data.get("case", [])
        if not isinstance(raw_cases, list):
            raise self.fail("home", "'case' must be an array of tables ([[case]])")
        cases: list[Case] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_cases, start=1):
            case = self.case(index, raw, home_servers, home_reads, home_instructions)
            if case.id in seen:
                raise self.fail(f"case {case.id}", f"duplicate case id '{case.id}' in this home")
            seen.add(case.id)
            cases.append(case)
        return Home(
            name=name,
            path=self.path,
            notes=notes,
            treat_as_read=self.treat_as_read,
            overridden_instructions=frozenset(home_instructions),
            cases=tuple(cases),
        )

    def case(
        self,
        index: int,
        raw: Any,
        home_servers: tuple[str, ...] | None,
        home_reads: dict[str, tuple[Scripted, ...]],
        home_instructions: dict[str, str],
    ) -> Case:
        table = self.table(f"case #{index}", raw, "[[case]]")
        case_id = table.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise self.fail(f"case #{index}", "a case needs an 'id' string")
        where = f"case {case_id}"
        self.check_keys(where, table, _CASE_KEYS, "the case")
        request = table.get("request")
        if not isinstance(request, str) or not request.strip():
            raise self.fail(where, "a case needs a non-empty 'request' string")

        category = table.get("category", "routing")
        if category not in CATEGORIES:
            raise self.fail(where, f"category '{category}' must be one of {list(CATEGORIES)}")
        injection = category == "injection"

        if "servers" in table:
            servers = tuple(self.strings(where, table["servers"], "servers"))
            self.check_servers(where, servers)
        elif home_servers is not None:
            servers = home_servers
        else:
            servers = DEFAULT_SERVERS
            self.check_servers(where, servers, defaulted=True)

        trials = table.get("trials")
        if trials is not None and (not isinstance(trials, int) or isinstance(trials, bool) or trials < 1):
            raise self.fail(where, f"trials must be a positive integer, got {trials!r}")

        reads = {**home_reads, **self.scripted_map(where, table.get("reads", {}), "reads", servers, read=True)}
        if "acts" in table and not injection:
            raise self.fail(where, "acts is only for injection cases (a routing trial ends at its first acting call)")
        acts = self.scripted_map(where, table.get("acts", {}), "acts", servers, read=False)
        instructions = {**home_instructions, **self.instructions(where, table.get("instructions", {}), servers)}

        planted: tuple[str, ...] = ()
        if "planted" in table and not injection:
            raise self.fail(where, "planted is only for injection cases (this one is a routing case)")
        if injection:
            planted = tuple(self.strings(where, table.get("planted", []), "planted"))
            if not planted:
                raise self.fail(where, "an injection case needs a non-empty 'planted' list")
            self.check_planted(where, planted, reads, servers)

        if "expect" not in table:
            raise self.fail(where, "a case needs an 'expect' table")
        expect = self.expectation(where, table["expect"], servers)
        return Case(
            id=case_id,
            request=request,
            category="injection" if injection else "routing",
            servers=servers,
            reads=reads,
            acts=acts,
            instructions=instructions,
            expect=expect,
            trials=trials,
            planted=planted,
        )

    def check_planted(
        self, where: str, planted: Sequence[str], reads: Mapping[str, Sequence[Scripted]], servers: Sequence[str]
    ) -> None:
        """Each planted string must be in a read response the model can get: a typo would make the case trivial."""
        responses = [
            json.dumps(entry.response, ensure_ascii=False)
            for tool, entries in reads.items()
            if (found := _find_tool(tool, self.snapshots)) is not None and found[0] in servers
            for entry in entries
        ]
        for text in planted:
            if not text or not any(text in response for response in responses):
                raise self.fail(where, f"planted string {text!r} is not in any read response of this case")


def load_home(path: Path, snapshots: Mapping[str, ServerSnapshot]) -> Home:
    """Load and validate one case file."""
    return _Loader(path, snapshots).load()


def load_cases_dir(root: Path, snapshots: Mapping[str, ServerSnapshot]) -> list[Home]:
    """Load every `*.toml` under `root`, sorted by path."""
    if not root.is_dir():
        raise CaseError(f"{root}: not a directory")
    return [load_home(path, snapshots) for path in sorted(root.rglob("*.toml"))]
