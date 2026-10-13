# mcp-server-shared

The single source of truth for code the sibling MCP servers each carry a copy of:
[mcp-server-onkyo](https://github.com/SDNick484/mcp-server-onkyo),
[-shieldtv](https://github.com/SDNick484/mcp-server-shieldtv),
[-harmony](https://github.com/SDNick484/mcp-server-harmony),
[-sofabaton](https://github.com/SDNick484/mcp-server-sofabaton) and
[-webos](https://github.com/SDNick484/mcp-server-webos).

| Here | In each sibling | What it is |
|---|---|---|
| `src/mcp_shared/remote.py` | `src/<pkg>/remote.py` | Streamable HTTP behind Cloudflare Access |
| `src/mcp_shared/logsafe.py` | `src/<pkg>/logsafe.py` | log redaction (addresses, MACs, secrets) |
| `src/mcp_shared/tasks.py` | `src/<pkg>/tasks.py` | `stop_task`: stopping a task that drops a cancel |
| `templates/tests/test_*.py` | `tests/test_*.py` | their tests, with `__PKG__` for the package name |
| `scripts/sync_shared.py` | `scripts/sync_shared.py` | the script that copies all of the above |

[`shared.toml`](shared.toml) is that table in machine-readable form, and the script reads it,
so sharing another file is an edit there and nothing else.

## Why this exists

The siblings used to keep these files in step by hand: "change it in one, copy it to the
others". That drifted silently. Harmony and Sofabaton fell a revision behind without anyone
editing them, because nothing noticed a copy that was merely *old*. Now each sibling pins the
commit of this repo its copies come from, and CI checks both kinds of drift:

- **A copy edited in a sibling** is caught on that sibling's push or PR (`--check`).
- **A sibling left behind** by a change here goes red on a weekly schedule (`--check --latest`).

## How it works

Each sibling has a `shared.lock`:

```toml
repo = "SDNick484/mcp-server-shared"
commit = "<40-character SHA of this repo>"
package = "webos_mcp"
```

`python scripts/sync_shared.py` reads `shared.toml` *at that commit*, fetches each listed file
from `raw.githubusercontent.com` at that commit, puts the package name into each target path
(and, for the test templates, the file's contents), and writes whatever differs.

| Command (in a sibling) | Does |
|---|---|
| `python scripts/sync_shared.py` | write the files as of the locked commit |
| `python scripts/sync_shared.py --update` | move the lock to this repo's `main`, then write |
| `python scripts/sync_shared.py --check` | write nothing; exit 1 if any copy differs from the locked commit |
| `python scripts/sync_shared.py --check --latest` | the same, against `main` |
| `... --source ../mcp-server-shared` | read a local checkout instead of GitHub |

Choices worth knowing about, and why:

- **Fetched by SHA, never by branch.** A SHA is one immutable snapshot, so every file in a sync
  comes from the same commit, and the CDN's few-minute caching of branch URLs can't serve a
  stale file. `--update` and `--latest` first ask the GitHub API what `main` points at.
- **Fetch everything, then write; the lock last.** A network failure halfway through leaves the
  sibling exactly as it was.
- **Standard library only, Python 3.11+.** It runs before `pip install`, so the CI job needs
  nothing but a Python.
- **`--check --latest` compares files, not SHAs.** If `main` moved for a README edit only, no
  shared file changed and the sibling isn't behind in any way that matters. It passes and says
  the lock lags.
- **The script syncs itself.** It's in the manifest like everything else. If a future
  `shared.toml` needs a newer script (its `format` goes up), the old script fetches just the new
  version of itself and asks you to run the command again.
- **Exit codes:** 0 in sync, 1 drift, 2 anything else (network, a bad lock, bad arguments).

## Workflows

### Changing a shared file

1. Change it **here**, never in a sibling's copy (the sibling's CI would reject that anyway).
   For a test, edit `templates/tests/`.
2. Optional: try it in a sibling before pushing, from a local checkout:
   `python scripts/sync_shared.py --source ../mcp-server-shared`, then run that sibling's tests.
   `--check` will (rightly) complain until step 4.
3. PR here; CI renders the tests for `mcp_shared` and runs pytest, ruff and strict mypy.
   Merge to `main`.
4. In each sibling: `python scripts/sync_shared.py --update`, run its tests, and commit the
   changed files together with `shared.lock`.

Until step 4 reaches a sibling, its scheduled run goes red. That's the reminder.

### Sharing another file

Add a `[[file]]` to `shared.toml` (`render = true` if it needs `__PKG__` filled in). For a new
test template, also add its rendered path to `.gitignore` here. Then do steps 3 and 4 above.
Removing an entry doesn't delete the siblings' copies; delete those by hand.

### Adding a sibling

```sh
mkdir -p scripts
curl -fsSLo scripts/sync_shared.py https://raw.githubusercontent.com/SDNick484/mcp-server-shared/main/scripts/sync_shared.py
printf 'repo = "SDNick484/mcp-server-shared"\ncommit = "%040d"\npackage = "newthing_mcp"\n' 0 > shared.lock
python scripts/sync_shared.py --update   # replaces the placeholder commit with main's
```

Then add the `shared-drift` job below to its `.github/workflows/ci.yml`, and a conftest
fixture `anyio_backend` returning `"asyncio"` (the shared async tests rely on it).

## The `shared-drift` CI job (in each sibling)

```yaml
on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 8 * * 1"            # Mondays 8:00
      timezone: America/Los_Angeles  # so it stays 8 a.m. Pacific across daylight saving
  workflow_dispatch:               # a "Run workflow" button, e.g. to check right after a change here

jobs:
  shared-drift:
    runs-on: ubuntu-latest
    env:
      GITHUB_TOKEN: ${{ github.token }}  # for api.github.com's rate limit; raw files are fetched without it
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.11"  # the oldest supported Python, to keep the script honest
      - name: Shared files match the locked commit
        run: python scripts/sync_shared.py --check
      - name: Shared files match mcp-server-shared main
        if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
        run: python scripts/sync_shared.py --check --latest
```

**Why `--latest` only runs on the schedule:** a PR in a sibling should be judged on what it
changes. If `--latest` ran on PRs, any merge here would turn every open sibling PR red for a
reason unrelated to it. The lock makes PR results reproducible; the schedule catches lag.

The other jobs in each `ci.yml` (tests, Alpine, mosquitto) run weekly too, because a schedule
triggers the whole workflow. That's useful in itself: it catches a new release of a dependency
(within the version bounds) that breaks a sibling while nobody is working on it.

### Scheduled runs stop after 60 quiet days

GitHub disables scheduled workflows in a public repository when it has had no activity for 60
days. In these repos the schedule lives in `ci.yml`, so it's the whole CI workflow that gets
disabled, push and PR runs included, until it's turned back on. A sibling that's finished and
untouched is exactly the one that stops being watched.

To re-enable it: the repo's **Actions** tab → **CI** in the sidebar → **Enable workflow**. Or:

```sh
gh workflow enable ci.yml -R SDNick484/mcp-server-webos
gh workflow list --all -R SDNick484/mcp-server-webos   # state shows disabled_inactivity when it has happened
```

Pushes count as activity and reset the 60-day clock, so a repo you're working in won't hit
this; once a workflow has been disabled, re-enable it as above.

## Developing this repo

The shared tests aren't committed under `tests/` here; they're rendered from the templates for
this repo's own package, exactly as the siblings get them (and ignored by git):

```sh
pip install -e ".[dev]"
python scripts/sync_shared.py --source . --package mcp_shared
pytest && ruff check . && ruff format --check . && mypy
```

`tests/test_sync_shared.py` tests the script itself against a fake GitHub, so it needs no
network. It stays here and isn't shared.

Line endings: the copies are compared byte for byte, so a checkout that converts to CRLF
(`core.autocrlf` on Windows) will show every file as different.
