"""Graceful shutdown handling for the bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("riyxoen.shutdown")


class ShutdownManager:
    """Coordinate a graceful shutdown on SIGINT/SIGTERM.

    A signal handler (or a test) calls :meth:`request_stop`; the main run loop
    waits on :meth:`wait_for_stop` and then closes the client with a bounded
    timeout so shutdown never hangs.
    """

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._stop_event = asyncio.Event()

    @property
    def stop_event(self) -> asyncio.Event:
        """The event set when a shutdown has been requested."""
        return self._stop_event

    def request_stop(self) -> None:
        """Signal that the bot should shut down.

        Safe to call from a signal handler (it never blocks or raises).
        """
        self._stop_event.set()
        logger.info("shutdown requested")

    async def wait_for_stop(self) -> None:
        """Block until a shutdown has been requested."""
        await self._stop_event.wait()

    async def close_client(self, client: Any) -> None:
        """Close a client with a bounded timeout; never hang."""
        try:
            await asyncio.wait_for(client.close(), timeout=self.timeout_seconds)
        except TimeoutError:
            logger.error(
                "client.close() exceeded %.1fs timeout; forcing shutdown",
                self.timeout_seconds,
            )
        except Exception:
            logger.exception("client.close() raised during shutdown")
