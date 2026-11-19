# Per-tool authorization: what each caller may do

Status: draft for review, 2026-11-18

## Intent

`remote.py` settles **who** is calling: every HTTP request carries a Cloudflare Access JWT that is
checked for signature, audience, issuer and expiry, and optionally an email allow-list. Once a
caller is in, every tool is open to it. This design adds **what** each caller may do.

The user's stance (user): people they let in get full power, including the power to shoot
themselves in the foot. Authorization is not for policing people. It is for two narrower cases:

1. **Automated callers.** Jobs that connect with an Access service token instead of a person's
   login, such as a scheduled agent that makes sure things were left off. None exist yet; this
   is the case the user finds most interesting. Such a job gets only what its job needs, so a bug
   or a planted instruction (see the untrusted-output envelope, #47) can't turn it into a remote
   control for the house.
2. **Costly tools**, the ones that are not dangerous but are a chore to undo: a firmware update,
   rewriting a device's config, a reboot, losing unsaved game progress. People can still run
   them, but only after confirming in a prompt the app shows them, a prompt the model cannot
   answer.

## Goals

1. Every tool in every sibling has an explicit **tier**: `read`, `power-off`, `everyday`, or
   `costly`.
2. **People** may call every tier. A `costly` call runs only after the person confirms it in an MCP
   elicitation prompt. Anything other than an explicit yes (decline, cancel, timeout, or an app
   that can't show the prompt) refuses the call, with a message saying why.
3. **Automated callers** (service tokens) may call `read` and `power-off` by default, and never
   `costly`. A setting can add `everyday`; nothing can add `costly`.
4. Service tokens can get in at all when an email allow-list is set (today they can't: see
   Constraints).
5. A shared test fails any sibling tool without a tier.

## Non-goals

- **Location rules.** An earlier idea was "costly tools only from the home network". Dropped
  (user): every Claude app reaches the servers through Anthropic's cloud and Cloudflare even from
  the couch, Claude Code isn't always at home, and a confirmation answers the real question (did
  the person mean it?) wherever they are.
- **Per-device rules** (read-only on the phone). The server can't tell which app or device made a
  request: all Claude apps arrive with the same Access login.
- **Restricting people** beyond the costly-tool confirmation.
- **Volume safety.** The hard guarantee for the speakers is the hub volume cap, separate work (see
  the envelope design's follow-on section). Volume tools are `everyday` here.
- **Hiding tools from callers.** An automated caller still sees the whole tool list; calls outside
  its tiers are refused with a clear message (designer's call: simpler, and the refusal tells
  whoever reads the job's log what it tried).

## Anti-goals (would make this a failure even if it works)

- **A confirmation the model can give.** A "call again with confirm=yes" argument is no
  confirmation: an injected model would simply call again. Only an elicitation answered by the
  person counts. Where the app can't ask, the costly tool refuses (user).
- **A default that opens things up.** A tool with no tier, an unrecognized caller, or a setting
  that can't be parsed must never widen what a caller may do. The server refuses to start on an
  unparseable setting.
- **Getting in the way of people.** Outside `costly`, a person notices nothing.

## Constraints

- Lives in `remote.py` (user's framing) and the siblings' `server.py`. Shared modules stay
  self-contained: `remote.py` may not import `untrusted.py`, `tools.py` or the sibling's package.
- Over stdio (Claude Code launching the server locally) there is no HTTP request and no JWT. That
  caller is a person on the user's own machine: person rules apply, including the costly-tool
  confirmation (the Claude Code CLI shows elicitation prompts).
- **Two protocol revisions ask differently.** The SDK every sibling uses (`mcp>=2.3,<3`) accepts
  both. On 2025-11-25 and earlier, the server sends an elicitation request back to the client
  mid-call (`ctx.elicit`), which needs the sessions the servers already run. On 2026-07-28 there is
  no back channel (`ctx.elicit` raises `NoBackChannelError`): the tool returns an
  `InputRequiredResult`, and the client re-sends the whole `tools/call` with the person's answer
  and an SDK-sealed `request_state`. The SDK's `Resolve`/`Elicit` helpers handle both, and the
  confirmation must work on both, since which one Claude Code negotiates can change with a
  release.
- **Elicitation support in Claude's apps is uneven** (community reports, November 2026, not
  Anthropic documentation): it works in the Claude Code CLI. The VS Code extension and Claude
  Desktop (Cowork) are reported to declare the capability but auto-decline or cancel without
  showing anything. Nothing indicates claude.ai web or mobile support it. **In practice, costly
  tools will work from the Claude Code CLI only, until the apps catch up.** The user accepted
  this.
- **Service tokens and the email allow-list.** When `MCP_ALLOWED_EMAILS` is set, `AccessVerifier`
  rejects any JWT whose `email` is not on the list. A service token's JWT has no `email` (it
  carries `common_name`, the token's client ID), so today service tokens are locked out whenever
  the allow-list is in use.

## Design

### 1. Callers

`remote.py` classifies each verified request:

- **person**: the JWT has an `email` (checked against `MCP_ALLOWED_EMAILS` when set, as today);
- **automated**: the JWT has a `common_name` and no `email`, and the `common_name` is in the new
  `MCP_ALLOWED_SERVICE_TOKENS` (comma-separated client IDs). With the setting unset, no service
  token is admitted; this replaces today's accidental lock-out with an explicit one;
- **local**: stdio, or HTTP with no Access configured (loopback only, as today). Treated as a
  person.
- With `--insecure-no-auth` (trusted-LAN testing), callers are `local`.

A JWT with neither `email` nor an admitted `common_name` is refused (403), as an unknown caller.

`MCP_AUTOMATION_TIERS` (default `read,power-off`; may add `everyday`; `costly` is rejected at
startup) sets what automated callers may do. One setting for all service tokens; per-token tiers
are left for when there is more than one job.

### 2. Tiers

| Tier | Meaning | Examples |
|---|---|---|
| `read` | changes nothing | every `*_get_status`, `*_list_*` with `READ_ONLY` annotations |
| `power-off` | turns something off, nothing else | `harmony_power_off`, `sofabaton_power_off`; `webos_set_power`, `onkyo_set_power`, `shield_set_power` **when `state` is `off`** |
| `everyday` | ordinary use | play/pause, inputs, activities, volume (within the caps), launching games and apps, typing into the TV's search |
| `costly` | a chore to undo | `mister_run_update`, `mister_set_ini`, `mister_write_nfc_card`, `shield_reboot`, `batocera_reset_game`, `batocera_quit_game`, `batocera_load_state`, `mister_return_to_menu` |

The `costly` list is the starting point from a survey of today's tools. The existing `destructive`
annotation is not reused: it means "may lose game progress" and misses `mister_set_ini`, which is
annotated as routine. Each sibling's sweep PR settles its own list.

A tier may depend on the arguments: `set_power` tools are `power-off` for `state="off"` and
`everyday` otherwise, so a "make sure it's off" job can't turn things on.

### 3. Declaring tiers in the siblings

`remote.py` exports a decorator, placed directly under `@mcp.tool(...)`:

```python
@mcp.tool(title="Run update", annotations=DESTRUCTIVE)
@tier("costly", confirm="Update the MiSTer's cores and firmware?")
async def mister_run_update(...): ...

@mcp.tool(title="Set power", annotations=ACT_IDEMPOTENT)
@tier(lambda args: "power-off" if args["state"] == "off" else "everyday")
async def webos_set_power(state: Literal["on", "off"], tv: TvArg = None): ...
```

`confirm` is the sentence the person sees; it is required for `costly`. The decorator needs the
tool's MCP `Context` to read the caller and to elicit (left to the builder: how to obtain it
without changing the tool's input schema).

`templates/tests/test_authorization.py` adds a test every sibling runs: list the server's tools
and fail on any tool without a tier, naming it. As with the envelope, `mcp_shared` itself has no
server and skips it.

### 4. Enforcement, per call

1. Work out the tier (calling the function form with the call's arguments).
2. Look up the caller (section 5).
3. `automated` and the tier is not in `MCP_AUTOMATION_TIERS` → refuse: "This tool needs a person;
   automated callers may only <tiers>."
4. `costly` and a person or local caller → ask, through the SDK's `Resolve`/`Elicit` helpers so
   both protocol revisions work: the `confirm` sentence, the server, the tool and its arguments,
   and a single yes/no field. If the client didn't declare elicitation (once per session on the
   older revision, per request on 2026-07-28) → refuse: "This needs your confirmation, and this app
   can't ask for it. Run it from Claude Code." Any answer other than an explicit yes (decline,
   cancel, an error, or no answer within 120 seconds) → refuse: "Not confirmed; nothing was
   done." On the older revision the 120 seconds is how long the server waits; on 2026-07-28 it is
   the sealed state's expiry (the SDK's default is 600 seconds, shortened here).
5. **One yes allows one run, by the person who gave it.** On 2026-07-28 the answer comes back
   inside a re-sent request, and the SDK ties its sealed state to the tool and arguments but to
   the caller only through the SDK's own auth, which `remote.py` doesn't use. So `@tier` binds
   the state to the caller recorded for the request (a custom `bind_principal`), and keeps a
   small in-memory record of used confirmations until they expire, so the same answered request
   can't run the tool twice. A restart clears the record; anything sealed before it has expired
   within 120 seconds anyway.
6. Otherwise run the tool.

A refusal is a normal tool error the model can relay, never a crash. Every refusal and every
confirmation answer is logged at INFO: caller kind, caller (email masked as `remote.py` already
does), tool, tier, outcome.

### 5. Getting the caller to the tool

**Pitfall for the builder:** `AccessMiddleware` verifies each HTTP request, but a Streamable HTTP
session runs in its own long-lived task, so a `contextvars.ContextVar` set in the middleware will
not reliably reach the tool call. The caller must travel with the message: the middleware puts the
verified caller in the ASGI scope (e.g. `scope["state"]["mcp_caller"]`), and `@tier` reads it from
the request the SDK exposes on the tool's context (`ctx.request_context.request`). Over stdio
there is no request: the caller is `local`. Each request is verified on its own, so the caller is
always the one who sent this message, not whoever opened the session.

### 6. Rollout

1. This repo: `remote.py` (caller kinds, `MCP_ALLOWED_SERVICE_TOKENS`, `MCP_AUTOMATION_TIERS`,
   `@tier`, enforcement), template tests for all of it with a fake elicitation client, the
   tier-coverage test, README and docstring updates.
2. Sibling sweep, one PR per sibling: a tier on every tool, the argument-dependent tier on
   `set_power` tools, `confirm` sentences for costly tools, sync `remote.py`, docs listing the
   costly tools.
3. The routing eval (#39 to #46) is unaffected: it never calls servers.

## Decided

- People get full power; authorization targets automated callers and costly tools (user).
- Costly tools need the person's confirmation in a prompt the model can't answer; no home-network
  rule (user, after discussion).
- Automated callers never get costly tools (user); by default they get read and power-off.
- An app that can't show the prompt means the costly tool refuses (user).
- The hub volume cap stays separate (user, in the envelope discussion).

## Calls made by the designer (check these)

- Four tiers: `read`, `power-off`, `everyday`, `costly`; the starting costly list in section 2.
- `set_power` tools are `power-off` only with `state="off"`.
- `MCP_ALLOWED_SERVICE_TOKENS` admits service tokens explicitly; unset means none.
- One `MCP_AUTOMATION_TIERS` setting for all tokens, default `read,power-off`, `costly` rejected.
- Refuse at call time rather than hiding tools from automated callers.
- Confirmation timeout 120 seconds; every refusal and answer logged.
- The confirmation works on both protocol revisions, through the SDK's `Resolve`/`Elicit` helpers
  rather than hand-written `ctx.elicit`.
- One yes allows exactly one run by the same caller: the sealed state is bound to the caller, and
  used confirmations are remembered until they expire.
- `@tier` sits under the envelope's `@guarded`, so a refused call never reaches the envelope.
- stdio and `--insecure-no-auth` callers are treated as people.

## Left to the builder

- How `@tier` obtains the `Context` without changing the input schema.
- Exact refusal and prompt wording, within the meanings above.
- The elicitation schema's shape (a single boolean is enough).
