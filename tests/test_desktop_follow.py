import json
import sys

import pytest

from claude_swap import desktop_follow as df
from claude_swap.exceptions import ClaudeSwitchError


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "backup"
    config = tmp_path / ".claude.json"
    monkeypatch.setattr(df.paths, "get_backup_root", lambda: root)
    monkeypatch.setattr(df.paths, "get_global_config_path", lambda: config)
    monkeypatch.setattr(df, "claude_config_lock", lambda **_: _NoLock())
    return root, config


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_apply_env_adds_ours_and_keeps_the_rest():
    config, wrote = df.apply_env({"theme": "dark", "env": {"A": "1"}}, {"HTTPS_PROXY": "p"})
    assert config == {"theme": "dark", "env": {"A": "1", "HTTPS_PROXY": "p"}}
    assert wrote == {"HTTPS_PROXY": "p"}


def test_apply_env_never_overwrites_a_users_own_value():
    config, wrote = df.apply_env({"env": {"HTTPS_PROXY": "corp"}}, {"HTTPS_PROXY": "p", "X": "y"})
    assert config["env"] == {"HTTPS_PROXY": "corp", "X": "y"}
    assert wrote == {"X": "y"}


def test_remove_env_takes_out_only_what_we_wrote():
    config = {"env": {"HTTPS_PROXY": "p", "NODE_EXTRA_CA_CERTS": "changed", "A": "1"}}
    out = df.remove_env(config, {"HTTPS_PROXY": "p", "NODE_EXTRA_CA_CERTS": "ca"})
    assert out["env"] == {"NODE_EXTRA_CA_CERTS": "changed", "A": "1"}


def test_remove_env_drops_an_empty_block():
    assert df.remove_env({"env": {"HTTPS_PROXY": "p"}, "x": 1}, {"HTTPS_PROXY": "p"}) == {"x": 1}


def test_ensure_does_nothing_when_off(home):
    assert df.ensure() == "off"


def test_ensure_leaves_a_live_proxy_alone(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    assert df.ensure() == "ok"


def test_ensure_restarts_a_dead_proxy(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    started = []
    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "start_proxy", lambda port: started.append(port))
    assert df.ensure() == "restarted"
    assert started == [9]


def test_ensure_gives_the_wiring_up_when_the_proxy_will_not_start(home, monkeypatch):
    root, config = home
    wrote = df.wanted_env(9)
    config.write_text(json.dumps({"env": {**wrote, "A": "1"}}))
    df.marker_path().parent.mkdir(parents=True)
    df.marker_path().write_text("claude-desktop\n")
    df.save_state({"enabled": True, "port": 9, "wrote": wrote})

    def fail(port):
        raise ClaudeSwitchError("no mitmdump")

    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "start_proxy", fail)
    # One failed restart (a slow start after sleep) keeps the wiring and tries again.
    for _ in range(df._GIVE_UP_AFTER - 1):
        assert df.ensure() == "retry"
        assert json.loads(config.read_text())["env"] == {**wrote, "A": "1"}
        assert df.load_state()["enabled"] is True
    assert df.ensure() == "healed"
    assert json.loads(config.read_text()) == {"env": {"A": "1"}}
    assert not df.marker_path().exists()
    assert df.load_state()["enabled"] is False


def test_a_start_that_times_out_keeps_the_new_pid(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9, "pid": 1})

    def slow(port):
        df.save_state({**df.load_state(), "pid": 2})
        raise ClaudeSwitchError("did not start listening")

    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "stop_proxy", lambda: None)
    monkeypatch.setattr(df, "start_proxy", slow)
    assert df.ensure() == "retry"
    assert df.load_state()["pid"] == 2


def test_ensure_forgets_earlier_failures_once_the_proxy_is_back(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9, "failures": 3})
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    assert df.ensure() == "ok"
    assert "failures" not in df.load_state()


def test_ensure_stops_a_wedged_proxy_before_starting_another(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    order = []
    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "stop_proxy", lambda: order.append("stop"))
    monkeypatch.setattr(df, "start_proxy", lambda port: order.append("start"))
    assert df.ensure() == "restarted"
    assert order == ["stop", "start"]


def test_state_word(home, monkeypatch):
    root, config = home
    assert df.state_word()[0] == "OFF"
    wrote = df.wanted_env(9)
    df.save_state({"enabled": True, "port": 9, "wrote": wrote})
    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    assert df.state_word()[0] == "NOT-OK"
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    config.write_text(json.dumps({"env": {}}))
    word, detail = df.state_word()
    assert word == "NOT-OK" and "HTTPS_PROXY" in detail
    config.write_text(json.dumps({"env": wrote}))
    assert df.state_word()[0] == "OK"


def test_proxy_listens_only_on_loopback():
    cmd = df.proxy_command("mitmdump", 8899)
    assert cmd[cmd.index("--listen-host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--listen-port") + 1] == "8899"


def test_task_runs_windowless_and_every_minute(monkeypatch):
    calls = []
    monkeypatch.setattr(df.sys, "platform", "win32")
    monkeypatch.setattr(df.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    df.install_task()
    cmd = calls[0]
    assert cmd[cmd.index("/SC") + 1] == "MINUTE" and cmd[cmd.index("/MO") + 1] == "1"
    assert "desktop-follow --ensure" in cmd[cmd.index("/TR") + 1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process flags")
def test_proxy_starts_without_a_console_window(home, monkeypatch):
    # DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW, and the mitmdump
    # launcher then opens a visible console for its python child.
    seen = {}

    class Proc:
        pid = 4242

        def poll(self):
            return None

    def popen(cmd, **kw):
        seen.update(kw)
        return Proc()

    answers = iter([False, True])
    monkeypatch.setattr(df.subprocess, "Popen", popen)
    monkeypatch.setattr(df, "find_mitmdump", lambda: "mitmdump")
    monkeypatch.setattr(df, "proxy_alive", lambda port: next(answers))
    ca = home[0] / "ca.pem"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("x")
    monkeypatch.setattr(df, "ca_path", lambda: ca)
    df.start_proxy(8899)
    flags = seen["creationflags"]
    assert flags & df.subprocess.CREATE_NO_WINDOW
    assert not flags & df.subprocess.DETACHED_PROCESS
    assert seen["startupinfo"].wShowWindow == df.subprocess.SW_HIDE


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for other platforms")
def test_other_platforms_are_pointed_at_pin(capsys):
    assert df.run(["--state"]) == 1
    assert "cswap pin" in capsys.readouterr().err


def _serve(reply):
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    conns = []

    def run():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conns.append(conn)
            if reply:
                conn.recv(1024)
                conn.sendall(reply)

    threading.Thread(target=run, daemon=True).start()
    return server, conns


def test_a_proxy_whose_addon_answers_the_probe_is_responsive():
    server, conns = _serve(b"HTTP/1.1 200 OK\r\ncontent-length: 8\r\n\r\ncswap-ok")
    try:
        assert df.proxy_responsive(server.getsockname()[1])
    finally:
        server.close()
        for c in conns:
            c.close()


def test_a_proxy_that_forwards_but_lost_its_addon_is_not_responsive():
    # Without the addon the probe is forwarded to a host that does not exist: a gateway error.
    server, conns = _serve(b"HTTP/1.1 502 Bad Gateway\r\n\r\nno such host")
    try:
        assert df.proxy_alive(server.getsockname()[1])
        assert not df.proxy_responsive(server.getsockname()[1])
    finally:
        server.close()
        for c in conns:
            c.close()


def test_the_proxy_runs_from_a_staged_copy_of_the_addon(home):
    staged = df.stage_addon()
    assert staged.parent == df.state_dir() and staged.read_bytes() == df.addon_path().read_bytes()
    assert str(staged) in df.proxy_command("mitmdump", 8899, staged)


def test_a_reinstalled_addon_restarts_the_proxy(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9, "addon_sha": "old"})
    order = []
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    monkeypatch.setattr(df, "stop_proxy", lambda: order.append("stop"))
    monkeypatch.setattr(df, "start_proxy", lambda port: order.append("start"))
    assert df.ensure() == "restarted" and order == ["stop", "start"]
    df.save_state({"enabled": True, "port": 9, "addon_sha": df.addon_sha()})
    order.clear()
    assert df.ensure() == "ok" and order == []


def test_a_proxy_that_accepts_but_never_answers_is_not_responsive():
    server, conns = _serve(None)
    try:
        port = server.getsockname()[1]
        assert df.proxy_alive(port)  # a plain connect is satisfied
        assert not df.proxy_responsive(port, timeout=0.3)
    finally:
        server.close()
        for c in conns:
            c.close()


def test_ensure_restarts_a_hung_proxy(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    order = []
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: False)
    monkeypatch.setattr(df, "stop_proxy", lambda: order.append("stop"))
    monkeypatch.setattr(df, "start_proxy", lambda port: order.append("start"))
    assert df.ensure() == "restarted"
    assert order == ["stop", "start"]


def test_state_says_not_ok_for_a_hung_proxy(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: False)
    word, detail = df.state_word()
    assert word == "NOT-OK" and "does not answer" in detail


def test_ensure_puts_the_band_marker_back(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    assert not df.marker_path().exists()
    assert df.ensure() == "ok"
    assert "claude-desktop" in df.marker_path().read_text()


# --- an account per session ---------------------------------------------------------------------

def _sequence(root, active="1"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": int(active),
        "accounts": {"1": {"email": "a@x.com", "alias": "work"}, "2": {"email": "b@x.com"}}}))


def test_account_arguments_resolve_by_number_email_and_alias(home):
    root, _ = home
    _sequence(root)
    assert df.resolve_account("2") == "b@x.com"
    assert df.resolve_account("B@X.com") == "b@x.com"
    assert df.resolve_account("work") == "a@x.com"
    assert df.resolve_account("b") == "b@x.com"  # the name before the @, as the band shows it
    with pytest.raises(ClaudeSwitchError):
        df.resolve_account("nobody")


def test_per_session_mode_is_a_state_flag(home):
    df.set_per_session(True)
    assert df.load_state()["per_session"] is True
    df.set_per_session(False)
    assert df.load_state()["per_session"] is False


def test_pin_and_unpin_a_session_and_its_subagents(home, monkeypatch):
    root, _ = home
    _sequence(root)
    monkeypatch.setattr(df, "publish_tokens", lambda: 0)
    assert df.pin_session("s1", "2") == "b@x.com"
    assert df.read_sessions()["s1"]["account"] == "b@x.com"
    assert df.pin_session("s1", "work", subagents=True) == "a@x.com"
    entry = df.read_sessions()["s1"]
    assert entry["account"] == "b@x.com" and entry["subagents"] == "a@x.com"
    assert df.pin_session("s1", None, subagents=True) == ""
    assert "subagents" not in df.read_sessions()["s1"]


def test_run_lists_pins_and_forgets_sessions(home, monkeypatch, capsys):
    root, _ = home
    _sequence(root)
    monkeypatch.setattr(df.sys, "platform", "win32")
    monkeypatch.setattr(df, "publish_tokens", lambda: 0)
    assert df.run(["--pin", "s1", "2"]) == 0
    assert df.run(["--sessions"]) == 0
    assert "s1  b@x.com" in capsys.readouterr().out
    assert df.run(["--unpin", "s1"]) == 0
    assert df.read_sessions() == {}
    assert df.run(["--unpin", "s1"]) == 1
    assert df.run(["--pin", "s1", "nobody"]) == 1


def test_old_sessions_are_pruned(home):
    root, _ = home
    df.write_sessions({"old": {"account": "a@x.com", "seen": "2020-01-01T00:00:00Z"},
                       "new": {"account": "a@x.com", "seen": df.time.strftime("%Y-%m-%dT%H:%M:%SZ", df.time.gmtime())}})
    assert df.prune_sessions() == 1
    assert set(df.read_sessions()) == {"new"}


def test_publish_tokens_writes_only_the_accounts_that_are_not_active(home, monkeypatch):
    root, _ = home
    _sequence(root, active="1")

    class Switcher:
        def _get_sequence_data(self):
            return json.loads((root / "sequence.json").read_text())

        def _read_account_credentials(self, num, email):
            return json.dumps({"claudeAiOauth": {"accessToken": f"tok-{num}", "refreshToken": "r", "expiresAt": 9_999_999_999_999}})

    monkeypatch.setattr("claude_swap.switcher.ClaudeAccountSwitcher", Switcher)
    assert df.publish_tokens() == 1
    written = json.loads(df.tokens_path().read_text())["accounts"]
    assert list(written) == ["b@x.com"] and written["b@x.com"]["accessToken"] == "tok-2"


def test_the_minute_task_publishes_tokens_only_in_per_session_mode(home, monkeypatch):
    calls = []
    monkeypatch.setattr(df, "publish_tokens", lambda: calls.append("tokens"))
    monkeypatch.setattr(df, "prune_sessions", lambda: calls.append("prune"))
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    monkeypatch.setattr(df, "proxy_responsive", lambda port, timeout=3.0: True)
    df.save_state({"enabled": True, "port": 9})
    df.ensure()
    assert calls == []
    df.save_state({"enabled": True, "port": 9, "per_session": True})
    df.ensure()
    assert calls == ["tokens", "prune"]


def test_setting_subagents_on_an_unseen_session_pins_it_where_it_is(home, monkeypatch):
    root, _ = home
    _sequence(root, active="2")
    monkeypatch.setattr(df, "publish_tokens", lambda: 0)
    df.pin_session("new", "1", subagents=True)
    assert df.read_sessions()["new"]["account"] == "b@x.com"
    assert df.read_sessions()["new"]["subagents"] == "a@x.com"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process control")
def test_stop_kills_the_recorded_pid_and_any_other_proxy_of_ours_on_the_port(home, monkeypatch):
    df.save_state({"enabled": True, "port": 8899, "pid": 4242})
    calls = []
    monkeypatch.setattr(df.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    df.stop_proxy()
    assert calls[0][:3] == ["taskkill", "/PID", "4242"]
    script = calls[1][-1]
    assert "--listen-port 8899 " in script and "desktop-follow" in script and "Stop-Process" in script
