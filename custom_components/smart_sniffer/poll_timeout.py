"""Say which agent timed out, and on which request.

Without this a request to the agent that runs past its time limit reaches
Home Assistant as a bare ``TimeoutError``, and the log says only "Timeout
fetching smart_sniffer data", with no host. With several agents set up there
is no telling which one.

The coordinator catches the timeout and asks ``PollTimeouts`` for the
message. The first timeout in a row is logged as one warning naming the
agent's host and port and the request path; later ones are not, so a host
that stays down does not log every poll. The first poll that succeeds after
that logs one line saying the agent answers again. The message also goes into
``UpdateFailed``, so Home Assistant's own line names the host too.

The path is the agent API path only. The token travels in a header and is
never part of it.

HA-free so it can be tested without Home Assistant.
"""

from __future__ import annotations

import logging

# Time limit, in seconds, for each request of a poll of the agent.
AGENT_POLL_TIMEOUT = 30


class PollTimeouts:
    """Tracks whether the last poll of one agent timed out."""

    def __init__(self, logger: logging.Logger, host: str, port: int, seconds: float) -> None:
        self._logger = logger
        self._where = f"{host}:{port}"
        self._seconds = seconds
        self.failing = False

    def timed_out(self, path: str) -> str:
        """Record a timeout on ``path`` and return the message for UpdateFailed."""
        message = (
            f"SMART Sniffer agent at {self._where} did not answer {path} "
            f"within {self._seconds:g} seconds"
        )
        if not self.failing:
            self._logger.warning("%s; will keep trying every poll", message)
        self.failing = True
        return message

    def succeeded(self) -> None:
        """Record a poll that completed; log the recovery once."""
        if self.failing:
            self._logger.info("SMART Sniffer agent at %s is answering again", self._where)
        self.failing = False
