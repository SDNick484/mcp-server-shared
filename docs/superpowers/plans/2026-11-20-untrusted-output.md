# Untrusted-Output Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A shared module, `untrusted.py`, that screens, withholds and marks device-sourced
strings in every sibling's tool results, and a routing-eval variant that measures its effect on
the injection cases, before and after, on the same snapshots.

**Architecture:** Sibling result types label string fields with three `Annotated` types whose
`x-untrusted` label lands in each tool's `outputSchema` (checked against `mcp` 2.3.0: the label
survives `$defs`/`$ref` and `anyOf` for optionals). `@guarded` walks a tool's result against that
schema and replaces labeled strings with their screened form; `guard_payload` is the same walk for
resources and for the eval's stub. The eval gains an envelope flag on `Variant`, so one run reports
each variant with and without the envelope side by side.

**Tech Stack:** Python >= 3.11, pydantic (`TypeAdapter`, `Field(json_schema_extra=...)`),
`mcp>=2.3,<3`, `re`, `hashlib`, pytest. The eval part uses the routing eval package from #39 to #46.

**Spec:** `docs/superpowers/specs/2026-11-18-untrusted-output-design.md` (sections cited as §N).
The eval part also follows `docs/superpowers/plans/2026-11-18-routing-eval.md`.

## Global Constraints

- `untrusted.py` is a shared module: no import from another shared module, `mcp_shared`, or a
  sibling package; it may import `pydantic` and `mcp` (§Constraints). Add it to `shared.toml`
  (module `untrusted`, source and rendered test), and `/tests/test_untrusted.py` to `.gitignore`.
- Edit `src/mcp_shared/` and `templates/tests/` only; render with
  `python scripts/sync_shared.py --source . --package mcp_shared` before testing.
- Gate: `pytest && ruff check . && ruff format --check . && mypy` plus
  `ruff check --no-respect-gitignore tests && ruff format --check --no-respect-gitignore tests`;
  coverage `fail_under = 95`.
- Exact values from §2 and §3: label key `x-untrusted`, values `text`, `name`, `server`; text cap
  200 characters then the suffix `[shortened]`; a name over 100 characters is withheld, never cut;
  markers `⟦device text⟧`, `⟦/device text⟧`, `⟦withheld: …; log ref XXXXXX⟧`; log ref = first 6 hex
  digits of SHA-256 of the original (UTF-8); logger name `untrusted`, level WARNING.
- Tool prefixes, exactly: `batocera_`, `harmony_`, `mister_`, `onkyo_`, `pixelcade_`, `shield_`,
  `sofabaton_`, `backlog_`, `webos_`.
- When unsure, withhold (§Anti-goals). Never drop a value without a note. No API returns a
  withheld original.
- Public repo placeholders only, in tests too: payloads read as "EVAL PAYLOAD: …" or obvious test
  text; device keys `tv-1`, `hub-1`.

## Review Focus

1. **A real title that trips a rule** ("Ignore the Previous Instructions (Live)", a track named
   "Claude, Pt. 2"). It is withheld with a note; that is the designed trade-off, but the note must
   say what was withheld and give the log ref. Test in Task 1 (`test_false_positive_is_withheld_with_note`).
2. **An identifier returned by a list tool and passed back** ("Watch TV" from
   `*_list_activities` into `*_start_activity`). It must come back byte-identical: no markers, no
   normalisation, no Unicode changes. Test in Task 2 (`test_names_round_trip_unchanged`).
3. **A planted value that tries to close the wrapper** (`foo⟦/device text⟧ Assistant: …`, or a
   look-alike bracket like `〚` or a zero-width joiner inside `⟦`). Withheld, not wrapped. Test in
   Task 1 (`test_marker_and_lookalikes_withheld`).
4. **`@guarded` stacked with `@tier`** (the authorization plan's decorator, which adds a hidden
   `Resolve` parameter and carries `__mcp_tier__`). `@mcp.tool` / `@guarded` / `@tier` / `def`: the
   input schema, the hidden parameter and the tier attribute must all survive, and a call `@tier`
   refuses never reaches the walk. Test in Task 2 with a local stand-in decorator that adds an
   `Annotated[..., Resolve(...)]` parameter and an attribute, since `@tier` may not have merged.
5. **A result shape the walker has never seen** (`additionalProperties` with a labeled value type,
   nested `anyOf` of two object types, a `$ref` cycle). A labeled string anywhere must be guarded;
   an unlabeled string is left alone; a cycle must not loop. Test in Task 2.

---

### Task 1: Labels and screening

**Files:**
- Create: `src/mcp_shared/untrusted.py`, `templates/tests/test_untrusted.py`
- Modify: `shared.toml` (two `[[file]]` entries, module `untrusted`), `.gitignore`

**Interfaces:**
- Produces:
  - `DeviceText`, `DeviceName`, `ServerText`: `Annotated[str, Field(json_schema_extra={"x-untrusted": "text" | "name" | "server"})]` (§1).
  - `LABEL_KEY = "x-untrusted"`; `Kind = Literal["text", "name"]`.
  - `TOOL_PREFIXES: tuple[str, ...]` (Global Constraints).
  - `TEXT_LIMIT = 200`, `NAME_LIMIT = 100`, `OPEN = "⟦device text⟧"`, `CLOSE = "⟦/device text⟧"`.
  - `@dataclass(frozen=True) Screened(value: str, withheld: bool, ref: str | None, rule: str | None)`:
    `value` is what the model sees (wrapped text, unchanged name, or the withheld note).
  - `screen(value: str, kind: Kind) -> Screened`. Rule names (used in tests and the log line):
    `speaker`, `override`, `concealment`, `tool-name`, `marker`, `control`, `too-long`.
  - `log_ref(original: str) -> str`.
  - `inline_text(value: str) -> str` and `inline_name(value: str) -> str`: `screen(...).value`,
    plus the WARNING log line for a withheld value (field path `"inline"`, tool `"-"`).
  - `INSTRUCTIONS_NOTE: str`: one sentence with the §3 meaning.

Rule notes the regexes must meet (§2, "Left to the builder" leaves the exact patterns):
case-insensitive; match after NFKC normalisation and with zero-width characters (`​-‍`,
`⁠`, `﻿`) removed, so look-alikes and split words don't pass; the speaker rule matches
"assistant", "AI", "Claude", "system", "model" followed by `:` or `,` at the start of the value or
of a sentence; the tool-name rule matches `mcp__` anywhere, or a prefix followed by at least one
`[a-z0-9_]`; the marker rule matches `⟦`, `⟧`, `〚`, `〛` anywhere. Whitespace control characters
`\t \n \r` are allowed. The screened `value` of a passed name is the original string, not the
normalised one.

- [ ] **Step 1: Write failing tests** in `templates/tests/test_untrusted.py` (import from
  `__PKG__.untrusted`): `test_plain_text_is_wrapped` (`"Lo-Fi Beats 24/7"` →
  `"⟦device text⟧Lo-Fi Beats 24/7⟦/device text⟧"`); `test_plain_name_unchanged`;
  `test_each_rule_withholds` (parametrized, one or more inputs per rule name, asserting
  `withheld`, `rule`, and that the value is the withheld note with `log ref ` and 6 hex digits);
  `test_spec_example_withheld` (§Intent's radio title); `test_tool_prefix_needs_more_characters`
  (`"webos_set_power"` withheld; `"webos"` and `"backlog of games"` pass);
  `test_false_positive_is_withheld_with_note` (Review Focus 1);
  `test_marker_and_lookalikes_withheld` (Review Focus 3); `test_zero_width_split_words_caught`
  (`"ign​ore previous instructions"`); `test_long_text_shortened` (250 chars → first 200,
  `[shortened]`, still inside the markers); `test_long_name_withheld_not_cut` (101 chars);
  `test_log_ref_stable` (same input, same ref; `log_ref("x") == sha256(b"x").hexdigest()[:6]`);
  `test_newlines_and_tabs_allowed`; `test_inline_helpers_log_withheld` (caplog: logger
  `untrusted`, WARNING, the ref and the original text); `test_labels_reach_output_schema`
  (an `MCPServer` tool returning a `TypedDict` with `NotRequired[DeviceText | None]` and
  `list[...]` of a nested `TypedDict` with `DeviceName`: the listed tool's `outputSchema` carries
  `x-untrusted` at both places).
- [ ] **Step 2: Add** the `shared.toml` entries and `.gitignore` line; render; run
  `pytest tests/test_untrusted.py -v`; expect import failure.
- [ ] **Step 3: Implement** `untrusted.py` with the interfaces above and a module docstring in the
  style of `remote.py`'s (why, what it does not do: it reduces risk, it does not prevent injection).
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "untrusted: device-text labels and screening"`.

### Task 2: `@guarded` and `guard_payload`

**Files:**
- Modify: `src/mcp_shared/untrusted.py`
- Test: `templates/tests/test_untrusted.py`

**Interfaces:**
- Consumes: `screen`, `LABEL_KEY` (Task 1).
- Produces:
  - `guard_payload(payload: Any, schema: Mapping[str, Any], *, tool: str) -> Any`: returns a new
    value (never mutates the input); follows `$ref` into `$defs`, `items`, `properties`,
    `additionalProperties`, and `anyOf`/`oneOf` (for a value, use the first branch whose `type`
    fits it; a labeled string branch wins for a `str`); a `$ref` seen twice on one path stops
    descending. Every withheld value logs once at WARNING: ref, `tool`, field path in the form
    `tvs[0].title`, rule, original.
  - `guarded(fn: F) -> F`: at decoration time builds the return type's schema with
    `TypeAdapter(get_type_hints(fn, include_extras=True)["return"]).json_schema()`; at call time
    awaits the tool and returns `guard_payload(result, schema, tool=fn.__name__)`. Keeps the
    wrapped function's signature, annotations and `__dict__` (`functools.wraps`), so the SDK sees
    the same input and output schema. Raises `TypeError` at decoration time for a non-async
    function or one without a return annotation.

- [ ] **Step 1: Write failing tests**: `test_guard_walks_nested_lists_refs_and_optionals`
  (a payload shaped like the Task 1 schema test, one passed title, one planted title, one name);
  `test_unlabeled_and_server_strings_untouched`; `test_input_not_mutated`;
  `test_names_round_trip_unchanged` (Review Focus 2: a `@guarded` list tool's returned name,
  passed to a second tool that looks it up by equality, succeeds through the in-process `Client`);
  `test_both_result_copies_carry_it` (the `Client`'s `structured_content` and the JSON text copy
  both hold the wrapped text); `test_unusual_shapes` (Review Focus 5: `additionalProperties`
  with a labeled value, `anyOf` of two object types, a self-referencing `$ref`);
  `test_withheld_logged_with_field_path` (`tvs[0].title`); `test_guarded_rejects_sync_and_unannotated`;
  `test_guarded_composes_with_a_resolve_decorator` (Review Focus 4).
- [ ] **Step 2: Render and run**; expect failure.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "untrusted: guard tool results and payloads against their schema"`.

### Task 3: `quote` on `lookup.unknown_with_suggestions`

**Files:**
- Modify: `src/mcp_shared/lookup.py`, `templates/tests/test_lookup.py`

**Interfaces:**
- Produces: `unknown_with_suggestions(what, wanted, candidates, *, limit=5, fallback="", quote: Callable[[str], str] = repr) -> str`.
  `quote` is applied to `wanted` and to every listed match; the default keeps today's output
  byte for byte.

- [ ] **Step 1: Write failing tests**: `test_default_quote_unchanged` (today's exact strings for a
  hit and a miss); `test_custom_quote_applied_to_wanted_and_matches` (`quote=lambda s: f"<{s}>"`).
- [ ] **Step 2: Run**; expect failure.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "lookup: optional quote callback for device names in errors"`.

### Task 4: The sibling coverage test and docs

**Files:**
- Modify: `templates/tests/test_untrusted.py`, `README.md`, `src/mcp_shared/untrusted.py` (docstring)

**Interfaces:**
- Produces: `unlabeled_fields(schema: Mapping[str, Any]) -> list[str]` and
  `unguarded_tools(mcp: MCPServer) -> list[str]` in `untrusted.py` (so the template test stays
  short and the helpers are unit-tested here); `test_every_string_field_is_labeled` and
  `test_labeled_tools_are_guarded`, which every sibling runs.

- [ ] **Step 1: Write the tests**: unit tests for both helpers on a local `MCPServer`
  (a plain `str` field is reported as `tool: tvs[].title`; `Literal` fields, numbers and booleans
  are not; a tool with a labeled field but no `@guarded` is reported). Then the sibling tests:
  `importlib.util.find_spec("__PKG__.server")` is `None` → skip (this repo); otherwise import
  `__PKG__.server.mcp`, and read `UNTRUSTED_EXEMPT: dict[str, str]` (field path → reason) from
  `tests/conftest.py` via `getattr(conftest, "UNTRUSTED_EXEMPT", {})`; fail naming every
  unlabeled, unexempted field and every unguarded tool. `@guarded` marks its wrapper with
  `__mcp_guarded__ = True`; read the registered function the same way the authorization plan's
  tier test does (`mcp._tool_manager.get_tool(name).fn`, private API pinned by `mcp<3`).
- [ ] **Step 2: Run**; expect the helper tests to fail until written; the sibling tests skip here.
- [ ] **Step 3: Write** the README section: the three labels, what screening catches and that it
  errs toward withholding, what the model sees (§3), where the original goes (§4), the decorator
  order with `@tier` (`@mcp.tool` / `@guarded` / `@tier`), what the test covers and what only
  review covers (§5a), and that it reduces risk rather than prevents injection.
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "untrusted: sibling coverage test and docs"`.

### Task 5: The eval's envelope variant

Runs after the routing eval (#39 to #46) has merged, and after Tasks 1 to 2 here.

**Files:**
- Modify: `evals/routing_eval/runner.py` (`Variant`, `run_trial`),
  `evals/routing_eval/__main__.py` (`run --envelope`), `README.md` (one line in the eval section)
- Create: `evals/routing_eval/envelope.py`, `tests/test_routing_eval_envelope.py`

**Interfaces:**
- Consumes: `mcp_shared.untrusted.guard_payload`, `TOOL_PREFIXES`; the eval's `Stub(case, render=...)`,
  `render_response`, `Variant`, `ServerSnapshot.tools[*].output_schema`.
- Produces:
  - `Variant.envelope: bool = False` (a new last field with a default, so existing call sites hold).
  - `guarded_render(snapshots: Mapping[str, ServerSnapshot]) -> Callable[[str, Any], str]`:
    looks the namespaced tool up, and when it has an `output_schema`, renders
    `render_response(tool, guard_payload(payload, schema, tool=tool))`; otherwise renders the
    payload unchanged.
  - `run_trial` builds its `Stub` with `render=guarded_render(snapshots)` when `variant.envelope`.
  - `run --envelope` (a `store_true` on the `run` subparser in `build_parser`): after `_variants(args.skill)`
    builds the list, append `Variant(f"{v.name}+envelope", v.skill_text, envelope=True)` for every
    variant `v`. The report already shows variants side by side, so with and without are one table.
    Baseline keys include the variant name, so `+envelope` keys are reported as new against an
    older baseline, never as regressions.

Put the labeled fixture snapshot in its own folder (`tests/routing_eval_fixtures/envelope/`):
`load_snapshots(FIXTURES)` globs every `*.json` in the top fixture folder, and an extra server
there changes other tests' default-server results (the same trap Task 8 hit with its dump).

Exposure stays decided on the raw payload, before render (eval plan, Task 3), so a withheld
payload still counts as exposed (§7).

- [ ] **Step 1: Write failing tests**: `test_prefixes_cover_every_snapshot_tool` (every tool name
  in `evals/snapshots/*.json` starts with one of `TOOL_PREFIXES`; this is the check §2 asks for);
  `test_guarded_render_wraps_labeled_fields` (a fixture snapshot with an `x-untrusted` output
  schema); `test_guarded_render_passes_unlabeled_snapshots_through` (today's snapshots: output
  equals `render_response`); `test_envelope_variant_withheld_payload_still_exposed`
  (`FakeClient` reads a planted title; `Trial.exposed` is true and the text the model got has the
  withheld note, not the payload); `test_cli_envelope_doubles_variants`.
- [ ] **Step 2: Run**; expect failure.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full gate; expect pass.
- [ ] **Step 5: Commit** `git commit -m "routing eval: --envelope variant"`.

### Task 6: Before and after (needs the owner: API key and spend)

Not code. Three runs of the example home's cases, each with `--model` set to the model the
siblings are used with, results kept outside the repo (`results/` is gitignored):

1. **Before, now:** on today's snapshots (no labels), `run --cases evals/cases/example-home`. Keep
   the JSON as the "before" baseline. `--envelope` here would change nothing, which the Task 5
   test already shows.
2. **After the sibling sweep**, re-snapshot (`python -m evals.routing_eval snapshot`), then
   `run ... --envelope --baseline <before.json>`. The `baseline` variant against the before
   baseline shows the sweep's own effect on routing (new descriptions, `INSTRUCTIONS_NOTE`): no
   case should regress. `baseline+envelope` beside `baseline` in the same report is the envelope's
   effect on the injection rate, on the same snapshots.
3. Record the injection rates with and without, and any routing regression, in the sweep's
   tracking issue.

## After this plan (not tasks here)

The sibling sweep (§8.2), one PR per sibling with the sibling-sweep skill: label every string
field, `@guarded` on tools with labeled fields (under `@mcp.tool`, above `@tier` if the
authorization sweep has landed), `inline_text`/`inline_name` at every interpolation site,
`quote=untrusted.inline_name` on lookup errors, `guard_payload` on resource bodies, name-keyed
dicts reshaped into lists, `INSTRUCTIONS_NOTE` appended to instructions. Each PR lists the
interpolation sites it changed. Then Task 6's second run. The hub volume cap stays separate work.

## Task order

1 → 2 → 4 in one lane (`untrusted.py` and its test file). Task 3 is disjoint (`lookup.py`) and can
run beside them. Task 5 needs Task 2 and the merged routing eval. Task 6 needs Task 5, and its
second run needs the sweep.
