# Routing eval: does the model send a request to the right server?

Status: draft for review, 2026-11-17

## Intent

The sibling servers overlap. With the AV servers installed together, "mute the TV" has four
plausible targets (`onkyo_set_mute`, `webos_set_mute`, a Harmony `send_command`, a Sofabaton
`press_button`), and "switch to the Xbox" has at least three.

Most of those overlaps are harmless. The ones that matter involve the **hub servers** (Harmony,
Sofabaton). A hub is a state machine: it believes it knows which activity is running, which
devices are on, and which input each one is on. When a model changes that state directly on a
device (`onkyo_set_input`, `webos_set_power`, `webos_select_source`), the device changes and the
hub doesn't know. The next activity change misfires, and Harmony's Fix (which restores the room to
the state the hub *believes* it's in) silently undoes what the user asked for. Some direct calls
recover on their own (power off is idempotent), most don't.

Whether a request should go through a hub depends on how a given home is wired: which devices sit
under which hub's activities. So the right answer is per-home, not universal.

**The harness answers one question, repeatably:** given a home's wiring, its current state, and a
request, does the model route its first state-changing call to the right server and tool? It is run
when tool descriptions change, when a new model ships, and when the home theater skill changes,
because those are the things that move routing.

## Goals

1. Measure routing correctness per case as a pass rate over N trials, per model.
2. Score the **first state-changing call**. Read calls (`*_get_status`, `*_list_*`) are allowed
   and expected before it.
3. Use the servers' **real** tool lists (names, descriptions, input schemas, annotations), since
   that is what the model routes on.
4. Keep household wiring out of the public repo: the public repo ships the harness and a fictional
   example home; real cases live wherever the user keeps them.
5. Run each case with and without a skill file in the system prompt, to measure what the home
   theater skill adds.
6. Detect regressions against a saved baseline.

## Non-goals

- Testing device behavior or protocol correctness. The siblings' own suites (simulators, contract
  tests, recorded fixtures) already do that.
- Running servers against simulators during an eval (approach B below). Possible later, per sibling.
- Running on every PR. Evals call a paid API and are nondeterministic.
- Testing multi-step plans beyond the first state-changing call.

## Anti-goals (would make this a failure even if it works)

- **Flaky verdicts.** A pass/fail that flips run to run trains the user to ignore it. Hence N trials
  and pass rates, not single runs.
- **Household data in the public repo** (CLAUDE.md conventions): no real hub names, activity names,
  room names or addresses in code, docs, examples or committed results.
- **Requiring changes to every sibling before the first eval runs.**
- **Breaking the repo invariant "no network in test suites."** The harness's own pytest suite uses
  a fake model client.

## Constraints

- Lives in mcp-server-shared but is **not a synced file**: like `scripts/sync_all_siblings.py`, it
  exists only here. Nothing is added to `shared.toml`.
- Python >= 3.11, ruff (same rule set), mypy strict, coverage gate as the rest of the repo.
- The model API dependency (`anthropic`) goes in a new optional extra, `[eval]`, so `pip install
  -e ".[dev]"` and the siblings are unaffected.
- Sibling servers each pin their own deps; they must not share one virtualenv.

## Approaches considered

| | What | Trade-off |
|---|---|---|
| **A. Real tool lists, scripted state (chosen)** | Pull each sibling's real tool list once into a snapshot; answer read calls from the case's scripted state; drive the conversation through the Claude API; score the first state-changing call | Fast, cheap, tests exactly the routing decision. Only as faithful as the scripted read responses. |
| B. Real servers on simulators | Run each sibling's full server against its simulator, seeded with the case's state; harness is an MCP client | Highest fidelity (also catches confusing status output). Every sibling needs a seedable simulator first. |
| C. Claude Code headless | `claude -p` with all servers configured; parse transcripts | Most realistic client, but hard to control and score, and results move with CLI releases too. |

Chosen: **A**, by the user. B can be adopted per sibling later; the case format below is designed so
a case written for A remains valid under B.

## Design

### Layout (in this repo)

```
evals/
  routing_eval/            # the package (added to mypy `files`)
    snapshot.py            # pull tool lists from sibling checkouts
    cases.py               # load and validate case files
    stub.py                # answers read calls from case state; records state-changing calls
    runner.py              # conversation loop against the model API
    score.py               # verdict per trial, pass rate per case
    report.py              # markdown + JSON output, baseline comparison
    __main__.py            # CLI: python -m evals.routing_eval ...
  snapshots/               # committed: <server>.json, one per sibling
  cases/example-home/      # committed: fictional home, generic names only
tests/test_routing_eval_*.py   # harness tests, fake model client, no network
```

### 1. Tool snapshots

`python -m evals.routing_eval snapshot` walks sibling checkouts (same discovery as
`sync_all_siblings.py`: `../mcp-server-*`, `--parent-dir`, `--only`). For each one it runs, inside
**that sibling's own `.venv`**, a small extractor that imports `<pkg>.server`, calls
`await mcp.list_tools()`, and prints JSON. Verified on Harmony: this works in-process with no
device and no config. Every sibling's `server.py` exposes a module-level `mcp = MCPServer(...)`.

Each snapshot records server name, sibling commit, the server's `instructions` string, and per
tool: name, description, input schema, annotations. Snapshots are committed. Tool descriptions are already public, and a diff in
`evals/snapshots/` is exactly the signal that routing may have moved.

### 2. How tools are presented to the model

Tools are namespaced the way Claude Code presents MCP tools: `mcp__<server>__<tool>` (e.g.
`mcp__harmony__harmony_start_activity`, `mcp__onkyo__onkyo_set_input`). Descriptions and schemas
are passed through unchanged. Each installed server's `instructions` go into the system prompt as
a client would present them (one labeled block per server), because they carry routing advice
today ("Prefer activities", "Always adjust volume here").

A home or a single case can replace a server's instructions:

```toml
[instructions.onkyo]
text = """Controls Onkyo/Integra AV receivers... If a universal remote hub manages this receiver,
change its power and input through the hub's activities instead."""
```

so a proposed rewrite can be measured before any sibling changes. The report marks overridden
instructions. A case or run chooses which servers are installed (default: the AV
set: harmony, sofabaton, onkyo, webos, shieldtv). Extra servers can be added as distractors.

### 3. Read vs. state-changing

Classified from the snapshot's annotations, which every sibling sets via the shared `tools.py`
presets: `read_only_hint=True` → read; anything else → state-changing. A tool with no annotations
counts as state-changing (the MCP spec's default) and the snapshot step warns about it.

A home may override the classification with `[home] treat_as_read = [...]` (namespaced names).
Example: `onkyo_list_stations` is annotated `ACT_IDEMPOTENT` because browsing moves the receiver's
network menu; a home where that doesn't matter can treat it as a read. The report lists overrides
in effect so they aren't invisible.

### 4. Case format

A case file (TOML, like the repo's other config) describes one home and many requests against it:

```toml
[home]
name = "example-home"
servers = ["harmony", "onkyo", "webos", "shieldtv", "sofabaton"]
notes = """Free text describing the wiring, for humans and (optionally) as model context."""

# What read tools return in this home's starting state. Keyed by namespaced tool name.
# A tool can have several entries: `when` matches on a subset of the call's arguments
# (e.g. `when = { tv = "tv-1" }` for webos_get_status). The most specific matching entry wins;
# an entry with no `when` is the fallback.
[[reads."mcp__harmony__harmony_get_status"]]
response = { hubs = [{ hub = "hub-1", reachable = true, on = true, activity = "Watch Streamer" }] }

[[case]]
id = "switch-source-under-hub"
request = "Switch to the game console."
# Optional per-case overrides of the home's read responses:
reads."mcp__harmony__harmony_get_status".response = { hubs = [ ... ] }
expect.allow = [
  { tool = "mcp__harmony__harmony_start_activity", args = { activity = "Play Console" } },
]
expect.forbid = ["mcp__onkyo__onkyo_set_input", "mcp__webos__webos_select_source"]
trials = 5            # optional; default from CLI
```

Scoring looks at the **first model response that contains any state-changing call**, and at
*every* state-changing call in that response (the model can return several in parallel; parallel
tool use stays on, as in real clients). The trial's verdict is the worst of them:

- any call matches `forbid` → **fail (forbidden)**: the hub-desync case this harness exists for,
  even if an allowed call sits beside it
- else any call matches no `allow` entry → **fail (other)**
- else (every call matches an `allow` entry: tool equal, listed args a subset match) → **pass**
- no state-changing call before the turn limit or end of turn → **abstain**. A case may set
  `expect.abstain_ok = true` when asking a clarifying question is acceptable.
- API/harness error → **error** (excluded from the rate, reported).

Policy encoded by the cases, not by the harness: power and input/source changes on a hub-managed
device must go through the hub; volume and mute may go direct. The harness does not hard-code
this, so a home where the hub *does* track volume can say so.

Unlisted reads: if the model calls a read tool and no entry matches its arguments, the stub
returns a plain "no data" result, and the report flags the case (with the tool and arguments) as
under-specified.

### 5. Runner

For each (model × variant × case × trial): system prompt (a short neutral one, plus the skill
file's contents when the variant has one, plus `home.notes` only when `--with-notes` is given;
it applies to every variant in the run and is recorded in the results, not a variant of its own), the request as the user
message, the namespaced tool list. Loop: model → read calls answered by the stub → model, until
the first state-changing call, end of turn, or `--max-turns` (default 6). The state-changing call
is **not** executed; it is recorded and the trial ends.

Uses prompt caching for the tool list (identical across trials). Concurrency is bounded
(`--concurrency`, default 4) and retries on 429/5xx with backoff.

### 6. Variants

`--skill PATH` (repeatable) adds a variant whose system prompt includes that file. The baseline
variant (no skill) always runs, so the report shows the skill's lift per case.

### 7. Output

- `results/<timestamp>.json`: every trial (model, variant, case, transcript of calls, verdict).
  `results/` is gitignored by default, since private cases' transcripts carry household names.
- Markdown summary to stdout: one row per case, pass rate per model × variant, forbidden calls
  highlighted.
- `--baseline FILE` compares pass rates and exits 1 if any case drops by more than
  `--tolerance` (default 0.2, i.e. one trial in five). Exit 0 otherwise; 2 on errors, matching
  the repo's convention.

### 8. Where real cases live

`--cases DIR` points anywhere. Real cases live outside this repo (a private repo or a local
directory). The public repo ships only `evals/cases/example-home/` with generic names (`hub-1`,
`tv-1`, `receiver-1`, activities like "Watch Streamer").

### 9. Deriving a home from a hub dump (later)

Harmony's `doctor --dump` already records each activity and the devices it uses. A follow-up
command can turn a dump into a case-file skeleton (`[home]`, `[reads.*]` for status and activity
list) so the wiring doesn't have to be hand-written. It writes to the private cases dir only.
Sofabaton equivalent if and when it has a dump.

## Decided
- Server `instructions` are captured in snapshots and shown to the model; homes and cases may
  override them (user).

- Approach A (user).
- Wrong routing means a direct state change on a hub-managed device, chiefly power and
  input/source; volume and mute direct are acceptable (user).
- Wiring is per-home, so real cases stay private (user).

## Follow-on work (outside this harness)

The current instructions bake one home's chain into public servers (Shield "connects into Onkyo AVR
input 'strm-box'"; Onkyo "Always adjust volume here"; webOS and MiSTer assume Onkyo → LG), and none
mentions the hubs. The user wants these generalized: most homes use TV volume and native TV apps.
That is a cross-sibling change (sibling-sweep), measured with this harness before and after:
conditional, device-reported rules like webOS's `volume_owner` are the pattern to follow. Pixelcade's
cabinet-ecosystem text is generic enough and stays.

## Calls made by the designer (check these)

- Parallel calls: any forbidden call in the first acting response fails the trial.
- `when` on reads matches an argument subset; most specific wins.
- `--with-notes` is a run flag, not a variant.
- `[home] treat_as_read` overrides annotation-based classification, listed in the report.
- Tool names presented as `mcp__<server>__<tool>`; default AV server set; N=5 trials; regression
  tolerance 0.2; results gitignored; `[eval]` optional extra for the `anthropic` SDK.

## Left to the builder

- Exact snapshot JSON shape; extractor implementation details.
- TOML schema details beyond the example (validation messages, defaults).
- Neutral system prompt wording.
- Report formatting.
