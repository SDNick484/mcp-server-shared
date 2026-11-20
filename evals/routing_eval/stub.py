"""The read stub: answers a case's read calls from its scripted state, acknowledges acting calls.

Stands in for the MCP servers during a trial. Every response goes through `render_response`, the
single path where the untrusted-output envelope will hook in (spec section 10), so before/after
comparisons change only that hook. Exposure is decided on the payload *before* `render`: an
envelope that escapes or wraps the text must not change which trials count as exposed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from evals.routing_eval.cases import Case, Scripted

NO_DATA: dict[str, Any] = {"result": "no data"}
DEFAULT_ACK: dict[str, Any] = {"ok": True}


def render_response(tool: str, payload: Any) -> str:
    """Serialise a response for the model. The single path every response takes."""
    return json.dumps(payload, ensure_ascii=False)


def match(entries: Sequence[Scripted], args: Mapping[str, Any]) -> Scripted | None:
    """The scripted entry for a call: an entry applies when every `when` item equals the call's
    argument of that name. The entry with the most `when` items wins; ties go to file order."""
    best: Scripted | None = None
    best_size = -1
    for entry in entries:
        applies = all(name in args and args[name] == value for name, value in entry.when.items())
        if applies and len(entry.when) > best_size:
            best, best_size = entry, len(entry.when)
    return best


class Stub:
    """Answers one trial's calls from one case. Records unlisted reads and whether a planted
    string has reached the model (`exposed`, sticky once true)."""

    def __init__(self, case: Case, render: Callable[[str, Any], str] = render_response) -> None:
        self.case = case
        self.render = render
        self.unlisted: list[tuple[str, dict[str, Any]]] = []
        self.exposed = False

    def read(self, tool: str, args: Mapping[str, Any]) -> str:
        entry = match(self.case.reads.get(tool, ()), args)
        if entry is None:
            self.unlisted.append((tool, dict(args)))
            payload: Any = NO_DATA
        else:
            payload = entry.response
        raw = json.dumps(payload, ensure_ascii=False)
        if any(text in raw for text in self.case.planted):
            self.exposed = True
        return self.render(tool, payload)

    def act(self, tool: str, args: Mapping[str, Any]) -> str:
        entry = match(self.case.acts.get(tool, ()), args)
        payload: Any = DEFAULT_ACK if entry is None else entry.response
        return self.render(tool, payload)
