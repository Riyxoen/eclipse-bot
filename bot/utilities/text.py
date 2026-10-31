"""Small, dependency-free text helpers shared by the command cogs."""

from __future__ import annotations


def build_ping_response(latency_ms: float) -> str:
    """Format the ping response; kept pure for unit testing.

    Defined once here and re-exported by both ``/ping`` and ``/health ping``
    so the two commands can never word their reply differently.
    """
    return f"Pong! Gateway latency is {latency_ms:.0f} ms."
