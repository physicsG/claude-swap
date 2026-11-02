import importlib.util
import json
import re
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "desktop_follow_addon",
    Path(__file__).parent.parent / "src" / "claude_swap" / "desktop_follow_addon.py",
)
follow_active = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(follow_active)
_REAL_SEQUENCE = follow_active.sequence_email


@pytest.fixture
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    follow_active._CACHE.update(mtime=None, token=None)

    def write(token="active-tok", expires_at=9_999_999_999_999):
        (tmp_path / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires_at}})
        )

    return write


def test_rewrites_inference_bearer(creds):
    creds()
    headers = {"authorization": "Bearer app-tok"}
    assert follow_active.rewrite("api.anthropic.com", "/v1/messages?beta=1", headers)
    assert headers["authorization"] == "Bearer active-tok"


def test_log_line_has_a_timestamp_the_account_and_no_token(creds, tmp_path, capsys):
    creds(token="secret-tok")
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}))
    follow_active.rewrite("api.anthropic.com", "/v1/messages", {"authorization": "Bearer app-tok"})
    line = capsys.readouterr().out.strip()
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d cswap: bearer swapped to me@example\.com \(token [0-9a-f]{8}\)$", line)
    assert "secret-tok" not in line


def test_log_line_names_an_unreadable_account(creds, capsys):
    creds()
    follow_active._EMAIL.update(mtime=None, email=None)
    follow_active.rewrite("api.anthropic.com", "/v1/messages", {"authorization": "Bearer app-tok"})
    assert "unknown account" in capsys.readouterr().out


def _flow(path="/v1/messages", host="api.anthropic.com", request_id="req_1", ua="claude-cli/2.1 (external, claude-desktop)"):
    class Headers(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)

    req = type("Req", (), {"pretty_host": host, "path": path, "method": "POST", "timestamp_start": 1_793_600_000.0,
                           "headers": Headers({"user-agent": ua, "authorization": "Bearer active-tok"})})()
    resp = type("Resp", (), {"headers": Headers({"request-id": request_id}), "status_code": 200})()
    return type("Flow", (), {"request": req, "response": resp, "metadata": {}})()


def test_ledger_line_names_account_request_id_and_entrypoint(creds, tmp_path, monkeypatch):
    creds(token="active-tok")
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}))
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    flow = _flow()
    follow_active.FollowActive().request(flow)
    follow_active.FollowActive().responseheaders(flow)
    line = json.loads(ledger.read_text().splitlines()[0])
    assert line["account"] == "me@example.com" and line["request_id"] == "req_1"
    assert line["status"] == 200 and line["entrypoint"] == "claude-desktop"
    assert line["ts"].endswith("Z")


def test_ledger_has_no_account_when_the_apps_own_login_went_through(creds, tmp_path, monkeypatch):
    creds(token="other-token-than-on-the-request", expires_at=1)  # expired: not substituted
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    flow = _flow()
    flow.request.headers["authorization"] = "Bearer app-tok"
    follow_active.FollowActive().request(flow)
    follow_active.FollowActive().responseheaders(flow)
    assert json.loads(ledger.read_text().splitlines()[0])["account"] is None


def test_ledger_skips_token_counting_and_other_hosts(creds, tmp_path, monkeypatch):
    creds()
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    for flow in (_flow(path="/v1/messages/count_tokens"), _flow(host="example.com"), _flow(path="/v1/code/sessions/x")):
        follow_active.FollowActive().request(flow)
        follow_active.FollowActive().responseheaders(flow)
    assert not ledger.exists()


def test_the_health_probe_is_answered_by_the_addon_itself(monkeypatch):
    import sys
    import types

    made = {}
    fake_http = types.SimpleNamespace(Response=types.SimpleNamespace(make=lambda status, body, headers: made.update(status=status, body=body) or "resp"))
    monkeypatch.setitem(sys.modules, "mitmproxy", types.SimpleNamespace(http=fake_http))
    monkeypatch.setitem(sys.modules, "mitmproxy.http", fake_http)
    flow = _flow(host="cswap.invalid", path="/ping")
    follow_active.FollowActive().request(flow)
    assert flow.response == "resp" and made == {"status": 200, "body": b"cswap-ok"}


def test_responses_are_streamed_not_buffered():
    # A buffered long-lived stream (Remote Control's event stream) never reaches the client.
    flow = _flow(host="example.com")
    flow.response.stream = False
    follow_active.FollowActive().responseheaders(flow)
    assert flow.response.stream is True


def test_leaves_other_routes_and_hosts(creds):
    creds()
    for host, path in [("api.anthropic.com", "/api/oauth/usage"), ("example.com", "/v1/messages")]:
        headers = {"authorization": "Bearer app-tok"}
        assert not follow_active.rewrite(host, path, headers)
        assert headers["authorization"] == "Bearer app-tok"


def test_expired_or_missing_token_keeps_app_login(creds, tmp_path):
    headers = {"authorization": "Bearer app-tok"}
    assert not follow_active.rewrite("api.anthropic.com", "/v1/messages", headers)
    creds(expires_at=1)
    assert not follow_active.rewrite("api.anthropic.com", "/v1/messages", headers)
    assert headers["authorization"] == "Bearer app-tok"


def test_marker_lists_the_desktop_entrypoint_while_running(tmp_path):
    marker = tmp_path / "pin-proxy" / "inference-follows"
    follow_active.publish_marker(marker)
    listed = [line.split("#")[0].strip() for line in marker.read_text().splitlines()]
    assert "claude-desktop" in listed
    follow_active.withdraw_marker(marker)
    assert not marker.exists()
    follow_active.withdraw_marker(marker)  # already gone: no error


def test_picks_up_a_switch(creds):
    creds("first")
    assert follow_active.active_token() == "first"
    creds("second")
    assert follow_active.active_token() == "second"

def test_the_account_comes_from_cswaps_record_not_the_app_config(creds, tmp_path, monkeypatch):
    # The app rewrites ~/.claude.json with its own login; the live token still belongs to the
    # account cswap switched to.
    creds()
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "app@example.com"}}))
    sequence = tmp_path / "sequence.json"
    sequence.write_text(json.dumps({"activeAccountNumber": 2, "accounts": {"1": {"email": "app@example.com"}, "2": {"email": "cswap@example.com"}}}))
    monkeypatch.setattr(follow_active, "sequence_email", lambda path=None: _REAL_SEQUENCE(sequence))
    assert follow_active.active_email() == "cswap@example.com"


def test_a_proxy_that_never_published_the_marker_does_not_remove_it(tmp_path, monkeypatch):
    marker = tmp_path / "inference-follows"
    monkeypatch.setenv("CSWAP_DESKTOP_FOLLOW_MARKER", str(marker))
    running = follow_active.FollowActive()
    running.running()
    assert marker.exists()
    follow_active.FollowActive().done()  # a second proxy that failed to bind the port
    assert marker.exists()
    running.done()
    assert not marker.exists()


def test_shape_logging_is_off_unless_asked_and_never_logs_message_text(creds, tmp_path, monkeypatch, capsys):
    shape = tmp_path / "shape"
    monkeypatch.setattr(follow_active, "shape_path", lambda: shape)
    body = json.dumps({"model": "m", "system": [{"text": "You are an agent. cwd=C:/secret"}], "messages": [{"role": "user", "content": "private words"}],
                       "tools": [{"name": "Bash"}], "metadata": {"user_id": "user_0123456789abcdef0123_session_12345678-1234-1234-1234-123456789abc"}}).encode()
    flow = _flow()
    flow.request.get_content = lambda: body
    flow.client_conn = type("C", (), {"sockname": ("127.0.0.1", 8899), "peername": ("127.0.0.1", 5555)})()
    follow_active._shape(flow)
    assert capsys.readouterr().out == ""  # off by default
    shape.write_text("")
    follow_active._shape(flow)
    out = capsys.readouterr().out
    assert "shape " in out and "<id>" in out and '"listening_port": 8899' in out
    assert "private words" not in out and "012345678" not in out and "Bearer" not in out


# --- an account per session ---------------------------------------------------------------------

def _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a"):
    """Per-session mode on, with cswap's record naming ``active`` and its token as the live one."""
    from pathlib import Path

    root = Path.home() / ".claude-swap-backup"
    (root / "desktop-follow").mkdir(parents=True, exist_ok=True)
    (root / "desktop-follow" / "state.json").write_text(json.dumps({"per_session": True}))
    follow_active._STATE.update(mtime=None, per_session=False)
    follow_active._SESSIONS.update(mtime=None, data={})
    follow_active._TOKENS.update(mtime=None, data={})
    follow_active._SEEN_WRITTEN.clear()
    follow_active._CACHE.update(mtime=None, token=None)
    set_active(active, live)


def set_active(email, token):
    from pathlib import Path
    import os

    (Path.home() / ".claude-swap-backup" / "sequence.json").write_text(
        json.dumps({"activeAccountNumber": 1, "accounts": {"1": {"email": email}}}))
    base = Path(os.environ["CLAUDE_CONFIG_DIR"])
    (base / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": 9_999_999_999_999}}))
    follow_active._CACHE.update(mtime=None, token=None)


def publish_tokens(**tokens):
    from pathlib import Path

    (Path.home() / ".claude-swap-backup" / "desktop-follow" / "tokens.json").write_text(json.dumps(
        {"accounts": {email: {"accessToken": tok, "expiresAt": 9_999_999_999_999} for email, tok in tokens.items()}}))
    follow_active._TOKENS.update(mtime=None, data={})


def _request(session, klass="main", token="app-tok"):
    flow = _flow()
    flow.request.headers["authorization"] = f"Bearer {token}"
    flow.request.headers["x-claude-code-session-id"] = session
    flow.request.headers["x-claude-code-request-class"] = klass
    follow_active.FollowActive().request(flow)
    return flow


def test_a_new_session_is_pinned_to_the_active_account_and_a_switch_leaves_it_alone(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a")
    first = _request("s1")
    assert first.request.headers["authorization"] == "Bearer tok-a" and first.metadata["cswap_account"] == "a@x.com"
    set_active("b@x.com", "tok-b")  # cswap switch
    publish_tokens(**{"a@x.com": "tok-a2"})  # cswap publishes the account it left
    assert _request("s1").request.headers["authorization"] == "Bearer tok-a2"  # s1 stays on a
    later = _request("s2")  # a session started after the switch
    assert later.request.headers["authorization"] == "Bearer tok-b" and later.metadata["cswap_account"] == "b@x.com"


def test_a_pinned_account_without_a_usable_token_keeps_the_apps_login_and_says_so(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a")
    _request("s1")
    set_active("b@x.com", "tok-b")  # nothing published for a
    flow = _request("s1")
    assert flow.request.headers["authorization"] == "Bearer app-tok"
    assert flow.metadata["cswap_account"] is None and flow.metadata["cswap_wanted"] == "a@x.com"


def test_an_expired_published_token_is_not_used(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path)
    _request("s1")
    set_active("b@x.com", "tok-b")
    publish_tokens(**{"a@x.com": "tok-a2"})
    assert follow_active.stored_token("a@x.com", now_ms=10_000_000_000_000) is None


def test_subagent_requests_use_the_sessions_subagent_account(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a")
    _request("s1")
    sessions = follow_active.load_sessions()
    follow_active.save_sessions({**sessions, "s1": {**sessions["s1"], "subagents": "b@x.com"}})
    publish_tokens(**{"b@x.com": "tok-b"})
    assert _request("s1", "subagent").request.headers["authorization"] == "Bearer tok-b"
    assert _request("s1", "main").request.headers["authorization"] == "Bearer tok-a"


def test_the_ledger_has_session_class_and_the_wanted_account(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a")
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    _request("s1")
    set_active("b@x.com", "tok-b")
    flow = _request("s1", "subagent")
    follow_active.FollowActive().responseheaders(flow)
    line = json.loads(ledger.read_text().splitlines()[-1])
    assert line["session"] == "s1" and line["class"] == "subagent" and line["account"] is None and line["wanted"] == "a@x.com"


def test_with_per_session_off_every_session_follows_the_active_account(creds, tmp_path):
    from pathlib import Path

    creds(token="active-tok")
    follow_active._STATE.update(mtime=None, per_session=False)
    (Path.home() / ".claude-swap-backup" / "desktop-follow").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".claude-swap-backup" / "desktop-follow" / "state.json").write_text(json.dumps({"enabled": True}))
    flow = _request("s1")
    assert flow.request.headers["authorization"] == "Bearer active-tok"
    assert not (Path.home() / ".claude-swap-backup" / "desktop-follow" / "sessions.json").exists()


def test_activity_records_the_last_account_of_each_session_and_class(tmp_path):
    follow_active._ACTIVITY.update(sessions={}, flushed=0.0)
    out = tmp_path / "activity.json"
    follow_active.note_activity("s1", "main", "a@x.com", now=1000.0, path=out)
    follow_active.note_activity("s1", "subagent", "b@x.com", now=1001.0, path=out)  # inside the flush gap
    assert json.loads(out.read_text())["sessions"] == {"s1": {"main": {"account": "a@x.com", "t": 1000.0}}}
    follow_active.note_activity("s2", "main", "b@x.com", now=1010.0, path=out)
    data = json.loads(out.read_text())["sessions"]
    assert data["s1"]["subagent"]["account"] == "b@x.com" and data["s2"]["main"]["account"] == "b@x.com"


def test_activity_forgets_sessions_idle_for_ten_minutes(tmp_path):
    follow_active._ACTIVITY.update(sessions={}, flushed=0.0)
    out = tmp_path / "activity.json"
    follow_active.note_activity("old", "main", "a@x.com", now=1000.0, path=out)
    follow_active.note_activity("new", "main", "a@x.com", now=2000.0, path=out)
    assert set(json.loads(out.read_text())["sessions"]) == {"new"}


def test_a_request_in_per_session_mode_feeds_the_activity_file(creds, monkeypatch, tmp_path):
    creds()
    _per_session(monkeypatch, tmp_path, active="a@x.com", live="tok-a")
    follow_active._ACTIVITY.update(sessions={}, flushed=0.0)
    _request("s1")
    from pathlib import Path

    written = json.loads((Path.home() / ".claude-swap-backup" / "desktop-follow" / "activity.json").read_text())
    assert written["sessions"]["s1"]["main"]["account"] == "a@x.com"
