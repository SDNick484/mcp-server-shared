# mcp-server-shared

The single source of truth for code the sibling MCP servers each carry a copy of:

| Module | Source in this repo | Destination in sibling | What it is |
| --- | --- | --- | --- |
| `remote` | `src/mcp_shared/remote.py` | `src/<pkg>/remote.py` | HTTP behind Cloudflare Access (incl. `/healthz` readiness hook and CORS `OPTIONS` preflight) |
| `logsafe` | `src/mcp_shared/logsafe.py` | `src/<pkg>/logsafe.py` | log redaction (IPv4/IPv6 addresses, MACs, secrets incl. URL query tokens; `register_custom_pattern` for device-specific ones) |
| `tasks` | `src/mcp_shared/tasks.py` | `src/<pkg>/tasks.py` | `stop_task`, `stop_tasks` and `spawn_task`: starting background tasks and stopping ones that drop a cancel |
| `ratelimit` | `src/mcp_shared/ratelimit.py` | `src/<pkg>/ratelimit.py` | `TokenBucket` (`take`, `refund`, async `acquire`/`reserve`), `KeyedTokenBucket` and `refusal`: per-device rate limits |
| `registry` | `src/mcp_shared/registry.py` | `src/<pkg>/registry.py` | the assumption registry's record, statuses, queries and doctor summaries (`doctor_lines`, `doctor_summary`; each sibling's list stays in its `assumptions.py`) |
| `lookup` | `src/mcp_shared/lookup.py` | `src/<pkg>/lookup.py` | `norm`, `suggest`, and `unknown_with_suggestions`: normalized matching and closest candidate suggestions on lookup misses |
| `tools` | `src/mcp_shared/tools.py` | `src/<pkg>/tools.py` | standard MCP `ToolAnnotations` presets (`READ_ONLY`, `ACT`, `ACT_IDEMPOTENT`, `DESTRUCTIVE`, `OPEN_WORLD_READ`) |
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
package = "webos_mcp"                                   # what replaces __PKG__
exclude = ["registry"]                                  # optional: modules this sibling doesn't take
```

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

From this repo, `scripts/sync_all_siblings.py` can update, check, or test all siblings in one go:

```sh
# Check drift across all local sibling checkouts:
python scripts/sync_all_siblings.py check

# Update all local sibling checkouts from this repo:
python scripts/sync_all_siblings.py update --source .

# Run each sibling's test suite after syncing:
python scripts/sync_all_siblings.py update --source . --test
```

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

## Supply chain security

1. **Immutable Commit Pinning:** Sibling repos pin `mcp-server-shared` by full 40-character commit SHA in `shared.lock`. This guarantees byte-for-byte reproducibility and prevents supply chain poisoning via mutable git branch or tag references.
2. **Path Containment:** The sync script verifies all file read and write targets resolve strictly within the repository root (`target.resolve().is_relative_to(root.resolve())`), preventing symlink-based traversal attacks.
3. **Self-Update Integrity Check:** When `shared.toml` format increments, the sync script validates that the incoming `scripts/sync_shared.py` parses as valid Python syntax (`ast.parse`) before writing it to disk.
4. **Automated Vulnerability Scanning:** Dependency trees are checked for known CVEs via `pip-audit` in CI.

## Workflows

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

If everything passes, commit and push to `mcp-server-shared`, then update the siblings
to the new commit hash.

## Why CI runs weekly on a schedule

Every sibling repo's `ci.yml` has a weekly cron trigger:

```yaml
schedule:
  - cron: "0 9 * * 1"   # 09:00 UTC every Monday
```

This runs `python scripts/sync_shared.py --check-latest`, which checks whether `shared.lock`
is behind `mcp-server-shared`'s `main` branch.

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
  runs `sync_all_siblings.py update` locally, verifies all seven suites, and pushes clean PRs
  in one sitting.

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
