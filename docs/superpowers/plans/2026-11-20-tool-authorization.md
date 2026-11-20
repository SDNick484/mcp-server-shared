# Per-Tool Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `remote.py` knows what kind of caller sent each MCP message (person, automated, local),
and a `@tier` decorator on each sibling tool refuses calls outside the caller's tiers and makes
`costly` calls wait for the person's yes in an elicitation prompt.

**Architecture:** `AccessVerifier` classifies the verified JWT into a `Caller`; `AccessMiddleware`
puts it in the ASGI scope; `@tier` adds one hidden `Resolve(...)` parameter to the tool, whose
resolver reads the caller from the request on the tool's `Context`, decides the tier, and either
refuses, allows, or asks. On the 2026-07-28 revision the ask is an `Elicit` marker the SDK turns
into an `InputRequiredResult`; on older revisions the resolver calls `ctx.elicit` itself under a
120 s timeout. Sealed request state is bound to the caller and expires in 120 s; used
confirmations are remembered so one yes runs once.

**Tech Stack:** Python >= 3.11, `mcp>=2.3,<3` (`mcp.server.mcpserver.resolve.Resolve`/`Elicit`,
`mcp.server.request_state.RequestStateSecurity`, `mcp.server.elicitation.ElicitationResult`),
PyJWT, anyio, pytest with the SDK's in-process `Client` and the existing `running(...)` HTTP
fixture in `templates/tests/test_remote.py`.

**Spec:** `docs/superpowers/specs/2026-11-18-tool-authorization-design.md` (sections cited as §N).

## Global Constraints

- `remote.py` stays self-contained: no import from `untrusted.py`, `tools.py`, `logsafe.py`,
  `mcp_shared` or a sibling package (CLAUDE.md, §Constraints).
- Edit `src/mcp_shared/remote.py` and `templates/tests/`; never the rendered `tests/test_*.py`.
  Render with `python scripts/sync_shared.py --source . --package mcp_shared` before testing.
- Gate: `pytest && ruff check . && ruff format --check . && mypy` plus
  `ruff check --no-respect-gitignore tests && ruff format --check --no-respect-gitignore tests`;
  coverage `fail_under = 95`.
- Tier names exactly: `read`, `power-off`, `everyday`, `costly`.
- Settings exactly: `MCP_ALLOWED_SERVICE_TOKENS` (comma-separated client IDs, unset = none admitted);
  `MCP_AUTOMATION_TIERS` (default `read,power-off`; may add `everyday`; `costly` or an unknown name
  is a `ConfigError` at startup).
- Confirmation timeout and sealed-state TTL: 120 seconds (`CONFIRM_TIMEOUT = 120`).
- Nothing defaults open: no caller in an HTTP request's scope, a tool without a tier at call time,
  or a tier function that raises → refuse.
- Refusals are `ToolError`s (`mcp.server.mcpserver.exceptions`), never crashes. Every refusal and
  every confirmation answer is logged at INFO with caller kind, masked caller, tool, tier, outcome.
- Public repo placeholders only: `user@example.com`, `example.cloudflareaccess.com`, service-token
  IDs like `token-1.access`.

## Review Focus

1. **A service token with no `MCP_ALLOWED_SERVICE_TOKENS` and no email allow-list.** Today it gets
   in; after this change it must get 403 (§1: unset means none). This is a behaviour change for
   any deployment using one; the README says so. Test in Task 1.
2. **The same answered 2026-07-28 retry sent twice** (a client bug, or a replay). The second must
   be refused, not run the costly tool again (§4.5). Test in Task 3.
3. **A sealed confirmation re-sent by a different caller** (person A's yes in person B's request).
   Must be rejected by the SDK's principal binding. Test in Task 3 over HTTP with two JWTs.
4. **A sibling module with `from __future__ import annotations` whose tool uses a type alias
   defined in the sibling** (`TvArg`). The wrapper's hints must resolve in the sibling's namespace,
   not `remote.py`'s, and the tool's input schema must be unchanged by `@tier`. Test in Task 2.
5. **A client that declares elicitation but the person never answers** (older revision). The call
   must end refused after 120 s, not hang the session. Test in Task 3 with a patched timeout.

---

### Task 1: Caller kinds and the new settings

**Files:**
- Modify: `src/mcp_shared/remote.py` (module docstring settings list; `AccessPolicy`; `HttpConfig`;
  `http_config`; `AccessVerifier.verify`; `AccessMiddleware`; `build_app`)
- Test: `templates/tests/test_remote.py`

**Interfaces:**
- Produces:
  - `TIERS: tuple[str, ...] = ("read", "power-off", "everyday", "costly")`;
    `Tier = Literal["read", "power-off", "everyday", "costly"]`.
  - `@dataclass(frozen=True) Caller(kind: Literal["person", "automated", "local"], ident: str, tiers: frozenset[str])`
    with `shown -> str` (email masked as `x***@domain` exactly as `verify` does today; a service
    token's client ID as is; `"local"`). `LOCAL = Caller("local", "local", frozenset(TIERS))`.
  - `AccessPolicy.service_tokens: frozenset[str] = field(default=frozenset(), repr=False)`.
  - `HttpConfig.automation_tiers: frozenset[str] = frozenset({"read", "power-off"})`;
    `check()` raises `ConfigError` if it contains `costly` or a name not in `TIERS`.
  - `AccessVerifier.caller(claims: Mapping[str, Any], automation_tiers: frozenset[str]) -> Caller`:
    `email` present → person (all tiers); else `common_name` in `policy.service_tokens` →
    automated with `automation_tiers`; else raise `AccessDenied("unknown caller")`.
    `verify` keeps the email allow-list check but applies it only when the JWT has an `email`.
  - `CALLER_STATE_KEY = "mcp_caller"`. `AccessMiddleware(..., automation_tiers=...)` sets
    `scope.setdefault("state", {})[CALLER_STATE_KEY]` to the `Caller` on every request it passes
    (with no verifier: `LOCAL`). `build_app` passes `cfg.automation_tiers`.
  - `http_config` reads `MCP_ALLOWED_SERVICE_TOKENS` into `AccessPolicy.service_tokens` and
    `MCP_AUTOMATION_TIERS` (lower-cased, `_csv`) into `automation_tiers`.

- [ ] **Step 1: Write failing tests** in `templates/tests/test_remote.py`:
  `test_automation_tiers_default_and_parse` (unset → `{"read", "power-off"}`; `"read,everyday"` →
  that set); `test_automation_tiers_refuse_costly_and_unknown` (`"costly"` and `"reed"` →
  `ConfigError` from `check()` naming the value); `test_service_tokens_setting_parsed`;
  `test_caller_person_from_email` (kind `person`, tiers = all four);
  `test_caller_automated_from_admitted_common_name` (`common_name="token-1.access"`, admitted →
  `automated`, tiers = automation tiers); `test_service_token_refused_when_not_admitted` (Review
  Focus 1: no allow-lists at all, `common_name` only → `AccessDenied`);
  `test_service_token_admitted_alongside_email_allow_list` (emails set, token admitted → passes);
  `test_jwt_with_neither_email_nor_common_name_refused`; `test_caller_shown_masks_email`
  (`user@example.com` → `u***@example.com`); `test_middleware_puts_caller_in_scope` (an inner ASGI
  app records `scope["state"]["mcp_caller"]`; with a verifier → person; with none → `LOCAL`).
- [ ] **Step 2: Render and run** `pytest tests/test_remote.py -v`; expect the new tests to fail.
- [ ] **Step 3: Implement** the interfaces above. Update the module docstring's settings list with
  both new variables.
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "remote: classify callers; MCP_ALLOWED_SERVICE_TOKENS and MCP_AUTOMATION_TIERS"`.

### Task 2: `@tier` and refusing automated callers

**Files:**
- Modify: `src/mcp_shared/remote.py` (new section "per-tool authorization")
- Test: `templates/tests/test_authorization.py` (new), `shared.toml` (new `[[file]]`, module
  `remote`, target `tests/test_authorization.py`, `render = true`), `.gitignore`
  (`/tests/test_authorization.py`)

**Interfaces:**
- Consumes: `Caller`, `LOCAL`, `CALLER_STATE_KEY`, `TIERS` (Task 1).
- Produces:
  - `TierFn = Callable[[Mapping[str, Any]], Tier]`.
  - `@dataclass(frozen=True) TierSpec(tier: Tier | TierFn, confirm: str | None)` with
    `resolve(args: Mapping[str, Any]) -> Tier`.
  - `tier(level: Tier | TierFn, *, confirm: str | None = None) -> Callable[[F], F]`. Raises
    `ValueError` at decoration time for a literal `costly` without `confirm`, or a name not in
    `TIERS`. A function-form tier that returns `costly` at call time without `confirm` is refused.
  - `TIER_ATTR = "__mcp_tier__"`; the wrapper carries the `TierSpec` there (survives
    `functools.wraps` from outer decorators). `tier_of(fn: Callable[..., Any]) -> TierSpec | None`.
  - `caller_of(ctx: Context[Any, Any]) -> Caller`: `ctx.request_context.request` is `None` →
    `LOCAL` (stdio, or `MCPServer.call_tool` in a test); a request whose
    `scope["state"]` lacks the key → raise `ToolError` (never default open).

**How the wrapper is built** (the one non-obvious part; §3 left it to the builder): the wrapper's
signature is the tool's own parameters plus one keyword-only parameter
`_authorized: Annotated[ElicitationResult[Confirmation], Resolve(resolver)]`, which the SDK fills
before the body and leaves out of the input schema. Set `wrapper.__signature__` and
`wrapper.__annotations__` from `typing.get_type_hints(fn, include_extras=True)` evaluated at
decoration time, so names from the sibling's module resolve (`remote.py`'s globals can't see
`TvArg`). The resolver is a generated function whose `__signature__` is `(ctx: Context, **the
tool's parameters by name)`, so the SDK passes it the call's arguments; it calls
`spec.resolve(args)`, then `caller_of(ctx)`. In this task it refuses an automated caller whose
tiers lack the tier, with "This tool needs a person; automated callers may only use: <tiers>.",
and otherwise returns `Allowed()` (a plain value, which the SDK wraps as accepted). The wrapper
drops `_authorized` and calls the tool.

- [ ] **Step 1: Write failing tests** in `templates/tests/test_authorization.py` (import from
  `__PKG__.remote`; build a small `MCPServer` in the test; use the SDK's in-process `Client`):
  `test_tier_rejects_unknown_name_and_costly_without_confirm`;
  `test_input_schema_unchanged_by_tier` (Review Focus 4: a tool in a helper module with
  `from __future__ import annotations` and a local `Annotated` alias; `list_tools()` schema for the
  decorated tool equals the undecorated one's, and has no `_authorized`);
  `test_tier_of_survives_wraps` (`functools.wraps` around a `@tier` function keeps the spec);
  `test_local_caller_runs_every_tier` (no request → `LOCAL`; read, everyday tools run);
  `test_set_power_tier_depends_on_state` (`lambda a: "power-off" if a["state"] == "off" else "everyday"`:
  `resolve({"state": "off"}) == "power-off"`);
  `test_tier_fn_that_raises_refuses`.
  In `templates/tests/test_remote.py`, over HTTP with the `running(...)` fixture and service-token
  JWTs: `test_automated_caller_gets_read_and_power_off`;
  `test_automated_caller_refused_everyday_with_message` (the result `isError` and text names the
  allowed tiers); `test_automation_tiers_everyday_allows_it`;
  `test_automated_caller_never_gets_costly` (with `everyday` added, a costly tool is still refused);
  `test_set_power_on_refused_off_allowed_for_automated`;
  `test_refusal_logged_at_info` (caplog: kind, masked caller, tool, tier, `refused`).
- [ ] **Step 2: Add** the `shared.toml` entry and `.gitignore` line; render; run; expect failure.
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "remote: @tier, and refuse automated callers outside their tiers"`.

### Task 3: Confirming costly calls

**Files:**
- Modify: `src/mcp_shared/remote.py`
- Test: `templates/tests/test_authorization.py`, `templates/tests/test_remote.py`

**Interfaces:**
- Consumes: Task 2's resolver and wrapper.
- Produces:
  - `class Confirmation(BaseModel): confirm: bool` (the elicitation schema; field title "Run it?").
  - `CONFIRM_TIMEOUT = 120`.
  - `request_state_security() -> RequestStateSecurity`:
    `RequestStateSecurity(keys=[os.urandom(32)], ttl=CONFIRM_TIMEOUT, bind_principal=caller_principal)`
    (the same process-local key `ephemeral()` makes), with `caller_principal(ctx) -> str | None` (returns
    `f"{caller.kind}:{caller.ident}"` from the request scope; `None` with no request). Siblings
    pass it as `MCPServer(name, ..., request_state_security=request_state_security())`.

**Resolver for `costly`** (person or local caller):
1. Client capability check first, with our wording: if `ctx.client_capabilities` has no form
   elicitation → raise `ToolError("This needs your confirmation, and this app can't ask for it. Run it from Claude Code.")`.
2. Prompt text: `confirm` sentence, then `Server: <ctx server name> · Tool: <name> · Arguments: <compact JSON>`.
3. Revision ≥ 2026-07-28 (`ctx.protocol_version`): return `Elicit(prompt, Confirmation)`.
   Revision older: `with anyio.fail_after(CONFIRM_TIMEOUT): return await ctx.elicit(prompt, Confirmation)`
   and turn `TimeoutError` into a declined outcome (the resolver returns the outcome as a plain
   value; the wrapper unwraps either shape).
4. Wrapper: anything other than accepted with `confirm is True` → `ToolError("Not confirmed; nothing was done.")`.
5. Replay guard (≥ 2026-07-28 only): on an accepted answer, `sha256(ctx.request_state)` is checked
   against and added to a module-level `dict[str, float]` of used digests with expiry
   `time.monotonic() + CONFIRM_TIMEOUT`, pruned on each insert; a hit → `ToolError("This confirmation was already used.")`.
6. Log every answer at INFO (accepted, declined, cancelled, timed out, replayed).

- [ ] **Step 1: Write failing tests** in `templates/tests/test_authorization.py`, each run on both
  `Client(server, mode="legacy", elicitation_callback=...)` and `mode="2026-07-28"` (pytest
  parametrize): `test_costly_runs_after_yes`; `test_costly_refused_on_no`;
  `test_costly_refused_on_cancel`; `test_costly_refused_without_elicitation_capability` (no
  callback declared; message says "Run it from Claude Code");
  `test_prompt_names_server_tool_and_arguments`; `test_everyday_tool_never_prompts` (callback
  asserts it is never called). Legacy only: `test_unanswered_prompt_times_out` (Review Focus 5:
  monkeypatch `CONFIRM_TIMEOUT` to 0.1, callback sleeps; refused, tool not run).
  2026-07-28 only: `test_replayed_answer_refused` (Review Focus 2: drive the call by hand
  through the `InputRequiredResult`, send the answered retry twice; the tool body runs once).
  In `templates/tests/test_remote.py` over HTTP: `test_sealed_confirmation_bound_to_caller`
  (Review Focus 3: person A gets the `InputRequiredResult`; the answered retry sent with person B's
  JWT is rejected and the tool does not run); `test_request_state_security_ttl_is_120`.
- [ ] **Step 2: Render and run**; expect failure.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "remote: confirm costly tools by elicitation, once per yes"`.

### Task 4: Tier coverage test and docs

**Files:**
- Modify: `templates/tests/test_authorization.py`, `README.md`, `src/mcp_shared/remote.py`
  (module docstring: a "What each caller may do" section)
- Test: `templates/tests/test_authorization.py`

**Interfaces:**
- Consumes: `tier_of` (Task 2).
- Produces: `test_every_tool_has_a_tier`, the test every sibling runs.

- [ ] **Step 1: Write the test**: `importlib.util.find_spec("__PKG__.server")` is `None` →
  `pytest.skip("no server in this package")` (this is the case for `mcp_shared`); otherwise
  import `__PKG__.server.mcp`, and for every tool from `await mcp.list_tools()`, look up the
  registered function (`mcp._tool_manager.get_tool(name).fn`: private, pinned by `mcp<3`; say so in
  a comment) and collect names where `tier_of(fn) is None`. Assert the list is empty, naming them.
  Also `test_costly_tools_have_confirm_text`. Prove it bites with a local `MCPServer` holding one
  untiered tool, through a helper `untiered_tools(mcp) -> list[str]` that both tests use.
- [ ] **Step 2: Run** it; the helper test fails until written; the sibling test skips here.
- [ ] **Step 3: Write** the README section: the three caller kinds, the two settings and their
  defaults, the four tiers with the §2 table, the costly-tool confirmation and that it works from
  the Claude Code CLI only for now (§Constraints), the Review Focus 1 behaviour change, and the
  sibling wiring (`@tier` under `@mcp.tool`, `request_state_security=request_state_security()`).
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "authorization: tier coverage test and docs"`.

## After this plan (not tasks here)

The sibling sweep (§6.2): one PR per sibling, run with the sibling-sweep skill after this merges.
Each adds a tier to every tool, the function-form tier on `*_set_power`, `confirm` sentences on
the §2 costly list (each sibling settles its own), `request_state_security=request_state_security()`
on its `MCPServer`, and runs `sync_shared.py --update` so `test_every_tool_has_a_tier` arrives and
fails until every tool is tiered. Decorator order with the envelope's `@guarded` is settled in the
envelope plan.

## Task order

1 → 2 → 3 → 4, one lane: each task edits `remote.py` and the same test files.
