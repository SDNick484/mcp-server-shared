# CLAUDE.md

## What this is

`mcp-server-shared`: the single source of truth for the code the sibling MCP servers
(mcp-server-onkyo, -shieldtv, -harmony, -sofabaton, -webos, -misterfpga, -batocera, -pixelcade)
each carry a copy of, plus `scripts/sync_shared.py`, which copies it into them and checks for
drift. README.md explains the workflow; read it before changing how syncing works.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them. He's a
software/systems engineer: be technical and concrete, with specific examples.

## Layout

- `src/mcp_shared/remote.py`, `logsafe.py`, `tasks.py`, `ratelimit.py`, `registry.py`: the shared modules, copied byte for byte
  into each sibling's package. They must not import anything from their own package
  (`mcp_shared` or a sibling's), since the bytes have to work in all of them.
- `templates/tests/`: the shared tests, with `__PKG__` for the package name. Excluded from ruff
  (isort can't place `__PKG__`); linted as rendered.
- `tests/`: `conftest.py` and `test_sync_shared.py` (the script, against a fake GitHub). The
  shared tests are rendered into `tests/` by `--source . --package mcp_shared` and are
  git-ignored.
- `shared.toml`: the manifest, which files go where, grouped by `module` (a module's code and its
  test share a name). Add a file here, not in the script. A sibling's `shared.lock` can
  `exclude` modules by that name. Batocera and Pixelcade exclude `remote` because they're
  stdio-only. MiSTer is LAN-only but keeps it: its `serve --http` uses it. Exclude `remote`
  only where nothing imports it.
- `scripts/sync_shared.py`: standard library only, Python 3.11+. It's shared too (it syncs itself).

## Rules for changes

- **A change here reaches eight repos.** Keep shared modules free of anything one sibling needs and
  the others don't; that belongs in the sibling (e.g. webos's `redact.py` on top of `logsafe.py`).
- **Keep the script stdlib-only** and runnable on 3.11: CI runs it before `pip install`.
- A `shared.toml` change the current script can't read means bumping `format` here and `FORMAT`
  in the script together (the old script then fetches the new one; see `wanted_files`).
- The shared files' headers say where to change them; don't add "copied from" notes.
- After merging to `main`: `python scripts/sync_shared.py --update` in each sibling, one PR each.

## Commands

```sh
pip install -e ".[dev]"
python scripts/sync_shared.py --source . --package mcp_shared   # render the shared tests
pytest && ruff check . && ruff format --check . && mypy
```
