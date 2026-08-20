"""Small asyncio helpers used across the bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("riyxoen.async_utils")


async def cancel_task(task: asyncio.Task[Any], *, timeout: float = 5.0) -> None:
    """Cancel a task and wait for it to finish, swallowing ``CancelledError``.

    If the task does not stop within ``timeout`` seconds, the wait is
    abandoned (the task is left cancelled and the event loop cleans it up).
    """
    if task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.CancelledError, TimeoutError):
        logger.debug("task %r cancelled", task)
