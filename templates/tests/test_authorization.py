"""Per-tool authorization: the ``@tier`` decorator and who may call what.

Shared by the sibling servers along with remote.py; only the package name in
the imports differs between repos. Change it in mcp-server-shared, not in a
copy, then sync. These tests run the tools through the SDK's in-process
``Client`` (a local caller: no HTTP request); the automated-caller tests that
need a real request and a service-token JWT live in test_remote.py.
"""

from __future__ import annotations

import contextvars
import functools
import importlib.util
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitResult, InputRequiredResult

from __PKG__ import remote  # the package name differs between repos


def set_power_tier(args: Any) -> remote.Tier:
    return "power-off" if args["state"] == "off" else "everyday"


@pytest.fixture(params=["legacy", "2026-07-28"])
def protocol(request):
    """Both protocol revisions: they reach the tool's resolvers by different paths."""
    return request.param


def text(result: Any) -> str:
    return result.content[0].text


def tiered_server(ran: list[str], **kwargs: Any) -> MCPServer:
    m = MCPServer("tiered", **kwargs)

    @m.tool()
    @remote.tier("read")
    def status() -> str:
        ran.append("status")
        return "ok"

    @m.tool()
    @remote.tier("power-off")
    async def power_off() -> str:
        ran.append("power_off")
        return "off"

    @m.tool()
    @remote.tier("everyday")
    async def play(title: str = "anything"):  # no return annotation: the wrapper copes
        ran.append("play")
        return f"playing {title}"

    @m.tool()
    @remote.tier("costly", confirm="Update the firmware?")
    async def update(tv: str = "tv-1", channel: str = "stable") -> str:
        ran.append(f"update {tv}")
        return "updated"

    @m.tool()
    @remote.tier("costly", confirm="Reboot the TV?")
    def reboot() -> str:  # a sync costly tool
        ran.append("reboot")
        return "rebooting"

    return m


# --- declaring a tier ---------------------------------------------------------------
def test_tier_rejects_unknown_name_and_costly_without_confirm():
    with pytest.raises(ValueError, match="reed"):
        remote.tier("reed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="confirm"):
        remote.tier("costly")
    with pytest.raises(ValueError, match="confirm"):
        remote.tier("costly", confirm="")
    with pytest.raises(ValueError, match="tier"):
        remote.tier(42)  # type: ignore[arg-type]
    remote.tier("costly", confirm="Update the firmware?")  # fine
    remote.tier(set_power_tier)  # a function form needs no confirm until it says costly


def test_tier_refuses_a_tool_that_already_has_the_hidden_parameter():
    def odd(tier_authorized: int = 0) -> str:
        return "?"

    with pytest.raises(ValueError, match="tier_authorized"):
        remote.tier("read")(odd)


SIBLING = textwrap.dedent(
    '''
    from __future__ import annotations

    from typing import Annotated, Literal

    from mcp.server.mcpserver import Context
    from pydantic import Field

    from __PKG__.remote import tier

    TvArg = Annotated[str | None, Field(description="Which TV, e.g. tv-1; needed with several.")]


    async def set_power(state: Literal["on", "off"], ctx: Context, tv: TvArg = None) -> str:
        """Turn a TV on or off."""
        return f"{tv}:{state}"


    plain_set_power = set_power  # the same tool without a tier, to compare schemas with


    @tier(lambda args: "power-off" if args["state"] == "off" else "everyday")
    async def set_power(state: Literal["on", "off"], ctx: Context, tv: TvArg = None) -> str:
        """Turn a TV on or off."""
        return f"{tv}:{state}:{type(ctx).__name__}"
    '''
)


@pytest.fixture
def sibling(tmp_path):
    """A module as a sibling writes one: postponed annotations, and a type alias remote.py can't see."""
    path = tmp_path / "tiered_sibling.py"
    path.write_text(SIBLING)
    spec = importlib.util.spec_from_file_location("tiered_sibling", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["tiered_sibling"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules["tiered_sibling"]


@pytest.mark.anyio
async def test_input_schema_unchanged_by_tier(sibling, protocol):
    """Review Focus 4: the wrapper's hints resolve in the sibling's namespace, not remote.py's."""
    assert not hasattr(remote, "TvArg")
    plain, tiered = MCPServer("plain"), MCPServer("tiered")
    plain.tool(name="set_power")(sibling.plain_set_power)
    tiered.tool(name="set_power")(sibling.set_power)
    (before,) = await plain.list_tools()
    (after,) = await tiered.list_tools()
    assert after.input_schema == before.input_schema
    assert after.output_schema == before.output_schema
    assert after.description == before.description == "Turn a TV on or off."
    assert set(after.input_schema["properties"]) == {"state", "tv"}  # no ctx, no tier_authorized
    assert after.input_schema["properties"]["tv"]["description"].startswith("Which TV")
    # ...and the call still works, with the SDK's Context handed to the tool.
    async with Client(tiered, mode=protocol) as c:
        assert text(await c.call_tool("set_power", {"state": "on", "tv": "tv-1"})) == "tv-1:on:Context"


def test_tier_of_survives_wraps():
    @remote.tier("everyday")
    async def play() -> str:
        return "playing"

    @functools.wraps(play)
    async def logged() -> str:
        return await play()

    spec = remote.tier_of(play)
    assert spec == remote.TierSpec("everyday", None)
    assert remote.tier_of(logged) is spec
    assert getattr(play, remote.TIER_ATTR) is spec
    assert remote.TIER_ATTR == "__mcp_tier__"

    async def untiered() -> str:
        return "?"

    assert remote.tier_of(untiered) is None


# --- deciding the tier --------------------------------------------------------------
def test_set_power_tier_depends_on_state():
    spec = remote.TierSpec(lambda a: "power-off" if a["state"] == "off" else "everyday", None)
    assert spec.resolve({"state": "off"}) == "power-off"
    assert spec.resolve({"state": "on"}) == "everyday"
    assert remote.TierSpec("read", None).resolve({}) == "read"


def test_a_tier_function_naming_no_tier_is_an_error():
    with pytest.raises(ValueError, match="reed"):
        remote.TierSpec(lambda a: "reed", None).resolve({})  # type: ignore[arg-type,return-value]


# --- the local caller ----------------------------------------------------------------
@pytest.mark.anyio
async def test_local_caller_runs_every_tier(protocol):
    """No HTTP request (stdio, or the in-process client): the caller is LOCAL, a person on this machine."""
    ran: list[str] = []
    server = tiered_server(ran)
    async with Client(server, mode=protocol) as c:
        assert text(await c.call_tool("status", {})) == "ok"
        assert text(await c.call_tool("power_off", {})) == "off"
        assert text(await c.call_tool("play", {"title": "a film"})) == "playing a film"
    # MCPServer.call_tool, as a sibling's own tests use it: no request context at all.
    assert text(await server.call_tool("play", {})) == "playing anything"
    assert ran == ["status", "power_off", "play", "play"]


def test_caller_of():
    def ctx(request: Any) -> Any:
        return SimpleNamespace(request_context=SimpleNamespace(request=request))

    person = remote.Caller("person", "user@example.com", frozenset(remote.TIERS))
    assert remote.caller_of(ctx(None)) is remote.LOCAL
    assert remote.caller_of(ctx(SimpleNamespace(scope={"state": {"mcp_caller": person}}))) is person
    # An HTTP request the middleware never saw: refuse rather than guess.
    for request in (
        SimpleNamespace(scope={"state": {}}),
        SimpleNamespace(scope={}),
        SimpleNamespace(scope={"state": {"mcp_caller": "user@example.com"}}),
        object(),
    ):
        with pytest.raises(ToolError, match="who"):
            remote.caller_of(ctx(request))


# --- refusing ------------------------------------------------------------------------
@pytest.mark.anyio
@pytest.mark.parametrize(
    "level",
    [
        lambda a: a["missing"],  # raises KeyError
        lambda a: "reed",  # not a tier
        lambda a: "costly",  # costly, with no confirm sentence to ask with
    ],
)
async def test_tier_fn_that_raises_refuses(level, caplog, protocol):
    ran: list[str] = []
    m = MCPServer("broken")

    @m.tool()
    @remote.tier(level)
    async def act() -> str:
        ran.append("act")
        return "done"

    caplog.set_level("INFO", logger=remote.__name__)
    async with Client(m, mode=protocol) as c:
        result = await c.call_tool("act", {})
    assert result.is_error
    assert ran == []
    assert any("refused" in r.getMessage() and "act" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_wrapper_refuses_anything_but_an_allowed_outcome():
    """Belt and braces: the tool body runs only behind an accepted outcome from its own resolver."""
    ran: list[str] = []

    @remote.tier("read")
    async def status() -> str:
        ran.append("status")
        return "ok"

    @remote.tier("read")
    def sync_status() -> str:
        ran.append("sync_status")
        return "ok"

    allowed = AcceptedElicitation[Any].model_construct(data=remote.Allowed())
    assert await status(tier_authorized=allowed) == "ok"
    assert sync_status(tier_authorized=allowed) == "ok"
    for outcome in (
        DeclinedElicitation(),
        AcceptedElicitation[Any].model_construct(data=None),
        AcceptedElicitation[Any].model_construct(data=remote.Confirmation(confirm=False)),
    ):
        with pytest.raises(ToolError, match="nothing was done"):
            await status(tier_authorized=outcome)
        with pytest.raises(ToolError, match="nothing was done"):
            sync_status(tier_authorized=outcome)
    # A direct Python call that skips MCP is no exception: unwrap it (fn.__wrapped__) on purpose.
    with pytest.raises(ToolError, match="nothing was done"):
        await status()
    assert await status.__wrapped__() == "ok"
    assert ran == ["status", "sync_status", "status"]


# --- confirming costly calls -----------------------------------------------------------
def answering(action: str, confirm: bool | None = None, asked: list[str] | None = None):
    """An elicitation callback: the person's answer, recording each prompt they were shown."""

    async def callback(context: Any, params: Any) -> ElicitResult:
        if asked is not None:
            asked.append(params.message)
        content = None if confirm is None else {"confirm": confirm}
        return ElicitResult(action=action, content=content)

    return callback


def confirming_server(ran: list[str]) -> MCPServer:
    """The tiered tools on a server built as a sibling builds one."""
    return tiered_server(ran, request_state_security=remote.request_state_security())


@pytest.mark.anyio
async def test_costly_runs_after_yes(protocol):
    ran: list[str] = []
    async with Client(confirming_server(ran), mode=protocol, elicitation_callback=answering("accept", True)) as c:
        assert text(await c.call_tool("update", {"tv": "tv-2"})) == "updated"
        assert text(await c.call_tool("reboot", {})) == "rebooting"
    assert ran == ["update tv-2", "reboot"]


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [answering("accept", False), answering("decline")], ids=["no", "decline"])
async def test_costly_refused_on_no(answer, protocol):
    ran: list[str] = []
    async with Client(confirming_server(ran), mode=protocol, elicitation_callback=answer) as c:
        result = await c.call_tool("update", {})
    assert result.is_error
    assert text(result).endswith("Not confirmed; nothing was done.")
    assert ran == []


@pytest.mark.anyio
async def test_costly_refused_on_cancel(protocol):
    ran: list[str] = []
    async with Client(confirming_server(ran), mode=protocol, elicitation_callback=answering("cancel")) as c:
        result = await c.call_tool("update", {})
    assert result.is_error
    assert text(result).endswith("Not confirmed; nothing was done.")
    assert ran == []


@pytest.mark.anyio
async def test_costly_refused_without_elicitation_capability(protocol):
    """An app that can't show the prompt refuses: nothing the model could answer stands in for it."""
    ran: list[str] = []
    server = confirming_server(ran)
    async with Client(server, mode=protocol) as c:  # no callback: elicitation not declared
        result = await c.call_tool("update", {})
    assert result.is_error
    assert "this app can't ask for it. Run it from Claude Code." in text(result)
    # MCPServer.call_tool, with no request at all, can't ask either.
    with pytest.raises(ToolError, match="Run it from Claude Code"):
        await server.call_tool("update", {})
    assert ran == []


@pytest.mark.anyio
async def test_prompt_names_server_tool_and_arguments(protocol):
    asked: list[str] = []
    async with Client(confirming_server([]), mode=protocol, elicitation_callback=answering("accept", True, asked)) as c:
        assert not (await c.call_tool("update", {"tv": "tv-2"})).is_error
    assert asked == [
        'Update the firmware?\n\nServer: tiered · Tool: update · Arguments: {"channel":"stable","tv":"tv-2"}'
    ]


@pytest.mark.anyio
async def test_everyday_tool_never_prompts(protocol):
    async def never(context: Any, params: Any) -> ElicitResult:
        raise AssertionError("an everyday tool asked for a confirmation")

    ran: list[str] = []
    async with Client(confirming_server(ran), mode=protocol, elicitation_callback=never) as c:
        assert text(await c.call_tool("play", {})) == "playing anything"
        assert text(await c.call_tool("status", {})) == "ok"
    assert ran == ["play", "status"]


@pytest.mark.anyio
async def test_costly_tier_fn_without_confirm_refused_even_after_a_yes(protocol):
    """A function form that says costly has no sentence to ask with, so it never asks and never runs."""
    ran: list[str] = []
    asked: list[str] = []
    m = MCPServer("broken", request_state_security=remote.request_state_security())

    @m.tool()
    @remote.tier(lambda a: "costly")
    async def wipe() -> str:
        ran.append("wipe")
        return "wiped"

    async with Client(m, mode=protocol, elicitation_callback=answering("accept", True, asked)) as c:
        result = await c.call_tool("wipe", {})
    assert result.is_error
    assert ran == [] and asked == []


@pytest.mark.anyio
async def test_unanswered_prompt_times_out(monkeypatch, caplog):
    """Review Focus 5: on the older revision a prompt nobody answers ends refused, not a hung session."""
    monkeypatch.setattr(remote, "CONFIRM_TIMEOUT", 0.1)
    caplog.set_level("INFO", logger=remote.__name__)

    async def never_answers(context: Any, params: Any) -> ElicitResult:
        await anyio.sleep(30)
        raise AssertionError("unreachable")

    ran: list[str] = []
    with anyio.fail_after(10):
        async with Client(confirming_server(ran), mode="legacy", elicitation_callback=never_answers) as c:
            result = await c.call_tool("update", {})
            assert result.is_error
            assert text(result).endswith("Not confirmed; nothing was done.")
            # The session is still usable afterwards.
            assert text(await c.call_tool("status", {})) == "ok"
    assert ran == ["status"]
    assert any("update" in r.getMessage() and "timed out" in r.getMessage() for r in caplog.records)


YES = {"confirm": True}


async def first_round(c: Client, name: str, args: dict[str, Any]) -> tuple[InputRequiredResult, dict[str, Any]]:
    """Drive a 2026-07-28 call by hand: the InputRequiredResult, and the person's yes to it."""
    first = await c.session.call_tool(name, args, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    (key,) = first.input_requests
    return first, {key: ElicitResult(action="accept", content=YES)}


@pytest.mark.anyio
@pytest.mark.parametrize("sibling_security", [True, False], ids=["request_state_security", "sdk_default"])
async def test_replayed_answer_refused(sibling_security, caplog):
    """Review Focus 2: the same answered retry sent twice runs the costly tool once."""
    caplog.set_level("INFO", logger=remote.__name__)
    ran: list[str] = []
    server = confirming_server(ran) if sibling_security else tiered_server(ran)
    async with Client(server, mode="2026-07-28", elicitation_callback=answering("accept", True)) as c:
        first, yes = await first_round(c, "update", {})
        retry = {"input_responses": yes, "request_state": first.request_state, "allow_input_required": True}
        once = await c.session.call_tool("update", {}, **retry)
        again = await c.session.call_tool("update", {}, **retry)
    assert not once.is_error and text(once) == "updated"
    assert again.is_error and text(again).endswith("This confirmation was already used.")
    assert ran == ["update tv-1"]
    assert any("replayed" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_each_fresh_yes_runs_once():
    """Two separate confirmations of the very same call are two yeses, each good for one run."""
    ran: list[str] = []
    async with Client(confirming_server(ran), mode="2026-07-28", elicitation_callback=answering("accept", True)) as c:
        for _ in range(2):
            first, yes = await first_round(c, "update", {})
            done = await c.session.call_tool(
                "update", {}, input_responses=yes, request_state=first.request_state, allow_input_required=True
            )
            assert text(done) == "updated"
        assert text(await c.call_tool("update", {})) == "updated"  # the client's own retry loop
    assert ran == ["update tv-1"] * 3


@pytest.mark.anyio
async def test_used_confirmations_expire(monkeypatch):
    """The memory of used confirmations is pruned once their sealed state could no longer be accepted."""
    clock = [1000.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote, "_used_confirmations", {})
    remote._remember_confirmation("a")
    with pytest.raises(ToolError, match="already used"):
        remote._remember_confirmation("a")
    clock[0] += remote.CONFIRM_TIMEOUT + 1
    remote._remember_confirmation("b")  # pruning happens on insert
    assert set(remote._used_confirmations) == {"b"}
    remote._remember_confirmation("a")  # long expired: its state would be refused by the SDK anyway


@pytest.mark.anyio
async def test_confirmation_answers_logged(caplog, protocol):
    caplog.set_level("INFO", logger=remote.__name__)
    for answer in (answering("accept", True), answering("accept", False), answering("decline"), answering("cancel")):
        async with Client(confirming_server([]), mode=protocol, elicitation_callback=answer) as c:
            await c.call_tool("update", {})
    messages = [r.getMessage() for r in caplog.records if r.name == remote.__name__ and r.levelname == "INFO"]
    for outcome in ("confirmed", "declined", "declined", "cancelled"):
        assert any(f"local caller local, tool update, tier costly: {outcome}" in m for m in messages), outcome


def test_request_state_security_ttl_and_principal():
    security = remote.request_state_security()
    assert security.ttl == remote.CONFIRM_TIMEOUT == 120
    assert security.bind_principal is remote.caller_principal
    assert remote.request_state_security().codec is not security.codec  # a fresh key each time

    person = remote.Caller("person", "user@example.com", frozenset(remote.TIERS))
    assert remote.caller_principal(SimpleNamespace(request=None)) is None
    assert (
        remote.caller_principal(SimpleNamespace(request=SimpleNamespace(scope={"state": {"mcp_caller": person}})))
        == "person:user@example.com"
    )
    with pytest.raises(ToolError, match="who"):
        remote.caller_principal(SimpleNamespace(request=SimpleNamespace(scope={})))


def test_recording_codec_notes_only_the_sdk_envelope():
    """The codec notes the token behind the state it unsealed; anything it can't read, it just passes on."""
    codec = remote.request_state_security().codec

    def check() -> None:
        for payload in (b"not json", b"[]", b'{"v":1}', b'{"s":7}'):
            assert codec.unseal(codec.seal(payload)) == payload
            assert remote._unsealed.get() is None
        token = codec.seal(b'{"s":"state"}')
        assert codec.unseal(token) == b'{"s":"state"}'
        noted = remote._unsealed.get()
        assert noted is not None and noted[0] == "state"

    contextvars.Context().run(check)  # a fresh context: nothing noted yet, and nothing left behind
