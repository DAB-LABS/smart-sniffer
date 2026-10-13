"""A poll that times out names the agent's host and the request, once."""

from __future__ import annotations

import importlib
import logging

poll_timeout = importlib.import_module("custom_components.smart_sniffer.poll_timeout")

LOGGER = "smart_sniffer_test_poll_timeout"


def _timeouts():
    return poll_timeout.PollTimeouts(logging.getLogger(LOGGER), "10.0.0.7", 9099, 30)


def _lines(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == LOGGER]


def test_first_timeout_warns_with_host_and_path(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    t = _timeouts()
    msg = t.timed_out("/api/drives/fake-serial-01")
    assert msg == (
        "SMART Sniffer agent at 10.0.0.7:9099 did not answer "
        "/api/drives/fake-serial-01 within 30 seconds"
    )
    assert _lines(caplog) == [("WARNING", msg + "; will keep trying every poll")]
    assert t.failing


def test_repeated_timeouts_log_once_and_recovery_once(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    t = _timeouts()
    t.timed_out("/api/drives")
    second = t.timed_out("/api/drives")
    assert "10.0.0.7:9099" in second  # the UpdateFailed text still names the host
    assert len(_lines(caplog)) == 1
    t.succeeded()
    t.succeeded()
    assert _lines(caplog)[1:] == [("INFO", "SMART Sniffer agent at 10.0.0.7:9099 is answering again")]
    assert not t.failing


def test_success_without_a_timeout_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _timeouts().succeeded()
    assert _lines(caplog) == []


def test_the_message_never_carries_a_token():
    # The token is a header; only the API path reaches the message.
    msg = _timeouts().timed_out("/api/drives")
    assert "Bearer" not in msg and "token" not in msg.lower()
