# mcp-server-shared

The single source of truth for code the sibling MCP servers each carry a copy of:

| Module | Source in this repo | Destination in sibling | What it is |
| --- | --- | --- | --- |
| `remote` | `src/mcp_shared/remote.py` | `src/<pkg>/remote.py` | HTTP behind Cloudflare Access (incl. `/healthz` and `<path>/healthz` readiness with the running commit (see [Health and the running commit](#health-and-the-running-commit)), and CORS `OPTIONS` preflight), and per-tool authorization: `@tier` (see [What each caller may do](#what-each-caller-may-do)) |
| `logsafe` | `src/mcp_shared/logsafe.py` | `src/<pkg>/logsafe.py` | log redaction (IPv4/IPv6 addresses, MACs, secrets incl. URL query tokens; `register_custom_pattern` for device-specific ones) |
| `tasks` | `src/mcp_shared/tasks.py` | `src/<pkg>/tasks.py` | `stop_task`, `stop_tasks` and `spawn_task`: starting background tasks and stopping ones that drop a cancel |
| `ratelimit` | `src/mcp_shared/ratelimit.py` | `src/<pkg>/ratelimit.py` | `TokenBucket` (`take`, `refund`, async `acquire`/`reserve`), `KeyedTokenBucket` and `refusal`: per-device rate limits |
| `registry` | `src/mcp_shared/registry.py` | `src/<pkg>/registry.py` | the assumption registry's record, statuses, queries and doctor summaries (`doctor_lines`, `doctor_summary`; each sibling's list stays in its `assumptions.py`) |
| `lookup` | `src/mcp_shared/lookup.py` | `src/<pkg>/lookup.py` | `norm`, `suggest`, and `unknown_with_suggestions`: normalized matching and closest candidate suggestions on lookup misses |
| `tools` | `src/mcp_shared/tools.py` | `src/<pkg>/tools.py` | standard MCP `ToolAnnotations` presets (`READ_ONLY`, `ACT`, `ACT_IDEMPOTENT`, `DESTRUCTIVE`, `OPEN_WORLD_READ`); `trim_tools(mcp)`, called after the last `@mcp.tool()`, shortens what `tools/list` sends (drops generated schema titles, flattens optional-argument unions, unwraps descriptions) |
| `deploy` | `deploy/alpine/commit.sh` | `deploy/alpine/commit.sh` | sourced by the sibling's `install.sh`: pins the commit it installs and stamps `$PREFIX/COMMIT` |
| (each) | `templates/tests/test_*.py` | `tests/test_*.py` | each module's tests, with `__PKG__` for the package name |
| (none) | `scripts/sync_shared.py` | `scripts/sync_shared.py` | the script that copies all of the above |

The sibling repos are:
- `mcp-server-batocera`
- `mcp-server-harmony`
- `mcp-server-misterfpga`
- `mcp-server-onkyo`
- `mcp-server-pixelcade`
- `mcp-server-shieldtv`
- `mcp-server-sofabaton`
- `mcp-server-steam-backlog`
- `mcp-server-webos`

## Why not a PyPI package?

Each sibling server is meant to be runnable with `uvx mcp-server-<device>` without pulling
a tree of internal dependencies. The shared modules are small (~100–300 lines each), have
no third-party dependencies beyond what the servers already use (`mcp`, `httpx`), and are
stable. Vendor-copying them keeps the siblings self-contained while keeping a single place
to fix bugs.

## How sync works

The manifest lives **here**: `shared.toml` lists every shared file, which module it belongs
to, and where it lands in a sibling (`src/__PKG__/remote.py`, `tests/test_remote.py`, ...).
Each sibling carries one small file, `shared.lock`, plus the copies themselves:

```toml
# shared.lock, in a sibling
repo = "SDNick484/mcp-server-shared"
commit = "73e65ec9e63c8c3a1b3831697da56c5b86854a5c"   # a commit on this repo's main
version = "v0.1.0"                                      # written by --update: the tag on that commit, if any
package = "webos_mcp"                                   # what replaces __PKG__
exclude = ["registry"]                                  # optional: modules this sibling doesn't take
```

`version` is a label for people (`--check` and `sync_all_siblings.py status` show it); the
`commit` is still the pin. `--update` writes the release tag (`vX.Y.Z[-pre]`) pointing at the
commit it moves to, and leaves the key out when that commit is untagged. A script from before
`version` existed warns that it doesn't know the key and otherwise ignores it; deleting the line
is always safe.

Running the sync script in a sibling:

```sh
# Write the copies as of the locked commit (fixes a copy someone edited by hand):
python scripts/sync_shared.py

# Don't write; exit 1 if any copy differs from the locked commit (what CI runs on every PR):
python scripts/sync_shared.py --check

# Don't write; exit 1 if any copy differs from mcp-server-shared main (CI, weekly):
python scripts/sync_shared.py --check --latest

# Move the lock to main and write whatever changed:
python scripts/sync_shared.py --update

# Show what either of the above would change, without writing:
python scripts/sync_shared.py --update --dry-run

# Try a local checkout of mcp-server-shared (a branch you're working on) in this sibling:
python scripts/sync_shared.py --source ../mcp-server-shared
```

Exit status: `0` everything matches or was written, `1` a copy differs (`--check`; the diff
is printed), `2` anything else (bad arguments, an unreadable lock, the network).

**The locked commit must be one `main` has pointed at.** `--update` guarantees that; a lock
written any other way (by hand, or from a branch checkout) can pin a PR branch's head, which
the squash merge replaces and the branch deletion orphans. GitHub keeps serving such a commit
for a while, so everything looks fine until it doesn't. The script warns when a lock isn't on
`main`'s history; the fix is `--update`.

`scripts/sync_shared.py` is itself a shared file, so `--update` brings in the newest script;
`scripts/sync_all_siblings.py` (this repo only) runs it across every `../mcp-server-*` checkout.

### Automated sync across all siblings

From this repo, `scripts/sync_all_siblings.py` acts on every sibling checkout next to it
(`../mcp-server-*` with a `shared.lock`; `--parent-dir` points elsewhere, `--only webos,harmony`
narrows it down):

```sh
# Clone the siblings you don't have yet and fast-forward the ones you do:
python scripts/sync_all_siblings.py clone              # --https if you don't use SSH keys

# Check drift: is each sibling at its locked commit (or, with --latest, at main)?
python scripts/sync_all_siblings.py status [--latest]

# Try a local, unpushed change in every sibling, with each sibling's CI checks:
python scripts/sync_all_siblings.py update --source . --test

# Release: move every sibling to main and open one PR per sibling that passes its checks:
python scripts/sync_all_siblings.py update --pr --setup-venvs
```

`update --pr` does, for each sibling:

1. Fetch, then fast-forward its default branch. It must be checked out and clean: a sync commit
   holds only sync output, so local work stops the sibling instead of being stashed or
   rebased. Unpushed commits that are all earlier `Sync shared to …` commits are dropped
   (the log prints how to undo it) and redone.
2. Skip it if a PR for this exact sync (`sync-shared-<sha>`) is already open.
3. `scripts/sync_shared.py --update`.
4. Run what its CI test job runs, in order: `ruff check .`, `ruff format --check .`, `mypy`,
   `pytest`, in the sibling's `.venv` (`--setup-venvs` creates a missing one with
   `pip install -e ".[dev]"`). If one fails, the sync is undone so the next run starts
   clean; `--keep-failed` leaves it in place to look at.
5. Commit (`Sync shared to <sha>`, authored with the account's noreply address) on a
   `sync-shared-<sha>` branch, push it, and open a PR with `gh`. The PR lists the files,
   links the shared diff and reports the local checks. Older sync PRs still open are
   flagged as superseded, not closed.
6. With `--auto-merge` (off by default): `gh pr merge --auto --squash` on the new PR, so it
   merges itself once CI passes. Skipped, with the reason in the table, where the repo has
   "Allow auto-merge" off, or where the default branch requires no status checks (branch
   protection or a ruleset): there GitHub's auto-merge merges at once, before CI has run. As of
   2026-11 every sibling is in both cases, so it takes a ruleset requiring the CI jobs, plus the
   repo setting, before it does anything.

`--commit` stops after a local commit on the default branch; `--push` pushes that commit
straight to it instead of opening a PR. `--no-test` skips the local checks (CI still runs
them). The checks are the point: rendered test templates are linted only in the siblings,
so a template that passes here can still fail ruff in all nine.

## Design choices

Choices worth knowing about, and why:

- **Format 2 lockfile:** The lockfile format was bumped from 1 to 2 when `scripts/sync_shared.py`
  itself became a shared file. Format 1 locks had a bare `commit = "..."` without the `format`
  field. Format 2 sync scripts can read format 1 locks and auto-upgrade them on `--update`.
- **`--source` for offline work:** You can point `--source` at a local checkout of
  `mcp-server-shared`. That's how we test changes to shared modules across all siblings before
  pushing them here.
- **`shared.toml` is the manifest:** `mcp-server-shared` has its own `shared.toml` listing all
  available modules. Siblings declare which subset they consume in their own `shared.toml`.
  A sibling can adopt `logsafe` without adopting `remote`, and the sync script will only copy
  what's declared.
- **Byte-for-byte identical:** The shared files in `src/mcp_shared/` are copied as-is into each
  sibling's package. They do **not** use templating or string replacement. If a shared file
  needs to know the package name, it's not a shared file: it belongs in the sibling.
- **Templates for tests:** The shared test files in `templates/tests/` use `__PKG__` as a
  placeholder for the package name. The sync script replaces `__PKG__` with the sibling's
  package name (from `shared.toml`) when copying them. This lets each sibling test its copy
  of the shared modules with its own import paths.
- **Templates stay out of linting:** The files in `templates/tests/` are excluded from ruff
  and mypy in this repo because `__PKG__` isn't a valid Python identifier. To test them here,
  `scripts/sync_shared.py --source . --package mcp_shared` renders them into `tests/`, which
  is ignored by git. Ruff skips gitignored files by default, so lint the rendered copies with
  `ruff check --no-respect-gitignore tests` (CI does): siblings commit and lint them.
- **No orphan pruning:** `--update` overwrites files declared in `shared.toml`. It does not
  delete files that were previously synced but removed from `shared.toml`. You have to clean
  them by hand if they're unused.
- **Exit codes:** 0 in sync, 1 drift, 2 anything else (network, a bad lock, bad arguments).

## What each caller may do

`remote.py` settles who sent each request, and `@tier` settles what they may do. Three kinds of caller:

| Caller | Who | May use |
| --- | --- | --- |
| person | an Access login (the JWT has an `email`; `MCP_ALLOWED_EMAILS` still applies) | every tier; a `costly` call first asks for their yes |
| automated | an Access service token whose client ID (the JWT's `common_name`) is in `MCP_ALLOWED_SERVICE_TOKENS` | the tiers in `MCP_AUTOMATION_TIERS`, never `costly` |
| local | stdio, or HTTP with no Access (this machine only), including `--insecure-no-auth` | treated as a person, confirmation included |

A JWT with no `email` and no admitted `common_name` gets a 403, as does one with no assertion at all.

Two settings, both read by `http_config`:

- `MCP_ALLOWED_SERVICE_TOKENS`: comma-separated service-token client IDs, e.g. `token-1.access`. Unset admits
  none.
- `MCP_AUTOMATION_TIERS`: what an admitted token may use. Unset means `read,power-off`; it may add
  `everyday`; set blank, it means none. `costly` or a name that is not a tier refuses startup
  (`ConfigError`). One setting for every token.

Every tool has one tier:

| Tier | Meaning | Examples |
| --- | --- | --- |
| `read` | changes nothing | every `*_get_status` and `*_list_*` |
| `power-off` | turns something off, nothing else | `harmony_power_off`; `*_set_power` when `state` is `off` |
| `everyday` | ordinary use | play/pause, inputs, activities, volume, launching an app |
| `costly` | a chore to undo | a firmware update, rewriting a device's config, a reboot, losing unsaved game progress |

Each sibling settles its own `costly` list. The tier may depend on the call's arguments, so a "make sure it's
off" job can switch a TV off but not on.

### Behaviour change: service tokens

A Cloudflare Access service token used to get in whenever it passed Access, since only people's logins are
checked against `MCP_ALLOWED_EMAILS`. Now it is refused (403) unless its client ID is in
`MCP_ALLOWED_SERVICE_TOKENS`, even with no email allow-list. A deployment that relies on a service token must
set the variable, and gets `read` and `power-off` unless it sets `MCP_AUTOMATION_TIERS` too.

### Costly tools and the confirmation prompt

A person's `costly` call is answered with an MCP elicitation prompt: the tool's `confirm` sentence, the server,
the tool and its arguments, and one yes/no field, which the model cannot answer. Anything but an explicit yes
(decline, cancel, no answer in 120 seconds) refuses the call, and one yes runs the tool once, for the person who
gave it. Both protocol revisions are handled.

This works only where the app can show the prompt, which today means the Claude Code CLI. Elsewhere (an app
that doesn't declare elicitation, or declares it but shows nothing) the call is refused with "Run it from Claude
Code", and nothing is done. That is deliberate: there is no fallback the model could answer for the person.

### Wiring a sibling

```python
from mcp.server.mcpserver import MCPServer
from .remote import request_state_security, tier

mcp = MCPServer("webos", request_state_security=request_state_security())  # needed for the prompt


@mcp.tool(title="Check status", annotations=READ_ONLY)
@tier("read")
async def webos_get_status() -> str: ...


@mcp.tool(title="Set power", annotations=ACT_IDEMPOTENT)
@tier(lambda args: "power-off" if args["state"] == "off" else "everyday")
async def webos_set_power(state: Literal["on", "off"]) -> str: ...


@mcp.tool(title="Update firmware", annotations=DESTRUCTIVE)
@tier("costly", confirm="Update the TV's firmware?")  # costly needs a confirm sentence
async def webos_update(...) -> str: ...
```

- `@tier` goes directly under `@mcp.tool(...)`.
- Pass `request_state_security=request_state_security()` to the `MCPServer`. It makes the sealed answer expire in
  120 seconds and ties it to the person who gave it, so another person's request can't reuse it. Without it
  the SDK's default applies (600 seconds, not tied to anyone), which `remote.py` can't repair.
- `@tier` adds one hidden keyword-only parameter, `tier_authorized` (`AUTHORIZED_PARAM`), which the SDK fills and
  leaves out of the tool's input schema. It can't start with an underscore, because the SDK rejects those.
- Calling a tiered tool directly from Python is refused, since nothing filled that parameter. A test that means
  to skip the check calls the original: `fn.__wrapped__`.
- `test_every_tool_has_a_tier` (synced into every sibling, skipped here) fails the sibling's tests for each tool
  without a tier, naming it, and `test_costly_tools_have_confirm_text` for a blank confirm sentence. Both find
  the server as the module-level `<package>.server.mcp`; a sibling that builds its server in a function gets a
  skip and checks tiers in its own tests with the same helpers (`assert_every_tool_has_a_tier`).

## Health and the running commit

Each server answers two health routes, both before the Access check and with the same body:

- `GET /healthz`: for a monitor on the same machine or LAN.
- `GET <MCP_HTTP_PATH>/healthz` (`/webos/mcp/healthz`, `/mcp/backlog/healthz`): the one reachable from
  outside, since the tunnel routes only the MCP path to each server. It is the MCP path with `/healthz`
  appended (a trailing slash on the path is dropped); there is no setting for it.

200 `{"status": "ok", ...}` when healthy, 503 `{"status": "degraded", ...}` when the server's `health_check`
says so (a device that's off, say). Both, in either case, add the code the server runs:

```json
{"status": "ok", "commit": "0123456789abcdef0123456789abcdef01234567", "repo": "SDNick484/mcp-server-webos"}
```

That lets a dashboard (Workbench reads `https://mcp.example.com/<path>/healthz`) show when a deployed server is
behind `main`. The fields come from a `COMMIT` file: line 1 the SHA (`^[0-9a-f]{7,40}$`; install writes all 40),
line 2 optionally the repo (`^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`).

- **Where:** `Path(sys.prefix).parent / "COMMIT"`, which is `$PREFIX/COMMIT` for a venv at `$PREFIX/venv`.
  `MCP_COMMIT_FILE` points elsewhere.
- **When:** read on every health request, so restamping it shows without a restart.
- **If it's wrong:** a missing, unreadable, oversized (over 512 bytes) or malformed file, or line, leaves that
  field out. It never fails the check or changes the status code.

### Stamping it from a sibling's `install.sh`

`deploy/alpine/commit.sh` (module `deploy`) works out the commit **before** pip, so the stamp and the install are
the same commit, and writes `COMMIT` only **after** pip succeeds:

| `SOURCE` | Installs | Stamps |
| --- | --- | --- |
| `git+https://github.com/<REPO>` (the default) | `git+https://github.com/<REPO>@<sha>`, `<sha>` from `git ls-remote` of `main` | `<sha>` and `<REPO>` |
| a checkout directory | the directory | its `git rev-parse HEAD`, if it is the top of a git checkout |
| any other pip spec | the spec | nothing (any old `COMMIT` is removed) |

If `ls-remote` fails, the install stops rather than install an unpinned copy. In the sibling's install.sh, add
`git` to its `apk add`, then:

```sh
. "$HERE/commit.sh"
REPO=SDNick484/mcp-server-webos
pinned=$(commit_source "$SRC" "$REPO")
SRC=$(printf '%s\n' "$pinned" | sed -n 1p)
COMMIT=$(printf '%s\n' "$pinned" | sed -n 2p)

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
"$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$SRC"
write_commit "$PREFIX" "$COMMIT" "$REPO"   # set -e: never reached if pip failed
```

Then check it: `curl -s https://mcp.example.com/<path>/healthz`.

Not covered: matinee keeps its own `mcp_remote.py`, which isn't synced from here; it needs the same change by hand.

## Routing eval

`python -m evals.routing_eval` measures whether a model sends a home theater request to the right
MCP server and tool, and whether instructions planted in device strings (a video title, a game
name) make it change state. It is not a synced module: nothing here goes to the siblings. The
design is in `docs/superpowers/specs/2026-11-17-routing-eval-design.md`.

```sh
pip install -e ".[eval]"            # the anthropic SDK and tomli-w, kept out of the dev extra
export ANTHROPIC_API_KEY=...

# Refresh the committed tool snapshots from the sibling checkouts next to this repo
# (each needs its .venv; --only harmony,webos narrows it; tools without annotations are warned about):
python -m evals.routing_eval snapshot

# Run a case directory against one or more models (--model has no default). The baseline variant
# always runs; each --skill adds a variant whose system prompt includes that file:
python -m evals.routing_eval run --cases evals/cases/example-home --model <model-id> [--model <other-id>] \
    [--skill path/to/skill.md] [--trials 5] [--with-notes] [--case 'switch-*']

# Compare with an earlier run (its results JSON); exit 1 if a case or a section's pooled pass rate
# dropped by more than --tolerance (default 0.2, one trial in five):
python -m evals.routing_eval run --cases evals/cases/example-home --model <model-id> \
    --baseline results/20261120T120000Z.json

# Start a private home from Harmony: one `mcp-server-harmony doctor --dump` file per hub. It writes the
# notes and the two Harmony reads; add the other servers and your [[case]] entries by hand:
python -m evals.routing_eval skeleton --harmony-dump hub-1.json [hub-2.json] --out ~/private-cases/my-home.toml
```

`skeleton` refuses an `--out` inside this repository (the file names your real devices, so it belongs
in a private repository or a local directory) and an existing file unless you pass `--force`.

The markdown report goes to stdout; the JSON (every trial with its verdict) goes to
`results/<timestamp>.json` and its path to stderr. Exit status: `0` ok, `1` a baseline
regression, `2` an error (an invalid case file, the `[eval]` extra or `ANTHROPIC_API_KEY`
missing, an unreadable baseline, or any trial that errored, such as an API failure that survived
the SDK's retries; the other trials still finish and the results are still written).

`evals/cases/example-home/` is a fictional home with generic names (`hub-1`, `tv-1`), the model
for your own. Real cases name real rooms and devices, so they live outside this repo (a private
repository or a local directory; `--cases` takes any path), and `results/` is gitignored for the
same reason: its transcripts carry whatever the cases do. The tests never touch the network: they
drive the same code with a scripted client.

## Supply chain security

1. **Immutable Commit Pinning:** Sibling repos pin `mcp-server-shared` by full 40-character commit SHA in `shared.lock`. This guarantees byte-for-byte reproducibility and prevents supply chain poisoning via mutable git branch or tag references.
2. **Path Containment:** The sync script verifies all file read and write targets resolve strictly within the repository root (`target.resolve().is_relative_to(root.resolve())`), preventing symlink-based traversal attacks.
3. **Self-Update Integrity Check:** When `shared.toml` format increments, the sync script validates that the incoming `scripts/sync_shared.py` parses as valid Python syntax (`ast.parse`) before writing it to disk.
4. **Automated Vulnerability Scanning:** Dependency trees are checked for known CVEs via `pip-audit` in CI.

## Workflows

**Pre-commit:** run `pre-commit install` once per clone (it comes with `pip install -e ".[dev]"`); each commit then renders the test templates and lints them as the siblings will, which plain `ruff check .` can't (the rendered copies are gitignored here).

### Upgrading from format 1

If a sibling has a format 1 lockfile:

1. Update its `shared.toml` to `format = 2`.
2. Run `python scripts/sync_shared.py --update`.
3. The script will write a format 2 lockfile and copy the new sync script into the sibling.

### Adding a new shared module to a sibling

1. Add the module name to `modules` in the sibling's `shared.toml`.
2. Run `python scripts/sync_shared.py --update`.
3. Commit the new module, its rendered tests, and the updated `shared.lock`.

### Testing a change to `mcp-server-shared` across all siblings

```sh
# In mcp-server-shared:
# 1. Make the change in src/mcp_shared/ and templates/tests/
# 2. Render and test locally:
python scripts/sync_shared.py --source . --package mcp_shared
pytest && ruff check . && ruff format --check . && mypy
ruff check --no-respect-gitignore tests && ruff format --check --no-respect-gitignore tests

# 3. Test across all local siblings:
python scripts/sync_all_siblings.py update --source . --test
```

If everything passes, merge the change into `mcp-server-shared` main, then propose it to every
sibling with `python scripts/sync_all_siblings.py update --pr`.

## Why CI runs weekly on a schedule

Every sibling repo's `ci.yml` has a weekly cron trigger:

```yaml
schedule:
  - cron: "0 9 * * 1"   # 09:00 UTC every Monday
```

On that run the shared-drift job adds `python scripts/sync_shared.py --check --latest`, which
checks whether `shared.lock` is behind `mcp-server-shared`'s `main` branch.

### The drift job is a reusable workflow, not a synced file

`.github/workflows/shared-drift.yml` here is the one definition of the shared-drift job
(`on: workflow_call`). A sibling's `ci.yml` calls it, pinned like an action:

```yaml
jobs:
  shared-drift:
    uses: SDNick484/mcp-server-shared/.github/workflows/shared-drift.yml@<40-char SHA>  # v0.1.0
```

It checks out the caller and runs the caller's own `sync_shared.py --check`, plus `--check --latest`
on `schedule`/`workflow_dispatch` (or with `latest: true`). Read-only token, SHA-pinned actions,
`timeout-minutes`, and a job-level `concurrency` group (a called workflow sharing the caller's
workflow-level group deadlocks, since `github.workflow` there is the caller's name). The group is
per event and only PR runs cancel, so a push to `main` can't cancel the weekly `--latest` check.

The alternative was to make the workflow a synced file, like the modules. Why not:

| | Reusable workflow (chosen) | Synced template |
|---|---|---|
| Copies | none; one SHA per sibling in `ci.yml` | one file per sibling, checked by `--check` |
| CI-time dependency on this repo | yes, but `--check` already fetches from here at CI time | none beyond what `--check` already has |
| Sibling-specific triggers | stay in the sibling's `ci.yml` | one file for all: same schedule everywhere, or templating |
| Pushing a sync | an ordinary commit | touches `.github/workflows/`: needs a token with `workflow` scope |
| Updating | Dependabot bumps the SHA (and `# vX` comment) once `main` is tagged | the next `sync_all_siblings.py update` |
| Pins to keep straight | two: `shared.lock` and the `uses:` SHA | one |

The two-pins cost is small: the workflow changes rarely, and whichever SHA it is pinned to, it runs
the sibling's own sync script against the sibling's own lock.

### Why not fail normal PRs when the lock is behind?

A normal PR in a sibling checks that the code matches its lock (`python scripts/sync_shared.py`),
not that the lock is on the newest commit. That's intentional:

- A PR about an Onkyo volume bug shouldn't fail just because someone pushed a new shared module
  version an hour ago.
- Updating the shared lock is its own change, with its own review and its own test run.
- It decouples sibling development from `mcp-server-shared` development.

The scheduled weekly run is what catches a sibling falling behind: it opens an issue or alerts
maintainers that an update is available, without breaking unrelated PRs.

### Why a schedule instead of a repository_dispatch event?

When `mcp-server-shared` pushes a new commit to `main`, it *could* dispatch a webhook to all
seven sibling repos to trigger an update PR. We don't do that because:

- It requires each sibling to grant write access to a shared token (or a personal access token
  with repo scope across the whole org).
- Seven automated PRs landing at once creates notification spam and merge traffic for changes
  that might be irrelevant to most siblings (e.g., an Onkyo-specific change to `registry.py`).
- The scheduled run lets updates batch naturally. A maintainer sees the weekly notification,
  runs `sync_all_siblings.py update --pr` locally, which checks every sibling as its CI would
  and opens a PR for each one that passes, in one sitting.

The tradeoff is lag: a sibling might not pick up a fix for up to seven days unless someone
syncs it manually. For the kinds of changes this repo carries (rare, stable, additive), that's
the right tradeoff.

### Scheduled runs pin the lock; they don't auto-update it

The scheduled run does not run `--update` and push. It only runs `--check-latest` and fails
if the lock is stale. A human still runs the update, runs the sibling's test suite, and merges.

This avoids the `dependabot`-style failure mode where an automated update silently breaks a
sibling's tests and sits red in the PR list.

### Why test dependencies aren't pinned to exact hashes

Each sibling's `pyproject.toml` uses version ranges (`pytest>=8.0`, etc.) rather than exact pins.
The shared lock pins the *shared code*, not the environment. This means:

- The lock is reproducible: any machine syncing the same commit hash gets byte-identical files.
- The weekly scheduled run also catches external dependency rot: if a new minor release of
  `anyio` or `httpx` breaks something, the scheduled run catches it before a user does.

## Invariants (things that must never break)

- **Zero dependencies on `mcp_shared`:** Sibling repos must never import `mcp_shared`. They
  import from their own package (`from <pkg>.remote import ...`).
- **No imports between shared modules:** A shared module must never import another shared
  module. If `remote.py` imported `logsafe.py`, a sibling couldn't adopt `remote` without
  also adopting `logsafe`. Every shared module must stand alone.
- **Byte identity:** After a sync, the copy in the sibling and the source in this repo must
  be byte-for-byte identical (except for test templates, which replace `__PKG__`).
- **No network in test suites:** Sibling test suites test against simulated devices and fake
  transports. The sync script's own tests use a mock GitHub class. Nothing in `pytest` ever
  hits the real network.
