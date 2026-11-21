"""Per-tool authorization: the ``@tier`` decorator and who may call what.

Shared by the sibling servers along with remote.py; only the package name in
the imports differs between repos. Change it in mcp-server-shared, not in a
copy, then sync. These tests run the tools through the SDK's in-process
``Client`` (a local caller: no HTTP request); the automated-caller tests that
need a real request and a service-token JWT live in test_remote.py.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from __PKG__ import remote  # the package name differs between repos


def set_power_tier(args: Any) -> remote.Tier:
    return "power-off" if args["state"] == "off" else "everyday"


@pytest.fixture(params=["legacy", "2026-07-28"])
def protocol(request):
    """Both protocol revisions: they reach the tool's resolvers by different paths."""
    return request.param


def text(result: Any) -> str:
    return result.content[0].text


def tiered_server(ran: list[str]) -> MCPServer:
    m = MCPServer("tiered")

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
    async def update() -> str:
        ran.append("update")
        return "updated"

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


@pytest.mark.anyio
async def test_costly_never_runs_unconfirmed(protocol):
    """A costly call runs only after a person's yes, and this client can't give one."""
    ran: list[str] = []
    async with Client(tiered_server(ran), mode=protocol) as c:
        result = await c.call_tool("update", {})
    assert result.is_error
    assert ran == []


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
