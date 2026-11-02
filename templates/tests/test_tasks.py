"""Tests for tasks.py.

Shared by the sibling servers along with tasks.py; only the package name
in the import below differs between repos. Change it in mcp-server-shared, not
in a copy, then sync.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from __PKG__.tasks import (
    _BACKGROUND_TASKS,
    _consume_abandoned_exception,
    spawn_task,
    stop_background_tasks,
    stop_task,
    stop_tasks,
)


@pytest.mark.anyio
async def test_stop_task_cancels_and_waits():
    cancelled = False

    async def worker():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    task = asyncio.create_task(worker(), name="worker")
    await asyncio.sleep(0.01)  # let it enter sleep
    await stop_task(task, timeout=1.0, what="test worker")
    assert task.done()
    assert cancelled


@pytest.mark.anyio
async def test_stop_task_handles_none_and_already_done():
    await stop_task(None, timeout=1.0, what="none")

    async def finished():
        return 42

    task = asyncio.create_task(finished())
    await task
    await stop_task(task, timeout=1.0, what="done")


@pytest.mark.anyio
async def test_stop_task_warns_on_timeout(caplog):
    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Swallows cancellation
                await asyncio.sleep(0.05)

    task = asyncio.create_task(stubborn(), name="stubborn")
    await asyncio.sleep(0.01)

    with caplog.at_level(logging.WARNING):
        await stop_task(task, timeout=0.1, what="stubborn worker")

    assert "Timed out waiting for stubborn worker" in caplog.text
    assert "stubborn" in caplog.text
    # Clean up the stubborn task for real by not swallowing next time
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=0.2)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


@pytest.mark.anyio
async def test_stop_tasks_multiple():
    count = 0

    async def worker():
        nonlocal count
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            count += 1
            raise

    t1 = asyncio.create_task(worker(), name="t1")
    t2 = asyncio.create_task(worker(), name="t2")
    await asyncio.sleep(0.01)
    await stop_tasks([t1, None, t2], timeout=1.0, what="multiple")
    assert t1.done() and t2.done()
    assert count == 2


@pytest.mark.anyio
async def test_spawn_task_keeps_strong_reference():
    finished = False

    async def fast():
        nonlocal finished
        await asyncio.sleep(0.01)
        finished = True

    t = spawn_task(fast(), name="fast")
    assert t in _BACKGROUND_TASKS
    await t
    # After completion, discarded from the set
    assert t not in _BACKGROUND_TASKS
    assert finished


@pytest.mark.anyio
async def test_spawn_task_logs_unhandled_exception(caplog):
    async def boom():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        t = spawn_task(boom(), name="exploder")
        await asyncio.sleep(0.02)

    assert "Background task exploder failed" in caplog.text
    assert "boom" in caplog.text
    assert t not in _BACKGROUND_TASKS


@pytest.mark.anyio
async def test_spawn_task_calls_on_error():
    caught = None

    def handler(exc: Exception):
        nonlocal caught
        caught = exc

    async def boom():
        raise ValueError("custom error")

    spawn_task(boom(), name="handler-test", on_error=handler)
    await asyncio.sleep(0.02)

    assert isinstance(caught, ValueError)
    assert str(caught) == "custom error"


@pytest.mark.anyio
async def test_consume_abandoned_exception():
    async def fails():
        raise RuntimeError("abandoned")

    task = asyncio.create_task(fails())
    await asyncio.sleep(0.01)
    _consume_abandoned_exception(task)
    assert task.done()


@pytest.mark.anyio
async def test_stop_background_tasks():
    async def worker():
        await asyncio.sleep(10)

    t = spawn_task(worker(), name="bg-worker")
    assert t in _BACKGROUND_TASKS
    await stop_background_tasks(timeout=0.5)
    assert t.done()
    assert t not in _BACKGROUND_TASKS
