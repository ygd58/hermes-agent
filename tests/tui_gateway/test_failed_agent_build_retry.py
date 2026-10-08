"""A failed agent build must not wedge the session.

The first send with the local server off fails agent init and stores
``agent_error`` with ``agent_ready`` set. Before the fix, prompt.submit's
build kick was a no-op on that state (``agent_build_started`` stayed True),
so every later send — including the error card's Retry — replayed the stored
failure even after the server came back; only NEW sessions worked. The fix
routes prompt.submit through ``_restart_completed_failed_agent_build`` first,
which clears one completed failed generation and rebuilds with fresh
provider resolution.

These tests pin the restart helper's contract directly: it is the seam the
prompt path now calls, and its answer decides retry-vs-replay.
"""

from __future__ import annotations

import threading

from tui_gateway import server


def failed_session(tmp_path, sid: str) -> dict:
    """A session record whose deferred agent build COMPLETED in failure."""
    ready = threading.Event()
    ready.set()
    session = {
        "agent": None,
        "agent_ready": ready,
        "agent_build_started": True,
        "agent_error": "The local model server is turned off.",
        "cwd": str(tmp_path),
        "history": [],
        "history_lock": threading.RLock(),
        "profile_home": str(tmp_path),
        "running": False,
        "session_key": sid,
    }
    server._sessions[sid] = session
    return session


def test_restart_clears_failed_generation_and_rebuilds(tmp_path, monkeypatch):
    sid = "wedged-session"
    session = failed_session(tmp_path, sid)
    failed_ready = session["agent_ready"]

    rebuilt = []
    monkeypatch.setattr(server, "_start_agent_build",
                        lambda s, sess: rebuilt.append(s))

    try:
        assert server._restart_completed_failed_agent_build(
            sid, session, failed_ready) is True

        # The failed generation is gone: error cleared, fresh unset ready
        # event, build flag dropped — the next build starts from zero.
        assert session["agent_error"] is None
        assert session["agent_ready"] is not failed_ready
        assert not session["agent_ready"].is_set()
        assert "agent_build_started" not in session
        assert rebuilt == [sid]
    finally:
        server._sessions.pop(sid, None)


def test_deferred_build_failure_logs_the_traceback(tmp_path, monkeypatch, caplog):
    """Regression for issue #134890: a deferred-build exception (e.g. the reported bare
    "maximum recursion depth exceeded") stored agent_error and emitted a user-facing
    message, but the traceback was never logged -- so the failing frame could not be
    identified from errors.log/agent.log/etc. after the fact."""
    import logging

    sid = "recursing-session"
    ready = threading.Event()
    session = {
        "agent_ready": ready,
        "agent_build_started": False,
        "cwd": str(tmp_path),
        "history": [],
        "history_lock": threading.RLock(),
        "profile_home": str(tmp_path),
        "running": False,
        "session_key": sid,
    }
    server._sessions[sid] = session

    monkeypatch.setattr(server, "_bind_build_profile_scopes", lambda profile_home: None)
    monkeypatch.setattr(server, "_make_agent", lambda *a, **kw: (_ for _ in ()).throw(RecursionError("maximum recursion depth exceeded")))

    try:
        with caplog.at_level(logging.ERROR, logger="tui_gateway.server"):
            server._start_agent_build(sid, session)
            assert ready.wait(timeout=5.0), "deferred build never completed"

        assert session["agent_error"] == "maximum recursion depth exceeded"
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "Deferred agent build failed" in logged
        assert sid in logged
        # caplog.records carries the formatted traceback via the record's own exc_text once
        # rendered; check the record that logged it actually carries exception info.
        assert any(record.exc_info is not None for record in caplog.records), \
            "no record carries exc_info -- the traceback itself was not attached"
    finally:
        server._sessions.pop(sid, None)


def test_restart_declines_every_non_failure_state(tmp_path, monkeypatch):
    """False (caller falls through to the normal build) when there is no
    completed failure: healthy agent, build still in flight, or no error."""
    sid = "healthy-session"
    session = failed_session(tmp_path, sid)
    monkeypatch.setattr(server, "_start_agent_build",
                        lambda s, sess: None)

    try:
        # Build still in flight: ready not set.
        in_flight = threading.Event()
        session["agent_ready"] = in_flight
        assert server._restart_completed_failed_agent_build(
            sid, session, in_flight) is False

        # No error recorded.
        done = threading.Event()
        done.set()
        session["agent_ready"] = done
        session["agent_error"] = None
        assert server._restart_completed_failed_agent_build(
            sid, session, done) is False

        # Agent actually built.
        session["agent_error"] = "stale text"
        session["agent"] = object()
        assert server._restart_completed_failed_agent_build(
            sid, session, done) is False

        # No ready event at all.
        assert server._restart_completed_failed_agent_build(
            sid, session, None) is False
    finally:
        server._sessions.pop(sid, None)
