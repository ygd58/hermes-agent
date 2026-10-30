"""``_telegram_retry_delay`` classification (#146922).

Deliberately not in ``test_send_message_tool.py``: that module skips itself entirely when
python-telegram-bot isn't installed, and nothing here needs it -- the exceptions below are shaped like
PTB's (class name + ``telegram.error`` module), so this guard also runs in a bare environment.
"""

import pytest

from tools.send_message_senders import _telegram_retry_delay


def _ptb_error(name, message, base=Exception):
    """An exception shaped like python-telegram-bot's, without importing it. PTB's BadRequest subclasses
    NetworkError, which is why a type check alone can't tell a parse error from a gateway failure."""
    return type(name, (base,), {"__module__": "telegram.error"})(message)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # The reported case and its variants: never transient, whatever digits the text carries.
        (_ptb_error("BadRequest", "Can't parse entities: ... starting at byte offset 502"), None),
        (_ptb_error("BadRequest", "Bad Request: bad gateway in url"), None),
        (_ptb_error("Forbidden", "Forbidden: bot was blocked by the user (503)"), None),
        (Exception("can't parse entities: bad tag near /v1/502/x"), None),
        # A status code is a whole number, not a substring of a longer one.
        (_ptb_error("NetworkError", "flood id 15029 unknown"), None),
        # Behaviour that must NOT change.
        (_ptb_error("NetworkError", "Bad Gateway"), 1.0),
        (_ptb_error("NetworkError", "Service Unavailable (503)"), 1.0),
        (_ptb_error("NetworkError", "Too Many Requests (429)"), 1.0),
        (Exception("502 Bad Gateway"), 1.0),
        (type("RetryAfterLike", (Exception,), {"retry_after": 7})("flood"), 7.0),
        (_ptb_error("TimedOut", "Timed out"), None),
    ],
)
def test_retry_delay_classification(exc, expected):
    assert _telegram_retry_delay(exc, 0) == expected


def test_backoff_grows_with_the_attempt_number():
    assert [_telegram_retry_delay(Exception("502 Bad Gateway"), a) for a in (0, 1, 2)] == [1.0, 2.0, 4.0]


def test_same_class_name_from_another_library_is_not_treated_as_a_telegram_answer():
    """Matching is by class name AND a ``telegram`` module: another library's BadRequest wrapping a real
    gateway failure must keep its retry."""
    other = type("BadRequest", (Exception,), {"__module__": "google.api_core.exceptions"})("502 Bad Gateway")

    assert _telegram_retry_delay(other, 0) == 1.0
