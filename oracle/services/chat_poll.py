"""Poll-based transport for the Oracle chat, for clients that cannot hold SSE.

WHY THIS EXISTS
---------------
``POST /api/chat`` (oracle_server) streams the chat as Server-Sent Events.
That is the right shape for a desktop browser and the wrong shape for a
phone: Android and iOS suspend a backgrounded app and tear down its long
-lived sockets, so an SSE turn dies mid-answer and the client cannot tell a
finished turn from a killed one — and cannot resume either.

So mobile gets the same turn expressed as *state a client can re-read*:

    POST   /api/mobile/chat/turn            -> start, return a run id
    GET    /api/mobile/chat/turn/<run_id>   -> events[after:], repeatable
    DELETE /api/mobile/chat/turn/<run_id>   -> best-effort cancel

The turn runs on a background thread that drains the EXISTING
``ChatEngine.chat()`` generator into an ordered list. Nothing about the chat
logic is reimplemented here: this module is a buffer and an index, and if it
ever starts making decisions about content that is a bug. The events on the
wire are the raw engine dicts, byte-for-byte what the SSE route sends after
its ``data: `` prefix, so one client renderer serves both transports.

The index is the whole contract. ``after`` is how many events the client
already holds; the reply's ``next`` is what it sends next time. Because the
list is append-only and a completed run stays readable until it is reaped, a
phone that was frozen for ten minutes can come back and ask ``after=0`` for
the entire turn — the property SSE cannot offer.
"""
from __future__ import annotations

import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from oracle.services import api_auth
from oracle.services.api_auth import extract_bearer

#: Capability advertised on ``/api/mobile/info``. Wired into
#: ``mobile_api.CAPABILITIES`` by that module — defined here so the surface
#: and its name ship together.
CAPABILITY = "chat"

# ── Registry bounds ───────────────────────────────────────
# Both are hard memory bounds, not tuning knobs: every retained run holds its
# full event list (tool results included), so an unbounded registry is a leak
# with a chat UI attached to it.
#
# Cap: the newest N runs survive; older ones are dropped on insert even if
# they are unread. A phone polls one turn at a time, so 32 is many sessions'
# worth of scrollback, and the client can always re-read history from the
# conversation store instead.
MAX_RUNS = 32
#: How long a finished (done/error/aborted) run stays pollable. Sized for
#: "the app was backgrounded and came back", which is the case that motivates
#: the whole module. Running turns are never reaped, whatever their age.
RUN_TTL_S = 30 * 60

# ── Long-poll ─────────────────────────────────────────────
# Mirrors the `/feed?wait=` idiom in mobile_api: a bounded hold, a coarse
# tick, and a hard cap on how many server threads may be parked at once.
# Past the cap `wait` degrades to an immediate answer — a slower client, not
# a wrong one.
MAX_WAIT_S = 25
_WAIT_TICK_S = 0.25
_MAX_WAITERS = 4
_waiters = threading.Semaphore(_MAX_WAITERS)

bp = Blueprint("mobile_chat", __name__, url_prefix="/api/mobile/chat")

# Late-bound by oracle_server (avoids a circular import, and lets the engine
# be swapped in tests). ``configure`` takes accessors rather than objects
# because the server rebuilds both on ``_init_services``.
_get_engine = None   # () -> ChatEngine | None
_get_store = None    # () -> ChatStore | None
_get_cfg = None      # () -> dict


def configure(get_engine, get_store, get_cfg=None) -> None:
    """Late-bind the chat engine, the conversation store, and the config."""
    global _get_engine, _get_store, _get_cfg
    _get_engine = get_engine
    _get_store = get_store
    _get_cfg = get_cfg


def _engine():
    return _get_engine() if _get_engine is not None else None


def _store():
    return _get_store() if _get_store is not None else None


def _cfg() -> dict:
    if _get_cfg is not None:
        return _get_cfg() or {}
    return {}


# ── Auth ──────────────────────────────────────────────────

@bp.before_request
def _chat_auth_guard():
    """Bearer gate on every method, GETs included.

    Deliberately the same token and the same shape as ``mobile_api``'s guard
    so the phone's existing credential works unchanged. Kept as its own
    function rather than imported because a blueprint's ``before_request``
    only fires for its own blueprint.
    """
    if request.method == "OPTIONS":
        return None  # CORS preflight
    if not _cfg().get("mobile_api_enabled", True):
        return jsonify({"error": "mobile API disabled"}), 404
    if not api_auth.verify(extract_bearer(request.headers.get("Authorization"))):
        return jsonify({"error": "unauthorized"}), 401
    return None


# ── Run registry ──────────────────────────────────────────

class _Run:
    """One in-flight or finished chat turn.

    ``events`` is append-only and is only ever mutated under the registry
    lock, so a reader holding that lock sees a whole number of events and
    never a half-written one.
    """

    __slots__ = ("run_id", "conversation_id", "events", "status", "error",
                 "started", "finished", "cancelled", "thread")

    def __init__(self, run_id: str, conversation_id: str):
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.events: list[dict] = []
        self.status = "running"          # running | done | error | aborted
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancelled = False
        self.thread: threading.Thread | None = None


_lock = threading.Lock()
#: run_id -> _Run, insertion-ordered so the oldest is the first key.
_runs: dict[str, _Run] = {}
#: Signalled on every append and every terminal transition. One shared
#: condition rather than one per run: waiters are few and a spurious wake is
#: cheap (recheck the index and go back to sleep).
_event_added = threading.Condition(_lock)


def _reap_locked(now: float | None = None) -> None:
    """Drop finished runs past the TTL, then trim to the cap. Caller holds the lock.

    Order matters: TTL first, so an expired run is dropped in preference to a
    fresh one that only lost on age.
    """
    now = time.time() if now is None else now
    for run_id, run in list(_runs.items()):
        if run.finished is not None and (now - run.finished) > RUN_TTL_S:
            del _runs[run_id]
    while len(_runs) > MAX_RUNS:
        _runs.pop(next(iter(_runs)))


def _finish_locked(run: _Run, status: str, error: str | None = None) -> None:
    """Terminal transition. Caller holds the lock.

    A run that was cancelled keeps ``aborted`` even if the generator went on
    to end normally, so the client's view matches the DELETE it issued.
    """
    if run.status in ("done", "error", "aborted"):
        return
    run.status = "aborted" if run.cancelled and status == "done" else status
    run.error = error
    run.finished = time.time()


def _drain(run: _Run, conv_id: str, message: str) -> None:
    """Drain the existing engine generator into the run's event list.

    Runs on a background thread. The generator is consumed exactly as the SSE
    route consumes it; the only additions are the cancel check between events
    and the terminal bookkeeping, both of which are transport concerns.
    """
    try:
        engine = _engine()
        if engine is None:
            with _lock:
                _finish_locked(run, "error", "not initialized")
                _event_added.notify_all()
            return
        for event in engine.chat(conv_id, message):
            with _lock:
                if run.cancelled:
                    _finish_locked(run, "aborted")
                    _event_added.notify_all()
                    return
                run.events.append(event if isinstance(event, dict)
                                  else {"type": "text", "content": str(event)})
                _event_added.notify_all()
    except BaseException as exc:  # noqa: BLE001 — the thread must never die quietly
        # A generator that raises is a real outcome the phone has to see: an
        # exception on a daemon thread would otherwise leave the run stuck on
        # "running" until the TTL, which reads as a hang.
        with _lock:
            _finish_locked(run, "error", f"{type(exc).__name__}: {exc}")
            _event_added.notify_all()
        return
    with _lock:
        _finish_locked(run, "done")
        _event_added.notify_all()


def _snapshot(run: _Run, after: int) -> dict:
    """Wire view of a run from index *after*. Caller holds the lock."""
    after = max(0, min(after, len(run.events)))
    return {
        "run_id": run.run_id,
        "conversation_id": run.conversation_id,
        "next": len(run.events),
        "status": run.status,
        "events": list(run.events[after:]),
        "error": run.error,
    }


def reset_for_tests() -> None:
    """Drop every run. Test-support only."""
    with _lock:
        _runs.clear()


# ── Routes: the turn ──────────────────────────────────────

@bp.route("/turn", methods=["POST"])
def chat_turn_start():
    """Start a turn asynchronously. -> 201 {run_id, conversation_id}.

    The conversation is created here rather than inside the generator (which
    would also do it) so the id can be returned with the 201. A phone that
    dies immediately after this call has still persisted the conversation and
    can find it in the list.
    """
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return jsonify({"error": "not initialized"}), 500

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "No message provided"}), 400
    conv_id = data.get("conversation_id") or None
    if not conv_id:
        conv_id = store.create_conversation()

    run = _Run(uuid.uuid4().hex[:16], conv_id)
    with _lock:
        _reap_locked()
        _runs[run.run_id] = run
        _reap_locked()

    thread = threading.Thread(
        target=_drain, args=(run, conv_id, message),
        daemon=True, name=f"oracle-chat-poll-{run.run_id}",
    )
    run.thread = thread
    thread.start()
    return jsonify({"run_id": run.run_id, "conversation_id": conv_id}), 201


@bp.route("/turn/<run_id>", methods=["GET"])
def chat_turn_poll(run_id):
    """Events from ``after``, optionally holding up to ``wait`` seconds.

    Query: after (events already held, default 0), wait (0-25).

    An empty ``events`` with status ``running`` after a full hold is the
    normal answer, not an error: the client reconnects with the same
    ``next``. Holding stops the moment the run reaches a terminal state, so
    the last poll of a turn returns immediately rather than parking for the
    full deadline.
    """
    try:
        after = max(0, int(request.args.get("after", 0)))
    except (TypeError, ValueError):
        after = 0
    try:
        wait_s = max(0, min(MAX_WAIT_S, int(request.args.get("wait", 0))))
    except (TypeError, ValueError):
        wait_s = 0

    with _lock:
        run = _runs.get(run_id)
        if run is None:
            return jsonify({"error": "unknown run"}), 404
        if len(run.events) > after or run.status != "running" or not wait_s:
            return jsonify(_snapshot(run, after))

    # Nothing new yet and the client asked to hold. Past the waiter cap we
    # answer immediately with the empty slice instead of queueing.
    if not _waiters.acquire(blocking=False):
        with _lock:
            return jsonify(_snapshot(_runs.get(run_id) or run, after))
    try:
        deadline = time.monotonic() + wait_s
        with _lock:
            while True:
                run = _runs.get(run_id)
                if run is None:
                    return jsonify({"error": "unknown run"}), 404
                if len(run.events) > after or run.status != "running":
                    return jsonify(_snapshot(run, after))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return jsonify(_snapshot(run, after))
                _event_added.wait(min(_WAIT_TICK_S, remaining))
    finally:
        _waiters.release()


@bp.route("/turn/<run_id>", methods=["DELETE"])
def chat_turn_abort(run_id):
    """Best-effort cancel. -> {aborted: bool}.

    Cooperative by construction: the flag is read between events, so the
    worker stops at the next yield rather than being killed mid-tool-call.
    This never joins the thread — a generator blocked on a slow LLM read
    would otherwise wedge the request handler for as long as that read takes.
    The run stays readable, so the client still collects what it got.
    """
    with _lock:
        run = _runs.get(run_id)
        if run is None:
            return jsonify({"error": "unknown run"}), 404
        if run.status != "running":
            return jsonify({"aborted": False, "status": run.status})
        run.cancelled = True
        _event_added.notify_all()
        return jsonify({"aborted": True, "status": run.status})


# ── Routes: conversation + command proxies ────────────────
# Thin pass-throughs to the same stores the desktop routes use, so the phone
# does not need a second credential or a second base path.

@bp.route("/conversations", methods=["GET"])
def chat_conversations_list():
    store = _store()
    if store is None:
        return jsonify({"error": "not initialized"}), 500
    limit = request.args.get("limit", 50, type=int)
    return jsonify({"conversations": store.list_conversations(limit)})


@bp.route("/conversations", methods=["POST"])
def chat_conversations_create():
    store = _store()
    if store is None:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    return jsonify({"id": store.create_conversation(data.get("title"))}), 201


@bp.route("/conversations/<conv_id>", methods=["GET"])
def chat_conversation_get(conv_id):
    store = _store()
    if store is None:
        return jsonify({"error": "not initialized"}), 500
    return jsonify({"conversation_id": conv_id,
                    "messages": store.get_conversation(conv_id)})


@bp.route("/commands", methods=["GET"])
def chat_commands():
    engine = _engine()
    if engine is None:
        return jsonify({"error": "not initialized"}), 500
    return jsonify({"commands": engine.get_commands()})


@bp.route("/command", methods=["POST"])
def chat_command():
    """Slash commands stay synchronous — they are store edits, not LLM turns."""
    engine = _engine()
    if engine is None:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "No command provided"}), 400
    return jsonify(engine.execute_command(data.get("conversation_id"), command))
