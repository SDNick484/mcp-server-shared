# Health route under the MCP path, with the running commit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each sibling server reports the commit it runs at `<MCP_HTTP_PATH>/healthz` (reachable through the tunnel), so Workbench can show when a deployed server is behind `main`.

**Architecture:** `AccessMiddleware` in `remote.py` answers both `/healthz` and `<path>/healthz` from one code path, and adds `commit`/`repo` read from a `COMMIT` file on every request. A new synced POSIX shell library, `deploy/alpine/commit.sh`, gives each sibling's own `install.sh` two functions: resolve the source to a pinned commit before pip, and stamp `$PREFIX/COMMIT` after pip succeeds.

**Tech Stack:** Python 3 / Starlette ASGI (MCP SDK), pytest + httpx2, POSIX `sh` (busybox ash on Alpine), git.

**Spec:** the design agreed in chat on 2026-11-21 (the request plus the "synced `commit.sh`, sourced by each install.sh" decision). Recorded here since there is no separate spec file.

## Global Constraints

- Public repo: no private domain, hostnames, IPs or names. Placeholders only (`mcp.example.com`, `example.com`).
- Commit SHA regex `^[0-9a-f]{7,40}$`; the stamp always writes 40. Repo regex `^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`; repo value is `SDNick484/<name>`.
- COMMIT file: line 1 SHA, optional line 2 repo. Default location `Path(sys.prefix).parent / "COMMIT"`; `MCP_COMMIT_FILE` overrides. Read on every health request.
- COMMIT problems never change the status code or fail the check; the bad field is just left out.
- Health body stays small (Workbench reads ≤ 4 KB).
- No new setting for the health route: it is `MCP_HTTP_PATH` + `/healthz`. Keep the bare `/healthz`.
- `remote.py` must not import from any other shared module.
- Don't touch sibling repos or matinee.
- Commit, push and open the PR only after the user says so.

## Review Focus

- A sibling's `health_check` returning its own `commit`/`repo` key: the COMMIT file's value wins (test in Task 1).
- A huge or binary COMMIT file (wrong path in `MCP_COMMIT_FILE`): read is capped, body stays small, no crash (test in Task 1).
- `MCP_HTTP_PATH` with a trailing slash (`/mcp/backlog/`): route is `/mcp/backlog/healthz`, not `//healthz` (test in Task 2).
- `POST <path>/healthz` must not be answered as health; it goes on to the Access check like the bare route (test in Task 2).
- A failed `git ls-remote` must stop the install, not install an unpinned copy (test in Task 3).

---

### Task 1: COMMIT fields in the health body

**Files:**
- Modify: `src/mcp_shared/remote.py` (constants near `HEALTH_PATH`; new `running_commit()` before `AccessMiddleware`; health branch of `AccessMiddleware.__call__`)
- Test: `templates/tests/test_remote.py`

**Interfaces:**
- Produces: `COMMIT_FILE_ENV = "MCP_COMMIT_FILE"`; `running_commit(env: Mapping[str, str] | None = None) -> dict[str, str]` returning `{}` or `{"commit": sha}` or `{"commit": sha, "repo": repo}`, or `{"repo": repo}` if only line 2 is valid. A private `_health(send)` helper inside the middleware (or method) that Task 2 calls for both routes.

- [ ] **Step 1: Write failing tests** in `templates/tests/test_remote.py`:
  - autouse fixture `no_commit_file(monkeypatch, tmp_path)` setting `MCP_COMMIT_FILE` to `tmp_path / "absent"` so existing exact-body assertions stay stable on any machine.
  - `commit_file(tmp_path, monkeypatch, text)` helper that writes the file and points the env var at it.
  - `test_health_reports_commit_and_repo`: file `"<40 hex>\nSDNick484/mcp-server-demo\n"` → `GET /healthz` body `{"status": "ok", "commit": SHA, "repo": "SDNick484/mcp-server-demo"}`, 200.
  - `test_degraded_health_reports_commit`: parametrize over health_check returning `False`, raising, and `{"error": "x"}` → 503 and body includes `commit` and `repo`.
  - `test_bad_commit_file_leaves_fields_out` parametrized: missing file → `{}` extra; empty file; `"not-a-sha\n"`; `"ABCDEF0\n"` (uppercase rejected); SHA only, one line → only `commit`; SHA + `"bad repo!"` → only `commit`; `"zz\nSDNick484/x\n"` → only `repo`; a directory at the path; 1 MB of `"a"` → nothing. Each asserts status 200 unchanged.
  - `test_commit_file_is_read_on_every_request`: write SHA A, GET; rewrite to SHA B, GET; bodies show A then B.
  - `test_commit_file_wins_over_health_check_keys`: health_check returns `{"commit": "x"}` → body `commit` is the file's SHA.
  - `test_running_commit_defaults_beside_the_venv(monkeypatch, tmp_path)`: `monkeypatch.setattr(remote.sys, "prefix", str(tmp_path / "venv"))`, write `tmp_path / "COMMIT"`, call `remote.running_commit(env={})` → fields from it.
- [ ] **Step 2:** Render and run: `python scripts/sync_shared.py --source . --package mcp_shared && pytest tests/test_remote.py -q -k "commit"` — expect failures (`running_commit` missing / fields absent).
- [ ] **Step 3: Implement.** `running_commit` reads at most 512 bytes with `open(path, "rb").read(513)`; more than 512 → `{}`. Decode ASCII (errors → `{}`), `splitlines()`, `fullmatch` each regex (`_COMMIT_RE`, `_REPO_RE`). Any `OSError` → `{}`. Import `sys` and `pathlib.Path`. In the middleware, merge as `{**body, **running_commit()}` into every health response (200 and all three 503 variants), keeping `"status"` correct.
- [ ] **Step 4:** Run `pytest tests/test_remote.py -q` — all pass.
- [ ] **Step 5:** Stage only; suggested message `remote: report the running commit in the health body` (commits wait for the user's go-ahead).

### Task 2: Health route under the MCP path

**Files:**
- Modify: `src/mcp_shared/remote.py` (`AccessMiddleware.__init__` gains `mcp_path: str | None = None`; `build_app` passes `cfg.path`; module docstring mentions the route)
- Test: `templates/tests/test_remote.py`

**Interfaces:**
- Consumes: Task 1's health helper.
- Produces: `health_paths(mcp_path: str | None) -> frozenset[str]` returning `{"/healthz"}` plus `mcp_path.rstrip("/") + "/healthz"` when given.

- [ ] **Step 1: Write failing tests:**
  - `test_health_under_the_mcp_path_needs_no_assertion`: `CFG` (path `/thing/mcp`) → `GET /thing/mcp/healthz` is 200 `{"status": "ok"}` without an assertion; bare `/healthz` still 200.
  - `test_health_under_the_mcp_path_reports_commit_healthy_and_degraded`: both routes, with commit file, healthy check and a `False` check → 200/503, `commit`+`repo` present.
  - `test_health_under_a_multi_segment_path`: config path `/mcp/backlog` → `GET /mcp/backlog/healthz` 200.
  - `test_health_paths_strip_a_trailing_slash`: `remote.health_paths("/mcp/backlog/") == {"/healthz", "/mcp/backlog/healthz"}`.
  - `test_post_to_the_mcp_health_path_needs_an_assertion`: `POST /thing/mcp/healthz` → 403.
  - `test_mcp_health_path_refuses_forwarded_when_local_only`: `LOCAL` config, `x-forwarded-for` → 403 (same as bare route).
- [ ] **Step 2:** Render, `pytest tests/test_remote.py -q -k health` — new tests fail (403 on `/thing/mcp/healthz`).
- [ ] **Step 3: Implement** `health_paths`; store `self.health_paths = health_paths(mcp_path)`; test `scope["path"] in self.health_paths`.
- [ ] **Step 4:** `pytest -q` — all pass.
- [ ] **Step 5:** Stage only; suggested message `remote: answer health under the MCP path too` (commits wait for the user's go-ahead).

### Task 3: `deploy/alpine/commit.sh`, synced

**Files:**
- Create: `deploy/alpine/commit.sh`
- Modify: `shared.toml` (new `[[file]]`, `source = "deploy/alpine/commit.sh"`, `module = "deploy"`, `target = "deploy/alpine/commit.sh"`)
- Test: `tests/test_commit_sh.py` (this repo only; runs the script with `sh`)

**Interfaces:**
- Produces (POSIX sh functions, sourced; no side effects on source):
  - `commit_source SRC REPO` → prints two lines: the spec to pip-install, then the 40-hex SHA or an empty line. Default source (`git+https://github.com/$REPO`, with or without `.git`): `git ls-remote "https://github.com/$REPO" refs/heads/main`, SHA validated, spec `git+https://github.com/$REPO@<sha>`; failure or no valid SHA → message on stderr, return 1. Directory: `git -c safe.directory="$SRC" -C "$SRC" rev-parse HEAD` (failure → empty SHA, spec unchanged). Anything else: spec unchanged, empty SHA.
  - `write_commit PREFIX SHA REPO` → if SHA matches `^[0-9a-f]{40}$`: write `SHA\nREPO\n` to `$PREFIX/COMMIT.tmp`, chmod 0644, `mv -f` to `$PREFIX/COMMIT` (repo line only if it matches the repo regex); otherwise `rm -f "$PREFIX/COMMIT"`.
  - Sibling usage (goes in the README, Task 4):
    ```sh
    . "$HERE/commit.sh"
    REPO=SDNick484/mcp-server-webos
    pinned=$(commit_source "$SRC" "$REPO")
    SRC=$(printf '%s\n' "$pinned" | sed -n 1p); COMMIT=$(printf '%s\n' "$pinned" | sed -n 2p)
    ...pip install "$SRC" ...
    write_commit "$PREFIX" "$COMMIT" "$REPO"
    ```

- [ ] **Step 1: Write failing tests** in `tests/test_commit_sh.py`, each running `sh -c '. deploy/alpine/commit.sh; …'` with a fake `git` (a shell script in `tmp_path/bin` prepended to `PATH` that logs its args and prints a canned answer):
  - `test_default_source_pins_main`: fake ls-remote prints `<sha>\trefs/heads/main` → output `git+https://github.com/SDNick484/mcp-server-demo@<sha>\n<sha>`; fake git was called with `ls-remote https://github.com/SDNick484/mcp-server-demo refs/heads/main`.
  - `test_default_source_with_dot_git_suffix_pins_main`.
  - `test_ls_remote_failure_stops`: fake git exits 128 → exit status 1, stderr mentions `ls-remote`.
  - `test_ls_remote_garbage_stops`: fake prints `nope` → exit 1.
  - `test_checkout_uses_rev_parse`: SRC is a tmp dir → output `<dir>\n<sha>`, git called with `-C <dir> rev-parse HEAD`.
  - `test_checkout_without_git_gives_no_commit`: fake git exits 128 → output `<dir>\n` and exit 0.
  - `test_other_spec_gives_no_commit`: `mcp-server-demo==1.0` → output unchanged, empty SHA, git never called.
  - `test_write_commit_stamps_sha_and_repo`: file content `"<sha>\nSDNick484/mcp-server-demo\n"`, mode 0644, no `COMMIT.tmp` left.
  - `test_write_commit_without_sha_removes_a_stale_stamp`: pre-existing COMMIT, empty SHA → file gone.
  - `test_write_commit_rejects_a_short_sha`: 7-hex SHA → no file.
  - `test_the_manifest_syncs_commit_sh`: parse real `shared.toml` with `sync.parse_manifest` → an entry with target `deploy/alpine/commit.sh` and module `deploy`.
  - `test_stamp_round_trips_through_running_commit`: `write_commit` output read by `mcp_shared.remote.running_commit(env={"MCP_COMMIT_FILE": …})` returns both fields (ties the writer to the reader).
- [ ] **Step 2:** `pytest tests/test_commit_sh.py -q` — fail (no script).
- [ ] **Step 3: Implement** `deploy/alpine/commit.sh` (header comment: what it is, that it's synced from mcp-server-shared and must not be edited in a sibling, the usage block above). Validation via `case`/`grep -Eq`; `set -eu`-safe (callers use `set -eu`).
- [ ] **Step 4:** `pytest -q`, `sh -n deploy/alpine/commit.sh`, and `shellcheck -s sh deploy/alpine/commit.sh` if available.
- [ ] **Step 5:** Stage only; suggested message `deploy: a shared commit.sh to pin and stamp the installed commit` (commits wait for the user's go-ahead).

### Task 4: Docs and full verification

**Files:**
- Modify: `README.md` (module table: `remote` row mentions `<path>/healthz` and the commit; new `deploy` row; a short "Health and the running commit" section: both routes, body fields, COMMIT format/location/`MCP_COMMIT_FILE`, read per request, sibling install.sh snippet; follow-up note that matinee's `mcp_remote.py` is not synced and needs the same change by hand)
- Modify: `CLAUDE.md` layout list (add `deploy/alpine/commit.sh`)
- Modify: `src/mcp_shared/remote.py` module docstring if Task 2 didn't cover the COMMIT file.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** Full check: `python scripts/sync_shared.py --source . --package mcp_shared && pytest -q && ruff check . && ruff format --check . && mypy && ruff check --no-respect-gitignore tests && ruff format --check --no-respect-gitignore tests` — all clean.
- [ ] **Step 3:** `git grep -nI` for the private domain → nothing.
- [ ] **Step 4:** Stage only; suggested message `docs: health under the MCP path and the COMMIT file`. Ask the user before committing, pushing and opening the PR.
