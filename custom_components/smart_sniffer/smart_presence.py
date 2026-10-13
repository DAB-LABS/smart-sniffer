"""Has a drive with no SMART data now given SMART data before? (GH #61)

A drive can keep answering the agent, model and serial and all, while giving
no SMART data. Two very different situations look like that:

  - It never gave any: a USB enclosure that does not pass SMART through.
    Attention Needed is UNSUPPORTED, as it always was.
  - It gave SMART data before and has stopped: on macOS, the third-party SAT
    driver that passes SMART through a USB bridge stopped working, and came
    back when it was reinstalled. Blaming the enclosure would be wrong, and
    the drive is expected back. Attention Needed is unknown, Attention
    Reasons says ``REASON_SMART_NOT_AVAILABLE``, and no notification is
    raised.

"Before" is either of two memories, so losing one does not lose the answer:
this tracker's, for the life of the Home Assistant run, and the agent's
``smart_lost`` field (agent v0.8.3), for the life of the agent run. A restart
of both while SMART is missing reads as "never", which is the old behaviour.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

from typing import Any

from .attention import _has_usable_smart_data, coerce_smart_data

REASON_SMART_NOT_AVAILABLE = "SMART data not available right now"

EVENT_LOST = "lost"
EVENT_BACK = "back"


def has_usable_smart_data(drive_data: dict[str, Any]) -> bool:
    """The integration's own test for SMART data it can evaluate."""
    return _has_usable_smart_data(coerce_smart_data(drive_data))


def smart_lost(drive_data: dict[str, Any], had_smart: bool) -> bool:
    """No usable SMART data now, but there was before: per the caller
    (``had_smart``) or per the agent (``smart_lost``)."""
    if has_usable_smart_data(drive_data):
        return False
    return had_smart or drive_data.get("smart_lost") is True


class SmartPresence:
    """Per drive id: has it given SMART data this run, and is it lost now."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lost: set[str] = set()

    def update(self, drive_id: str, drive_data: dict[str, Any]) -> str | None:
        """Record one poll of a drive. Returns EVENT_LOST when it has just
        lost its SMART data, EVENT_BACK when it has just got it back, and
        None otherwise, so each change is reported once."""
        if has_usable_smart_data(drive_data):
            self._seen.add(drive_id)
        lost = smart_lost(drive_data, drive_id in self._seen)
        was_lost = drive_id in self._lost
        if lost and not was_lost:
            self._lost.add(drive_id)
            return EVENT_LOST
        if was_lost and not lost:
            self._lost.discard(drive_id)
            return EVENT_BACK
        return None

    def is_lost(self, drive_id: str) -> bool:
        return drive_id in self._lost

    def forget(self, drive_id: str) -> None:
        """A device the user removed: it starts afresh if it comes back."""
        self._seen.discard(drive_id)
        self._lost.discard(drive_id)
