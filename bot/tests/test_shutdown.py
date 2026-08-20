"""Tests for graceful shutdown behavior."""

from __future__ import annotations

import asyncio

from bot.core.shutdown import ShutdownManager


async def test_request_stop_fires_event() -> None:
    manager = ShutdownManager()
    assert not manager.stop_event.is_set()
    manager.request_stop()
    assert manager.stop_event.is_set()


async def test_wait_for_stop_returns_after_request() -> None:
    manager = ShutdownManager()
    waiter = asyncio.create_task(manager.wait_for_stop())
    await asyncio.sleep(0)
    assert not waiter.done()
    manager.request_stop()
    await asyncio.wait_for(waiter, timeout=1)


async def test_close_client_completes() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()
    await ShutdownManager(timeout_seconds=1).close_client(client)
    assert client.closed


async def test_close_client_times_out_gracefully() -> None:
    class HangingClient:
        async def close(self) -> None:
            await asyncio.sleep(60)

    # Must neither raise nor hang; the timeout abandons the close.
    await ShutdownManager(timeout_seconds=0.05).close_client(HangingClient())


async def test_close_client_swallows_errors() -> None:
    class BrokenClient:
        async def close(self) -> None:
            raise RuntimeError("boom")

    # Must not propagate the error; it is logged instead.
    await ShutdownManager().close_client(BrokenClient())
