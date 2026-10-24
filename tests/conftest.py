"""Fixtures for mcp-server-shared's own test run.

The shared tests (tests/test_remote.py, test_logsafe.py, test_tasks.py, ...) aren't
in git here: they're rendered from templates/tests/ for this repo's package by
`python scripts/sync_shared.py --source . --package mcp_shared`, exactly as each
sibling gets them. They rely on every sibling's conftest providing anyio_backend,
so this one does too.
"""

from __future__ import annotations

import pytest


# Async tests use anyio's plugin (pytest.mark.anyio), not pytest-asyncio, as in the siblings.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
