# CLAUDE.md

## What this is

`mcp-server-shared`: the single source of truth for the code the sibling MCP servers
(`mcp-server-batocera`, `-harmony`, `-misterfpga`, `-onkyo`, `-pixelcade`, `-shieldtv`,
`-sofabaton`, `-steam-backlog`, `-webos`) each carry a copy of.

See `README.md` for the full layout, motivation, and the sync workflow.

When answering questions about the architecture or design choices, speak as a seasoned
software/systems engineer: be technical and concrete, with specific examples.

## Layout

- `src/mcp_shared/remote.py`, `logsafe.py`, `tasks.py`, `ratelimit.py`, `registry.py`, `lookup.py`, `tools.py`: the shared modules, copied byte for byte
  into each sibling's package. They must not import anything from their own package
  (`mcp_shared` or a sibling's), since the bytes have to work in all of them.
- `templates/tests/`: the shared tests, with `__PKG__` for the package name. Excluded from ruff
  and mypy since `__PKG__` isn't a valid package.
- `tests/test_sync_shared.py`: tests for the sync script itself.
- `shared.toml`: the manifest listing the shared files.
- `scripts/sync_shared.py`: copies the shared files to/from siblings or GitHub.
- `scripts/sync_all_siblings.py` (this repo only, not synced): runs that across every sibling checkout;
  `clone`, `status`, and `update --pr` (sync, run each sibling's CI checks, open a PR per passing sibling).

## Commands

- `pip install -e ".[dev]"`: install dependencies and dev tools
- `python scripts/sync_shared.py --source . --package mcp_shared`: render test templates locally so pytest can run them
- `pytest`: run all tests
- `ruff check .`: lint
- `ruff format --check .`: check formatting
- `mypy`: type-check
- `pre-commit install`: once per clone; commits then render and lint the templates as a sibling would

## Workflows

### Changing a shared module

1. Edit the module in `src/mcp_shared/`.
2. Edit its tests in `templates/tests/` (remember: use `__PKG__`, not `mcp_shared`).
3. Re-render tests locally: `python scripts/sync_shared.py --source . --package mcp_shared`.
4. Run tests and linters: `pytest && ruff check . && ruff format --check . && mypy`, plus
   `ruff check --no-respect-gitignore tests && ruff format --check --no-respect-gitignore tests`:
   the rendered tests are gitignored here, so plain `ruff check .` skips them, but every sibling lints them.
5. Commit and push to `main` (or a PR branch).
6. Sync the siblings:
   - For a local sibling checkout: `python scripts/sync_shared.py --update` in the sibling repo.
   - For all local siblings: `python scripts/sync_all_siblings.py update --pr` from this repo (syncs,
     runs each sibling's CI checks, opens a PR per sibling that passes; `clone` first if any are missing).

### Adding a new shared module

1. Create `src/mcp_shared/<name>.py` (no imports from `mcp_shared`!).
2. Create `templates/tests/test_<name>.py` (imports use `__PKG__`).
3. Add both to `shared.toml` under a new `module = "<name>"` section.
4. Add `/tests/test_<name>.py` to `.gitignore` so the rendered copy isn't committed.
5. Render, test, lint, and commit as above.

## Conventions

- The repo is public and generic. No personal names, hostnames, IP addresses, room names or
  household references anywhere: code, docstrings, tests, docs, commit messages. Use placeholders:
  `mcp.example.com`, `example.cloudflareaccess.com`, `user@example.com`, device keys like `tv-1`,
  and the documentation address ranges (`192.0.2.0/24`, `2001:db8::/32`) rather than `192.168.x.x`.

## Gotchas

- **Do not edit rendered tests in `tests/` directly.** Edit `templates/tests/` instead.
- **Do not import across shared modules.** Each module must be self-contained so a sibling can
  adopt only the ones it needs.
- **Run the sync script before testing.** Tests won't see your template changes until re-rendered.
