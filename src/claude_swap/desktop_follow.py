"""Make Claude Desktop's Code tab bill the active account, on Windows.

The Code tab runs its CLI with the app's own login, so ``cswap switch`` never
reaches it. cswap-pin's proxy re-bills it on macOS and Linux but needs POSIX
file locks and FIFOs. This is the Windows route: a mitmproxy running the addon
in :mod:`claude_swap.desktop_follow_addon`, which swaps the bearer on
``/v1/messages`` for the active account's token.

``on`` does what ``cswap pin`` does: it starts the proxy, writes ``HTTPS_PROXY``
and ``NODE_EXTRA_CA_CERTS`` into the ``env`` block of Claude Code's global
config, and arranges for the proxy to start at logon. ``off`` undoes exactly
what ``on`` wrote. ``--ensure`` and ``--heal`` are for the logon task: they
restart a dead proxy, and when that fails they take the variables out again so
sessions fall back to a direct connection instead of failing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from claude_swap import paths
from claude_swap.claude_locks import claude_config_lock
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_text

DEFAULT_PORT = 8899
TASK_NAME = "cswap-desktop-follow"
ENTRYPOINT = "claude-desktop"
LISTEN_HOST = "127.0.0.1"
_STARTUP_WAIT_S = 30.0

# Failed restarts in a row (one a minute) before the wiring is given up.
_GIVE_UP_AFTER = 5

# Where mitmproxy's CA lives, relative to the state directory.
_CONFDIR = "mitmproxy"
_CA_FILE = "mitmproxy-ca-cert.pem"


def state_dir() -> Path:
    return paths.get_backup_root() / "desktop-follow"


def _state_file() -> Path:
    return state_dir() / "state.json"


def marker_path() -> Path:
    """The ``inference-follows`` file cswap-pin keeps and the swap-meter band reads."""
    return paths.get_backup_root() / "pin-proxy" / "inference-follows"


def ca_path() -> Path:
    return state_dir() / _CONFDIR / _CA_FILE


def addon_path() -> Path:
    return Path(__file__).with_name("desktop_follow_addon.py")


def proxy_url(port: int) -> str:
    return f"http://{LISTEN_HOST}:{port}"


def wanted_env(port: int) -> dict[str, str]:
    return {"HTTPS_PROXY": proxy_url(port), "NODE_EXTRA_CA_CERTS": str(ca_path())}


def load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(_state_file(), json.dumps(state, indent=2) + "\n")


def proxy_responsive(port: int, timeout: float = 3.0) -> bool:
    """The proxy answers, and our addon is the one answering. A hung proxy still accepts connections
    (the kernel queues them), and a proxy that lost its addon still forwards every request, so
    neither a connect nor an ordinary request proves it works: ask the addon's own probe."""
    try:
        with socket.create_connection((LISTEN_HOST, port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(b"GET http://cswap.invalid/ping HTTP/1.1\r\nHost: cswap.invalid\r\nConnection: close\r\n\r\n")
            reply = b""
            while len(reply) < 512:
                chunk = conn.recv(512)
                if not chunk:
                    break
                reply += chunk
                if b"cswap-ok" in reply:
                    break
            return reply.startswith(b"HTTP/") and b"cswap-ok" in reply
    except OSError:
        return False


def proxy_alive(port: int) -> bool:
    try:
        with socket.create_connection((LISTEN_HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


# --- the env block of Claude Code's global config -------------------------

def apply_env(config: dict, wanted: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Add ``wanted`` to ``config['env']``; return the config and what was written.

    A variable the user already set to something else is left alone and not
    recorded, so ``off`` never removes a value that was not ours.
    """
    env = config.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    wrote: dict[str, str] = {}
    for key, value in wanted.items():
        current = env.get(key)
        if current is None or current == value:
            env[key] = value
            wrote[key] = value
    config = dict(config)
    config["env"] = env
    return config, wrote


def remove_env(config: dict, wrote: dict[str, str]) -> dict:
    """Remove from ``config['env']`` the entries still holding what we wrote."""
    env = config.get("env")
    if not isinstance(env, dict):
        return config
    env = {k: v for k, v in env.items() if not (k in wrote and v == wrote[k])}
    config = dict(config)
    if env:
        config["env"] = env
    else:
        config.pop("env", None)
    return config


def _edit_global_config(edit) -> None:
    path = paths.get_global_config_path()
    with claude_config_lock(timeout=10):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = {}
        except ValueError as e:
            raise ClaudeSwitchError(f"{path} is not valid JSON; not touching it: {e}")
        if not isinstance(config, dict):
            raise ClaudeSwitchError(f"{path} is not a JSON object; not touching it")
        atomic_write_text(path, json.dumps(edit(config), indent=2) + "\n")


# --- the proxy process ----------------------------------------------------

def find_mitmdump() -> str:
    found = shutil.which("mitmdump")
    if not found:
        raise ClaudeSwitchError(
            "mitmdump is not installed. Install it with: uv tool install mitmproxy"
        )
    return found


def addon_sha(path: Path | None = None) -> str:
    import hashlib

    try:
        return hashlib.sha256((path or addon_path()).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def stage_addon() -> Path:
    """Copy the addon next to the state and run the proxy from the copy. mitmproxy watches the script
    it was given and reloads it when the file changes; reinstalling cswap rewrites the installed file,
    and a reload that fails leaves the proxy running with no addon, forwarding everything unchanged."""
    state_dir().mkdir(parents=True, exist_ok=True)
    staged = state_dir() / "addon_running.py"
    shutil.copyfile(addon_path(), staged)
    return staged


def proxy_command(mitmdump: str, port: int, addon: Path | None = None) -> list[str]:
    return [
        mitmdump,
        "--listen-host", LISTEN_HOST,
        "--listen-port", str(port),
        "--set", f"confdir={state_dir() / _CONFDIR}",
        "-s", str(addon or addon_path()),
        "-q",
    ]


def start_proxy(port: int) -> None:
    """Start the proxy detached and wait until it listens and has made its CA."""
    if proxy_alive(port):
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    flags = 0
    extra: dict = {}
    if sys.platform == "win32":
        # Not DETACHED_PROCESS: it makes Windows ignore CREATE_NO_WINDOW, and
        # the mitmdump launcher then starts python.exe in a visible console of
        # its own. A hidden console that the children inherit keeps them hidden.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        extra["startupinfo"] = startup
    staged = stage_addon()
    with open(state_dir() / "proxy.log", "ab") as log:
        proc = subprocess.Popen(
            proxy_command(find_mitmdump(), port, staged),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "CSWAP_DESKTOP_FOLLOW_MARKER": str(marker_path())},
            creationflags=flags,
            close_fds=True,
            **extra,
        )
    state = load_state()
    state["pid"] = proc.pid
    state["addon_sha"] = addon_sha()
    save_state(state)
    deadline = time.monotonic() + _STARTUP_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ClaudeSwitchError(
                f"the proxy exited at once (code {proc.returncode}); see {state_dir() / 'proxy.log'}"
            )
        if proxy_alive(port) and ca_path().exists():
            return
        time.sleep(0.25)
    raise ClaudeSwitchError(
        f"the proxy did not start listening on port {port}; see {state_dir() / 'proxy.log'}"
    )


def stop_proxy() -> None:
    """Kill the recorded proxy, and any other proxy of ours still on the port: the recorded pid goes
    stale (a start that timed out, a restart by another run), and a proxy left alive keeps the port
    and makes the next start return at once, so a hung or addon-less proxy would never be replaced."""
    if sys.platform == "win32":
        pid = load_state().get("pid")
        quiet = {"capture_output": True, "check": False, "creationflags": subprocess.CREATE_NO_WINDOW}
        if isinstance(pid, int) and pid > 0:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **quiet)
        port = load_state().get("port") or DEFAULT_PORT
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mitmdump*' "
             f"-and $_.CommandLine -like '*--listen-port {int(port)} *' -and $_.CommandLine -like '*desktop-follow*' }} "
             "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            timeout=30, **quiet,
        )
    marker_path().unlink(missing_ok=True)


# --- the logon task -------------------------------------------------------

def task_action() -> str:
    """The command the task runs: pythonw, so no console window flashes each minute."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    python = windowless if windowless.exists() else exe
    return f'"{python}" -m claude_swap desktop-follow --ensure'


def install_task() -> None:
    if sys.platform != "win32":
        return
    # Every minute, like the macOS LaunchAgent: it starts the proxy after logon
    # and brings it back within a minute if it dies.
    result = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", "1",
         "/TR", task_action()],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ClaudeSwitchError(f"could not create the scheduled task: {result.stderr.strip() or result.stdout.strip()}")


def remove_task() -> None:
    if sys.platform != "win32":
        return
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, check=False)


# --- commands -------------------------------------------------------------

def turn_on(port: int = DEFAULT_PORT) -> None:
    start_proxy(port)
    wanted = wanted_env(port)
    wrote: dict[str, str] = {}

    def edit(config: dict) -> dict:
        nonlocal wrote
        config, wrote = apply_env(config, wanted)
        return config

    _edit_global_config(edit)
    state = load_state()
    state.update(enabled=True, port=port, wrote=wrote)
    save_state(state)
    install_task()


def turn_off() -> None:
    state = load_state()
    remove_task()
    stop_proxy()
    wrote = state.get("wrote") if isinstance(state.get("wrote"), dict) else {}
    if wrote:
        _edit_global_config(lambda config: remove_env(config, wrote))
    save_state({"enabled": False})


def _ensure_marker() -> None:
    """Put the band's marker back if it went missing while the proxy runs."""
    try:
        path = marker_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{ENTRYPOINT}  # written by desktop-follow while its proxy runs\n", encoding="utf-8")
    except OSError:
        pass


# --- an account per session -----------------------------------------------

SESSION_MAX_IDLE_S = 7 * 86400


def sessions_path() -> Path:
    return state_dir() / "sessions.json"


def tokens_path() -> Path:
    return state_dir() / "tokens.json"


def read_sessions() -> dict:
    try:
        data = json.loads(sessions_path().read_text(encoding="utf-8")).get("sessions") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_sessions(sessions: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(sessions_path(), json.dumps({"version": 1, "sessions": sessions}, indent=1))


def _sequence() -> dict:
    try:
        data = json.loads((paths.get_backup_root() / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_account(arg: str) -> str:
    """An account's email from a number, an email or an alias."""
    accounts = _sequence().get("accounts") or {}
    wanted = arg.strip()
    if wanted in accounts:
        return accounts[wanted]["email"]
    for record in accounts.values():
        if wanted.lower() in (str(record.get("email", "")).lower(), str(record.get("alias", "")).lower()):
            return record["email"]
    # The name the band shows: the part of the email before the "@", when only one account has it.
    named = [r["email"] for r in accounts.values() if str(r.get("email", "")).lower().split("@")[0] == wanted.lower()]
    if len(named) == 1:
        return named[0]
    raise ClaudeSwitchError(f"no account {arg!r}; see `cswap list`")


def set_per_session(on: bool) -> None:
    state = load_state()
    state["per_session"] = on
    save_state(state)


def publish_tokens() -> int:
    """Write ``tokens.json``: the access token of every stored account that is not the active one, so
    the proxy can bill a session that stays on an account after a switch. The active account's token
    is the live credential, which the proxy reads itself. cswap's own usage collection keeps the stored
    ones fresh (it refreshes under its locks); nothing is refreshed here. Returns how many were written.
    Best effort: it never raises."""
    try:
        from claude_swap import oauth
        from claude_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        data = switcher._get_sequence_data() or {}
        active = str(data.get("activeAccountNumber"))
        entries = {}
        for num, account in (data.get("accounts") or {}).items():
            if num == active:
                continue
            blob = oauth.extract_oauth_data(switcher._read_account_credentials(num, account.get("email", "")) or "") or {}
            if blob.get("accessToken"):
                entries[account["email"]] = {"accessToken": blob["accessToken"], "expiresAt": blob.get("expiresAt")}
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(tokens_path(), json.dumps({"updated": int(time.time()), "accounts": entries}, indent=1))
        return len(entries)
    except Exception:
        return 0


def publish_tokens_after_switch() -> None:
    """Called after ``cswap switch``: the account just left is now stored, and sessions pinned to it
    need its token in ``tokens.json`` at once, not at the next minute."""
    try:
        if sys.platform == "win32" and load_state().get("per_session"):
            publish_tokens()
    except Exception:
        pass


def prune_sessions() -> int:
    """Drop sessions not seen for a week. Returns how many went."""
    sessions = read_sessions()
    now = time.time()
    keep = {}
    for sid, entry in sessions.items():
        try:
            seen = time.mktime(time.strptime(entry.get("seen") or entry.get("pinned"), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except (TypeError, ValueError):
            seen = now
        if now - seen <= SESSION_MAX_IDLE_S:
            keep[sid] = entry
    if len(keep) != len(sessions):
        write_sessions(keep)
    return len(sessions) - len(keep)


def pin_session(session: str, account: str | None, *, subagents: bool = False) -> str:
    """Put ``session`` on ``account`` (its subagents only, when ``subagents``); ``None`` clears the
    subagent override. Returns the account email, or an empty string when cleared."""
    sessions = read_sessions()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = dict(sessions.get(session) or {"pinned": now})
    email = resolve_account(account) if account else ""
    if subagents:
        if "account" not in entry:
            # A session the proxy has not met yet: pin it where it is now, as its first request would.
            seq = _sequence()
            active = (seq.get("accounts") or {}).get(str(seq.get("activeAccountNumber")), {}).get("email")
            if active:
                entry["account"] = active
        if email:
            entry["subagents"] = email
        else:
            entry.pop("subagents", None)
    else:
        entry["account"] = email
    entry["seen"] = now
    sessions[session] = entry
    write_sessions(sessions)
    publish_tokens()
    return email


def _sample_usage() -> None:
    """Record cswap's newest weekly percentages for the usage report; best effort."""
    try:
        from claude_swap import desktop_usage

        desktop_usage.sample_usage()
    except Exception:
        pass


def ensure(*, port: int | None = None) -> str:
    """Bring a wanted proxy back; give the wiring up when it cannot be.

    Returns ``"off"`` (not turned on), ``"ok"``, ``"restarted"``, ``"retry"``
    (a restart failed; the next run tries again) or ``"healed"`` (the variables
    were taken out so sessions go direct). Never raises.

    The machine wakes from sleep with a dead or wedged proxy and a slow start,
    so a single failed restart is not a reason to give the wiring up: only
    ``_GIVE_UP_AFTER`` failed runs in a row are.
    """
    state = load_state()
    if not state.get("enabled"):
        return "off"
    port = port or state.get("port") or DEFAULT_PORT
    _sample_usage()
    if state.get("per_session"):
        publish_tokens()
        prune_sessions()
    if proxy_alive(port) and proxy_responsive(port) and state.get("addon_sha", addon_sha()) == addon_sha():
        _ensure_marker()
        if state.get("failures"):
            state.pop("failures")
            save_state(state)
        return "ok"
    try:
        # A proxy that no longer listens may still be running, wedged; kill it first.
        stop_proxy()
        start_proxy(port)
        state = load_state()
        state.pop("failures", None)
        save_state(state)
        return "restarted"
    except Exception:
        # start_proxy records the new pid before it waits for the port; reload so a start that
        # timed out does not write the old pid back, and --off can still find the process.
        state = load_state()
        state["failures"] = int(state.get("failures") or 0) + 1
        if state["failures"] < _GIVE_UP_AFTER:
            save_state(state)
            return "retry"
        wrote = state.get("wrote") if isinstance(state.get("wrote"), dict) else {}
        try:
            if wrote:
                _edit_global_config(lambda config: remove_env(config, wrote))
        except Exception:
            pass
        marker_path().unlink(missing_ok=True)
        state.update(enabled=False, wrote={})
        state.pop("failures", None)
        save_state(state)
        return "healed"


def state_word() -> tuple[str, str]:
    """``(OK|NOT-OK|OFF, detail)``."""
    state = load_state()
    if not state.get("enabled"):
        return "OFF", "desktop-follow is not turned on"
    port = state.get("port") or DEFAULT_PORT
    if not proxy_alive(port):
        return "NOT-OK", f"nothing is listening on {LISTEN_HOST}:{port}"
    if not proxy_responsive(port):
        return "NOT-OK", f"the proxy on {LISTEN_HOST}:{port} accepts connections but does not answer"
    try:
        config = json.loads(paths.get_global_config_path().read_text(encoding="utf-8"))
        env = config.get("env") or {}
    except (OSError, ValueError):
        env = {}
    missing = [k for k, v in (state.get("wrote") or {}).items() if env.get(k) != v]
    if missing:
        return "NOT-OK", f"{', '.join(missing)} missing from the global config env"
    return "OK", f"proxy on {LISTEN_HOST}:{port}, variables set"


def _run_sessions(args) -> int:
    if args.per_session:
        set_per_session(args.per_session == "on")
        if args.per_session == "on":
            publish_tokens()
            print("Each Code tab session now keeps the account it started on. A session you have not seen yet "
                  "is pinned to the active account when it first speaks.")
        else:
            print("Every Code tab session follows the active account again.")
        return 0
    if args.sessions:
        sessions = read_sessions()
        if not sessions:
            print("No sessions yet." + ("" if load_state().get("per_session") else " (per-session mode is off: cswap desktop-follow --per-session on)"))
            return 0
        for sid, entry in sorted(sessions.items(), key=lambda kv: kv[1].get("seen", ""), reverse=True):
            extra = f", subagents -> {entry['subagents']}" if entry.get("subagents") else ""
            print(f"{sid}  {entry.get('account', '?')}{extra}  (seen {entry.get('seen', '?')})")
        return 0
    if args.pin:
        email = pin_session(args.pin[0], args.pin[1])
        print(f"Session {args.pin[0]} now bills {email}. Its next request pays a prompt-cache rebuild.")
        return 0
    if args.subagents:
        target = None if args.subagents[1].lower() == "off" else args.subagents[1]
        email = pin_session(args.subagents[0], target, subagents=True)
        print(f"Subagents of session {args.subagents[0]} " + (f"now bill {email}." if email else "follow the session again."))
        return 0
    sessions = read_sessions()
    if sessions.pop(args.unpin, None) is None:
        print(f"No session {args.unpin}.", file=sys.stderr)
        return 1
    write_sessions(sessions)
    print(f"Forgot session {args.unpin}.")
    return 0


def run(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="cswap desktop-follow",
        description=(
            "Bill Claude Desktop's Code tab to the active account (Windows). "
            "Needs mitmproxy: uv tool install mitmproxy"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="Start the proxy, set the variables, start it at logon")
    group.add_argument("--off", action="store_true", help="Undo --on")
    group.add_argument("--ensure", action="store_true", help="Silently restart a dead proxy; for the logon task")
    group.add_argument("--heal", action="store_true", help="Like --ensure, and say what was done")
    group.add_argument("--state", action="store_true", help="Print OK, NOT-OK or OFF, detail on stderr")
    parser.add_argument("--port", type=int, default=None, help=f"Proxy port (default {DEFAULT_PORT})")
    group.add_argument("--per-session", choices=("on", "off"), help="on: each Code tab session keeps the account it started on; off: all follow the active account")
    group.add_argument("--sessions", action="store_true", help="List the sessions and their accounts")
    group.add_argument("--pin", nargs=2, metavar=("SESSION", "ACCOUNT"), help="Put a session on an account (number, email or alias)")
    group.add_argument("--subagents", nargs=2, metavar=("SESSION", "ACCOUNT|off"), help="Send a session's subagents to an account, or off")
    group.add_argument("--unpin", metavar="SESSION", help="Forget a session; it is pinned again to the active account when it next speaks")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print(
            "desktop-follow is the Windows route. On macOS and Linux use `cswap pin`.",
            file=sys.stderr,
        )
        return 1
    if args.per_session or args.sessions or args.pin or args.subagents or args.unpin:
        try:
            return _run_sessions(args)
        except ClaudeSwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    if args.ensure:
        ensure(port=args.port)
        return 0
    if args.heal:
        print(ensure(port=args.port))
        return 0
    if args.state:
        word, detail = state_word()
        print(word)
        print(detail, file=sys.stderr)
        return 0
    try:
        if args.off:
            turn_off()
            print("desktop-follow is off. New sessions go direct again.")
        elif args.on:
            turn_on(args.port or DEFAULT_PORT)
            print(
                "desktop-follow is on. Start a new Code session; running sessions keep their old connection."
            )
        else:
            parser.print_help()
    except ClaudeSwitchError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0
