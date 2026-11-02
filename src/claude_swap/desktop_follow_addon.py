"""mitmproxy addon: bill Claude Desktop's Code tab to cswap's active account.

The Code tab runs its CLI with the app's own login, so ``cswap switch`` never
reaches it. Run behind this addon, the Code tab's inference requests carry the
active account's access token instead. Only ``/v1/messages`` is rewritten;
everything else (profile, usage, OAuth, Remote Control) keeps the app's login.
This is the Windows stand-in for cswap-pin's ``pin.desktopFollows``, which needs
POSIX file locks and FIFOs.

    mitmdump -p 8899 -s desktop_follow_addon.py

Read-only: it never refreshes or writes a credential. A token that has expired
is not substituted, so the app's own login keeps working instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

SESSION_HEADER = "x-claude-code-session-id"
CLASS_HEADER = "x-claude-code-request-class"
REWRITE_PREFIX = "/v1/messages"
API_HOST = "api.anthropic.com"
_CACHE: dict = {"mtime": None, "token": None}
_EMAIL: dict = {"mtime": None, "email": None}


def credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def sequence_email(path: Path | None = None) -> str | None:
    """The account cswap last switched to, from its own record. The live credential belongs to that
    account, while ``~/.claude.json`` can be rewritten by the app with its own login."""
    path = path or Path.home() / ".claude-swap-backup" / "sequence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        email = (data.get("accounts") or {}).get(str(data.get("activeAccountNumber")), {}).get("email")
    except (OSError, ValueError, AttributeError):
        return None
    return email if isinstance(email, str) and email else None


def active_email(path: Path | None = None) -> str | None:
    """The email of the account the live credential belongs to, for the log; None when unknown."""
    if path is None and (email := sequence_email()):
        return email
    path = path or config_path()
    try:
        mtime = path.stat().st_mtime_ns
        if _EMAIL["mtime"] != mtime:
            account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}
            _EMAIL["mtime"] = mtime
            _EMAIL["email"] = account.get("emailAddress")
    except (OSError, ValueError, AttributeError):
        return None
    email = _EMAIL["email"]
    return email if isinstance(email, str) and email else None


def active_token(path: Path | None = None, now_ms: float | None = None) -> str | None:
    """The active account's unexpired access token, or None."""
    path = path or credentials_path()
    try:
        mtime = path.stat().st_mtime_ns
        if _CACHE["mtime"] != mtime:
            oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
            _CACHE["mtime"] = mtime
            _CACHE["token"] = (oauth.get("accessToken"), oauth.get("expiresAt"))
    except (OSError, ValueError):
        return None
    token, expires_at = _CACHE["token"] or (None, None)
    if not isinstance(token, str) or not token:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if isinstance(expires_at, (int, float)) and expires_at <= now_ms + 30_000:
        return None
    return token


def state_dir() -> Path:
    return Path.home() / ".claude-swap-backup" / "desktop-follow"


_STATE: dict = {"mtime": None, "per_session": False}


def per_session_enabled() -> bool:
    """Whether each Code tab session keeps its own account (``cswap desktop-follow --per-session on``).
    Off, every session follows the account cswap has active, as before."""
    path = state_dir() / "state.json"
    try:
        mtime = path.stat().st_mtime_ns
        if _STATE["mtime"] != mtime:
            _STATE["mtime"] = mtime
            _STATE["per_session"] = bool(json.loads(path.read_text(encoding="utf-8")).get("per_session"))
    except (OSError, ValueError, AttributeError):
        return False
    return _STATE["per_session"]


_SESSIONS: dict = {"mtime": None, "data": {}}


def load_sessions(path: Path | None = None) -> dict:
    """``session id -> {account, subagents, pinned, seen}``, cached by mtime."""
    path = path or state_dir() / "sessions.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _SESSIONS.update(mtime=None, data={})
        return _SESSIONS["data"]
    if _SESSIONS["mtime"] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8")).get("sessions") or {}
        except (OSError, ValueError, AttributeError):
            data = _SESSIONS["data"]
        _SESSIONS.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    return _SESSIONS["data"]


def save_sessions(data: dict, path: Path | None = None) -> None:
    """Replace the table atomically; a reader never sees half a file."""
    path = path or state_dir() / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "sessions": data}, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    _SESSIONS.update(mtime=None)


_SEEN_WRITTEN: dict = {}


def account_for(session: str, request_class: str, active: str | None, path: Path | None = None) -> str | None:
    """The account a request of ``session`` bills. A session met for the first time is pinned to the
    active account; from then on a switch moves only sessions started later. A subagent request uses
    the session's ``subagents`` account when it has one."""
    if not session:
        return active
    sessions = load_sessions(path)
    entry = sessions.get(session)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if entry is None:
        if not active:
            return None
        sessions = dict(sessions)
        sessions[session] = {"account": active, "pinned": now, "seen": now}
        save_sessions(sessions, path)
        _SEEN_WRITTEN[session] = time.time()
        entry = sessions[session]
    elif time.time() - _SEEN_WRITTEN.get(session, 0) > 600:
        # Keep `seen` roughly current so idle entries can be expired; at most every ten minutes.
        _SEEN_WRITTEN[session] = time.time()
        sessions = dict(sessions)
        sessions[session] = {**entry, "seen": now}
        save_sessions(sessions, path)
        entry = sessions[session]
    if request_class == "subagent" and entry.get("subagents"):
        return entry["subagents"]
    return entry.get("account") or active


def tokens_path() -> Path:
    return state_dir() / "tokens.json"


_ACTIVITY: dict = {"sessions": {}, "flushed": 0.0}
ACTIVITY_FLUSH_S = 3.0
ACTIVITY_KEEP_S = 600.0


def note_activity(session: str, request_class: str, account: str | None, now: float | None = None, path: Path | None = None) -> None:
    """Remember which account each session's main loop and subagents last billed, and write it to
    ``activity.json`` (at most every few seconds) for the account bar's counts."""
    if not session or not account:
        return
    now = time.time() if now is None else now
    kind = "subagent" if request_class == "subagent" else "main"
    _ACTIVITY["sessions"].setdefault(session, {})[kind] = {"account": account, "t": now}
    if now - _ACTIVITY["flushed"] < ACTIVITY_FLUSH_S:
        return
    _ACTIVITY["flushed"] = now
    live = {}
    for sid, kinds in _ACTIVITY["sessions"].items():
        kept = {k: v for k, v in kinds.items() if now - v["t"] <= ACTIVITY_KEEP_S}
        if kept:
            live[sid] = kept
    _ACTIVITY["sessions"] = live
    try:
        target = path or state_dir() / "activity.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"updated": now, "sessions": live}), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


_TOKENS: dict = {"mtime": None, "data": {}}


def stored_token(email: str, now_ms: float | None = None, path: Path | None = None) -> str | None:
    """An unexpired access token for a stored account that is not the active one, as published by
    cswap (``tokens.json``). None when unknown or expired: the app's own login then stays."""
    path = path or tokens_path()
    try:
        mtime = path.stat().st_mtime_ns
        if _TOKENS["mtime"] != mtime:
            data = json.loads(path.read_text(encoding="utf-8")).get("accounts") or {}
            _TOKENS.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    except (OSError, ValueError, AttributeError):
        return None
    entry = _TOKENS["data"].get(email) or {}
    token, expires_at = entry.get("accessToken"), entry.get("expiresAt")
    if not isinstance(token, str) or not token:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if isinstance(expires_at, (int, float)) and expires_at <= now_ms + 30_000:
        return None
    return token


def token_for(email: str | None, active: str | None) -> str | None:
    if not email:
        return None
    return active_token() if email == active else stored_token(email)


def swap_for_session(host: str, path: str, headers) -> tuple[str | None, str | None]:
    """The per-session counterpart of :func:`rewrite`. Returns ``(billed, wanted)``: the account whose
    token now stands on the request (None when the app's own login stays) and the account the session
    is on."""
    if host != API_HOST or not path.startswith(REWRITE_PREFIX):
        return None, None
    if not headers.get("authorization", "").lower().startswith("bearer "):
        return None, None
    active = active_email()
    wanted = account_for(headers.get(SESSION_HEADER, ""), headers.get(CLASS_HEADER, ""), active)
    token = token_for(wanted, active)
    if token is None:
        return None, wanted
    if headers["authorization"] != f"Bearer {token}":
        headers["authorization"] = f"Bearer {token}"
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} cswap: bearer swapped to {wanted} (token {hashlib.sha256(token.encode()).hexdigest()[:8]})", flush=True)
    return wanted, wanted


def rewrite(host: str, path: str, headers) -> bool:
    """Swap the bearer on an inference request. True when it changed."""
    if host != API_HOST or not path.startswith(REWRITE_PREFIX):
        return False
    if not headers.get("authorization", "").lower().startswith("bearer "):
        return False
    token = active_token()
    if token is None or headers["authorization"] == f"Bearer {token}":
        return False
    headers["authorization"] = f"Bearer {token}"
    # The account and a short token hash, so each billed request and each switch is visible in the
    # log without exposing the token.
    digest = hashlib.sha256(token.encode()).hexdigest()[:8]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} cswap: bearer swapped to {active_email() or 'unknown account'} (token {digest})", flush=True)
    return True


ENTRYPOINT = "claude-desktop"


def marker_path() -> Path:
    """cswap-pin's ``inference-follows`` file, which the swap-meter band reads.

    Listing the entrypoint there is how the band knows the Code tab bills the
    active account instead of the app's own login."""
    override = os.environ.get("CSWAP_DESKTOP_FOLLOW_MARKER")
    if override:
        return Path(override)
    return Path.home() / ".claude-swap-backup" / "pin-proxy" / "inference-follows"


def publish_marker(path: Path | None = None) -> None:
    path = path or marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{ENTRYPOINT}  # written by desktop-follow while its proxy runs\n", encoding="utf-8")


def withdraw_marker(path: Path | None = None) -> None:
    (path or marker_path()).unlink(missing_ok=True)


def trace_path() -> Path:
    """While this file exists the proxy logs every request and response (no bodies, no headers)."""
    return Path.home() / ".claude-swap-backup" / "desktop-follow" / "trace"


def _trace(text: str) -> None:
    if trace_path().exists():
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} trace {text}", flush=True)


def shape_path() -> Path:
    """While this file exists the proxy logs the SHAPE of each model request: header names, body
    keys, the first words of the system prompt blocks, counts. For working out what tells a
    session or a subagent apart. Ids are masked; message text and credentials are never logged."""
    return Path.home() / ".claude-swap-backup" / "desktop-follow" / "shape"


_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}")


def _mask(text: str) -> str:
    return _ID.sub("<id>", text)


def _session_tag(user_id) -> str | None:
    """A short hash of the session id in ``metadata.user_id`` (a JSON string), to tell sessions
    apart in the log without logging the id."""
    try:
        sid = json.loads(user_id).get("session_id") if isinstance(user_id, str) else None
    except ValueError:
        return None
    return hashlib.sha1(sid.encode()).hexdigest()[:6] if isinstance(sid, str) and sid else None


def _shape(flow) -> None:
    if not shape_path().exists() or not is_inference(flow.request.pretty_host, flow.request.path):
        return
    try:
        req = flow.request
        try:
            body = json.loads(req.get_content() or b"{}")
        except ValueError:
            body = {}
        system = body.get("system")
        blocks = [system] if isinstance(system, str) else [b.get("text", "") for b in system or [] if isinstance(b, dict)]
        tools = [t.get("name", "") for t in body.get("tools") or [] if isinstance(t, dict)]
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        messages = body.get("messages") or []
        names = sorted(h.lower() for h in req.headers.keys())
        interesting = {h: _mask(str(req.headers[h]))[:80] for h in req.headers.keys()
                       if any(k in h.lower() for k in ("session", "agent", "client-request", "x-app", "anthropic-beta", "user-agent", "request-class", "dispatch", "prompt-id"))}
        sock = getattr(flow.client_conn, "sockname", None)
        line = {
            "path": req.path.split("?")[0], "model": body.get("model"), "stream": body.get("stream"),
            "body_keys": sorted(body), "metadata": {k: _mask(str(v))[:100] for k, v in meta.items()},
            "session": _session_tag(meta.get("user_id")),
            "thread": sorted(body["thread"]) if isinstance(body.get("thread"), dict) else type(body.get("thread")).__name__,
            "agent_prompt": any("You are an agent for Claude Code" in b for b in blocks),
            "system_blocks": len(blocks), "system_heads": [_mask(b[:60]) for b in blocks[:3]],
            "tools": len(tools), "tools_hash": hashlib.sha1(",".join(sorted(tools)).encode()).hexdigest()[:8],
            "messages": len(messages), "roles": "".join(m.get("role", "?")[0] for m in messages[-6:] if isinstance(m, dict)),
            "header_names": names, "header_values": interesting,
            "listening_port": sock[1] if isinstance(sock, (tuple, list)) and len(sock) > 1 else None,
            "client_port": (flow.client_conn.peername or (None, None))[1] if getattr(flow.client_conn, "peername", None) else None,
        }
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} shape {json.dumps(line)}", flush=True)
    except Exception as exc:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} shape error {type(exc).__name__}: {exc}", flush=True)


def ledger_path() -> Path:
    """One JSON line per billed inference request: when, which account, the API's request id."""
    return Path.home() / ".claude-swap-backup" / "desktop-follow" / "requests.jsonl"


def is_inference(host: str, path: str) -> bool:
    """A billed model call: not token counting, which costs nothing."""
    return host == API_HOST and path.startswith(REWRITE_PREFIX) and not path.startswith("/v1/messages/count_tokens")


def billed_account(headers) -> str | None:
    """The email the request is billed to: the active account when its token is on the request,
    None when the app's own login went through (an expired or missing active token)."""
    token = active_token()
    if token is not None and headers.get("authorization", "") == f"Bearer {token}":
        return active_email()
    return None


def _entrypoint(user_agent: str) -> str:
    """``claude-desktop`` from ``claude-cli/2.1 (external, claude-desktop)``; '' when absent."""
    inner = user_agent.rpartition("(")[2].rstrip(")")
    return inner.split(",")[-1].strip() if "," in inner else ""


def record(flow, status: int | str) -> None:
    """Append the ledger line. Failing to write it must never fail the request."""
    req = flow.request
    if not is_inference(req.pretty_host, req.path):
        return
    try:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(req.timestamp_start)),
            "account": flow.metadata.get("cswap_account"),
            "request_id": flow.response.headers.get("request-id", "") if getattr(flow, "response", None) else "",
            "status": status,
            "entrypoint": _entrypoint(req.headers.get("user-agent", "")),
            "session": req.headers.get(SESSION_HEADER, ""),
            "class": req.headers.get(CLASS_HEADER, ""),
        }
        wanted = flow.metadata.get("cswap_wanted")
        if wanted and wanted != line["account"]:
            line["wanted"] = wanted  # the session's account had no usable token: the app's login stood
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


PING_HOST = "cswap.invalid"


def _ping(flow) -> bool:
    """Answer the health probe ``GET http://cswap.invalid/ping`` ourselves. A proxy whose addon was
    unloaded (mitmproxy reloads a script whose file changes, and drops it when the reload fails)
    still forwards every request, so it looks healthy while billing the app's login. Only a proxy
    that has this addon answers the probe."""
    if flow.request.pretty_host != PING_HOST:
        return False
    from mitmproxy import http

    flow.response = http.Response.make(200, b"cswap-ok", {"content-type": "text/plain"})
    return True


class FollowActive:
    published = False

    def running(self) -> None:
        publish_marker()
        self.published = True

    def done(self) -> None:
        # Only the instance that published the marker takes it away: a second proxy that fails to
        # bind the port exits through here and must not remove the running one's marker.
        if self.published:
            withdraw_marker()

    def request(self, flow) -> None:
        if _ping(flow):
            return
        req = flow.request
        if is_inference(req.pretty_host, req.path) and per_session_enabled() and req.headers.get(SESSION_HEADER):
            billed, wanted = swap_for_session(req.pretty_host, req.path, req.headers)
            note_activity(req.headers.get(SESSION_HEADER, ""), req.headers.get(CLASS_HEADER, ""), wanted)
            flow.metadata["cswap_account"] = billed
            flow.metadata["cswap_wanted"] = wanted
            if billed:
                flow.comment = f"cswap: billed to {billed}"
        else:
            if rewrite(req.pretty_host, req.path, req.headers):
                flow.comment = "cswap: billed to active account"
            if is_inference(req.pretty_host, req.path):
                flow.metadata["cswap_account"] = billed_account(req.headers)
        _shape(flow)
        _trace(f"> {flow.request.method} {flow.request.pretty_host}{flow.request.path.split('?')[0][:90]}")

    def responseheaders(self, flow) -> None:
        # Only requests are ever changed, so pass every response through as it arrives. Buffered, a
        # long-lived stream (Remote Control's /worker/events/stream) never answers the client.
        flow.response.stream = True
        # The headers carry the status and request id; the body (still arriving) is not needed.
        record(flow, flow.response.status_code)

    def response(self, flow) -> None:
        r = flow.response
        _trace(f"< {r.status_code} {flow.request.pretty_host}{flow.request.path.split('?')[0][:90]} {r.headers.get('content-type', '-')[:30]}")

    def error(self, flow) -> None:
        if not getattr(flow, "response", None):
            record(flow, "error")
        _trace(f"! {flow.request.pretty_host}{flow.request.path.split('?')[0][:90]} {flow.error}")

    def websocket_start(self, flow) -> None:
        _trace(f"ws open {flow.request.pretty_host}{flow.request.path.split('?')[0][:90]}")

    def websocket_message(self, flow) -> None:
        m = flow.websocket.messages[-1]
        _trace(f"ws {'from client' if m.from_client else 'from server'} {len(m.content)} bytes {flow.request.pretty_host}")

    def websocket_end(self, flow) -> None:
        _trace(f"ws closed {flow.request.pretty_host}")


addons = [FollowActive()]
