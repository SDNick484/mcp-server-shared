"""Background task lifecycle: cancellation, bounded wait, and exception suppression.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why this helper exists: when a server shuts down or restarts a device
connection (reconnect loops, SSE/WebSocket listeners), cancelling an asyncio
Task is only half the job. You also have to wait for the task to finish
handling the CancelledError, and if the task catches CancelledError and keeps
running, cancel it again. If a timeout expires, log what didn't stop rather than
hanging forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Strong references to running background tasks started by spawn_task,
# so they aren't garbage-collected mid-execution.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _consume_abandoned_exception(task: asyncio.Task[Any]) -> None:
    if task.done() and not task.cancelled():
        task.exception()


async def stop_task(task: asyncio.Task[Any] | None, timeout: float, what: str) -> None:
    """Cancel a background task and wait until it has finished, re-cancelling as needed, for at most `timeout`.

    If the task is already done, None, or finishes cleanly within `timeout`,
    returns. If it does not finish in time, logs a warning and returns (does not
    hang the caller).
    """
    if task is None or task.done():
        return
    await stop_tasks((task,), timeout, what)


async def stop_tasks(tasks: Iterable[asyncio.Task[Any] | None], timeout: float, what: str) -> None:
    """Cancel several background tasks and wait for them all to finish, for at most `timeout` total."""
    given = [t for t in tasks if t is not None]
    pending = [t for t in given if not t.done()]
    if not pending:
        return

    for task in pending:
        task.cancel()

    deadline = asyncio.get_running_loop().time() + timeout
    step = min(0.1, timeout / 5) if timeout > 0 else 0.0

    while pending:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            names = [t.get_name() for t in pending]
            log.warning(
                "Timed out waiting for %s to stop after %.2fs (%d of %d still running: %s)",
                what,
                timeout,
                len(pending),
                len(given),
                names,
            )
            for task in pending:
                task.add_done_callback(_consume_abandoned_exception)
            return
        for task in pending:
            task.cancel()
        _, unready = await asyncio.wait(pending, timeout=min(step, remaining))
        pending = list(unready)


def spawn_task(
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> asyncio.Task[T]:
    """Spawn a background task with strong referencing and optional unhandled exception logging.

    The task is held in a module-level set until it completes, avoiding the
    classic "Task was destroyed but it is still pending" garbage collection bug.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def done(t: asyncio.Task[T]) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    log.exception("Error handler for task %s raised", t.get_name())
            else:
                log.error("Background task %s failed: %s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(done)
    return task


async def stop_background_tasks(timeout: float = 5.0, what: str = "background tasks") -> None:
    """Cancel and wait for all currently running tasks started by ``spawn_task``."""
    await stop_tasks(tuple(_BACKGROUND_TASKS), timeout, what)
