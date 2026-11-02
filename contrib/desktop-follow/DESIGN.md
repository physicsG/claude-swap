# Desktop follow: design notes

How the Claude Desktop Code tab is made to bill the account cswap has active on
Windows, what was built around it, what went wrong on the way, and the designs
that are still open (an account per project, an account per subagent). Written
for whoever picks this up next, human or agent. Start with the status table.

| # | Design | Status | Where |
| --- | --- | --- | --- |
| 1 | Desktop-follow proxy | shipped, PR realiti4/claude-swap#530 | `src/claude_swap/desktop_follow.py`, `desktop_follow_addon.py` |
| 2 | Proxy reliability (sleep, hangs, markers) | shipped, same PR | same files |
| 3 | Request ledger and usage report | shipped, same PR | `desktop_usage.py`, `desktop_usage_html.py`, `cswap usage-report` |
| 4 | The credential decides the active account | committed, branch `fix/credential-owns-active`, no PR yet | `switcher.py` (`live_credential_owner`, `reconcile_identity`) |
| 5 | The account bar (swap-meter) | shipped, PR RomanovX/claude-mods#1 | `claude-mods/plugins/swap-meter/hooks` |
| 6 | What a switch costs (cache rebuild) | measured | section 6 |
| 7 | **An account per project** | **proposed** | section 7 |
| 8 | **An account per subagent** | **proposed, nice to have** | section 8 |

Commands you will need: `cswap desktop-follow --on|--off|--state|--heal`,
`cswap usage-report --open`. Everything lives under `~\.claude-swap-backup\desktop-follow\`
(state, proxy log, ledger, samples, report).

## 0. Why this exists

The Code tab runs its CLI with the app's own login (`CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_USER_EMAIL`, entrypoint `claude-desktop`). `cswap switch` swaps
`~/.claude/.credentials.json`, which that CLI never reads, so the tab kept billing
the app's account whatever cswap said. cswap-pin fixes this on macOS and Linux with
a local proxy, but needs `fcntl` locks and FIFOs, so it refuses Windows.
`settings.json` `env` cannot override variables the host sets, so there is no
config-only route either.

```
Code tab session (HTTPS_PROXY, NODE_EXTRA_CA_CERTS from ~/.claude.json env)
        |
        v
mitmproxy 127.0.0.1:8899  --addon-->  swap the bearer on /v1/messages
        |                              token = active account, from ~/.claude/.credentials.json
        v                              + ledger line per request
api.anthropic.com
```

## 1. The desktop-follow proxy

- `cswap desktop-follow --on` starts a detached mitmproxy on `127.0.0.1:8899` (CA under
  the state folder), writes `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` into the `env` block
  of `~/.claude.json` (only keys it owns, recorded in `state.json` as `wrote`), and
  registers the Scheduled Task `cswap-desktop-follow`, which runs `--ensure` every minute.
- The addon changes only the `Authorization` bearer on `api.anthropic.com` requests whose
  path starts with `/v1/messages`. Profile, usage, OAuth, Remote Control and Artifacts keep
  the app's own login. It never refreshes or writes a credential, and it does not
  substitute an expired token (the app's login keeps working instead).
- `inference-follows` (in `~\.claude-swap-backup\pin-proxy\`) lists `claude-desktop` while
  the proxy runs. The swap-meter band reads it to mark the right account as billed.
- Limits: the token is only refreshed when a terminal `claude` runs on the active account;
  Remote Control and Artifacts stay on the app's account; every session passes through a
  local proxy that handles tokens (loopback only).

Rejected: reusing cswap-pin (POSIX only); env vars per Code tab environment as the
*only* mechanism (works, but needs setting in every environment; kept as the way to do
per-project, section 7); patching Claude Code (not ours).

## 2. Reliability: what broke, and the fix

Each of these was observed, not guessed.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Console window stays on screen | `DETACHED_PROCESS` makes Windows ignore `CREATE_NO_WINDOW`; the mitmdump launcher opened a console for its python child | Start with `CREATE_NO_WINDOW` and a hidden `STARTUPINFO`, never `DETACHED_PROCESS` |
| desktop-follow turned itself off overnight | After sleep/resume the proxy is dead or wedged and restarts slowly; one failed 15 s start removed the variables and set `enabled: false` | Kill the old proxy first, wait 30 s, give up only after 5 failed runs in a row (`_GIVE_UP_AFTER`) |
| Phone messages never reached Claude Code | mitmproxy buffers responses until they end; Remote Control's inbound channel `GET /v1/code/sessions/<id>/worker/events/stream` never ends, so it never answered and the client reconnected forever | `responseheaders` hook sets `flow.response.stream = True` for every response |
| `--off` left a proxy running | A start that timed out wrote the stale pid back over the new proxy's pid | Reload state before saving the failure |
| Proxy "up" but every request stalls | A hung proxy still accepts connections (the kernel queues them), so a connect check passes | `proxy_responsive()` sends a request and wants an `HTTP/` reply; `--state` and `--ensure` use it |
| Band showed the wrong account as billed | The marker file went missing: a second proxy that failed to bind the port exited through `done()` and withdrew the running one's marker | Only the instance that published the marker withdraws it; `--ensure` puts it back |

Debugging aid: create the file `desktop-follow\trace` and the proxy logs method, host,
path and status of every request (no bodies, no headers). Delete it when done.

## 3. Request ledger and usage report

Question: "tokens against weekly percentage, per account, desktop only".

Data, joined at report time:

1. **Proxy ledger** `requests.jsonl`: one line per billed `/v1/messages` call (not
   `count_tokens`): time, account, API `request-id`, status, entrypoint.
2. **Claude Code transcripts** `~/.claude/projects/**/*.jsonl`: final usage per request id
   (input, output, cache read, cache write 5 m / 1 h), model, `cwd`, session. The join on
   `request-id` was checked: ledger ids appear as `requestId` in the transcripts. A request
   writes several transcript lines with the same usage; the fullest wins.
3. **Percentage samples** `usage-samples.jsonl`, taken from cswap's usage cache whenever it
   has a new reading (cswap keeps no history). The minute task does the sampling, and no
   extra call is made to the usage endpoint.

`cswap usage-report [--open] [--html PATH] [--no-html]` prints a summary and writes a
self-contained HTML page (inline SVG, light and dark, tooltips, table view): tokens per 1%
of the week, percentage over time, percentage against tokens, tokens per day by kind, by
model, by project, a Fable section, and notes.

Caveats, all stated on the page: tokens are input-equivalent (output 5x, cache read 0.1x,
cache write 1.25x or 2x), model prices are not modelled, and Anthropic does not publish
how the percentage is weighted, so tokens per 1% is an estimate; the percentage has 1 point
resolution, so it is only meaningful after several points of movement; requests the app
makes itself (entrypoint `agent-sdk/...`) use quota but leave no transcript; only requests
through the proxy are counted, from the day the ledger started.

The account in the ledger comes from cswap's `sequence.json`, not `~/.claude.json` (design 4).

## 4. The credential decides the active account

Problem: Claude Desktop rewrites `oauthAccount` in `~/.claude.json` with its own login when a
Code session starts. cswap decided "active" from that file alone, so `cswap list`, the band
and `cswap auto` named the wrong account, a `switch` to it stopped at "Already on", and the
log repeated "Live credential resolves to a different account ... foreign credential under
a stale config". The proxy was unaffected (it uses the credential), so billing was right and
the display was wrong.

Design (commit `3e9cfd7`):

- `live_credential_owner()`: the slot whose stored credential has the live credential's
  lineage fingerprint (refresh-token hash, stable across access-token rotation). If none
  match, optionally ask the profile oracle (`fetch_oauth_profile`). Two slots sharing a
  lineage prove nothing. A failed probe is not repeated for 5 minutes.
- `current_account_number()` prefers that owner when the file's own slot holds a stored
  credential that the live one is not; with nothing stored there is no evidence against the
  file.
- `reconcile_identity()` copies the owner's `oauthAccount` back into `~/.claude.json` and
  records the active slot, under cswap's locks, touching nothing else. It runs before
  `list`, `status`, `switch` and each `auto` tick, and never raises.

Verified: 2854 tests pass; a hand-made overwrite of the file was repaired by the next
`cswap list`. Not verified: that Desktop is the writer (only inferred from timing).
Rejected: a task that re-asserts the identity from outside (works, but leaves cswap itself
wrong); a `list --json` mismatch flag (unnecessary once repaired).

## 5. The account bar (swap-meter)

`claude-mods/plugins/swap-meter` draws one row per account above the prompt from
`cswap list --json` once a minute: billed dot, 5h and 7d bars with a pace tick, resets.
Click a name to run `cswap switch`. Added for desktop-follow: a line under the rows,
green "proxy running" or red/dim "proxy down/off · click to start" (a button that runs
`cswap desktop-follow --on`), amber "proxy starting..." while a restart runs, and a start
of a dead proxy on each refresh. The band reads `CLAUDE_CODE_USER_EMAIL` and the marker to
decide which account this session bills. Note: the installed plugin cache copy is patched by
hand until the PR is released, and a plugin update overwrites it.

## 6. What a switch costs

Measured on this machine: the first call after an account change in a session rebuilds the
conversation's prompt cache on the new account (caches belong to an account).

| Session | Before: cache read / write | First call after: cache read / write |
| --- | --- | --- |
| A | 83K / 0.3K | 0 / 132.6K |
| A | 307K / 0.4K | 48K / 243K |
| A | 293K / 0.04K | 48K / 248K |
| B | 307K / 0.2K | 60K / 263K |
| B | 341K / 0.8K | 60K / 286K |
| B | 416K / 0.3K | 60K / 305K |

About 50 to 60K still hits (the shared prefix); the rest of the context is written again, at
1.25x to 2x an input token, once per open session per switch. Flipping back and forth pays it
each way. Consequences for any design: never alternate accounts per request; a session (or
a whole subagent) should stay on one account for as long as it runs.

## 7. Proposed: an account per project (Code tab)

Requirement: several projects at once, each on its own account; switching one must not move
the others.

Idea: the proxy picks the account from *which proxy address the session uses*, not from one
global answer. Set it per project in the Code tab environment (the variables are per
environment; this is how the work started).

- **Label by port (preferred).** One mitmproxy process listens on several ports,
  e.g. `8899` (follow the global active account, today's behaviour), `8900` (account 1),
  `8901` (account 2). The addon reads `flow.client_conn.sockname[1]` and looks the port up in
  a table. A project's environment sets `HTTPS_PROXY=http://127.0.0.1:8901`.
- **Label by proxy login (fallback).** `HTTPS_PROXY=http://acct2@127.0.0.1:8899`; the label
  arrives as a `Proxy-Authorization` on the CONNECT request. Depends on the client sending
  credentials from the URL.
- **Tokens for accounts that are not active.** This is the hard part. Today the addon reads
  the live credential file, which holds only the active account. A pinned account needs its
  own fresh access token. cswap keeps backups encrypted and refreshes them only through its
  locked machinery (refresh tokens rotate; a second refresher strands one of them). So:
  cswap, not the proxy, refreshes, and publishes `desktop-follow\tokens.json` (mode 0600,
  per account: access token and `expiresAt`) from the minute task; the addon only reads it
  and, like today, does not substitute an expired token. Cost: tokens of inactive accounts
  sit in plaintext on disk, as the live one already does.
- **Bar:** show per session which account it bills (the band can read its own
  `HTTPS_PROXY` through `$.env`), and offer "pin this session to account N" next to the
  global switch. The usage report gains a per-project account column for free (the ledger
  already has the account and the transcript has `cwd`).
- **Unchanged:** Remote Control and Artifacts stay on the app's account. A global switch
  moves only sessions on the follow-active port.

Unverified (spikes, section 9): S1 mitmproxy exposes the listening port to the addon;
S2 Claude Code sends credentials from a proxy URL (for the fallback); S3 a Code tab
environment's `HTTPS_PROXY` wins over the global one in `~/.claude.json`.

Rejected: a different proxy process per account (more processes to keep alive; the port
route gets the same with one); choosing the account by working folder at the proxy (the
proxy cannot see the folder; the transcript knows it only afterwards); `cswap run` for the
Code tab (terminal only; it is the right tool for terminal sessions and `cswap map` already
binds a folder to an account there).

## 8. Proposed: an account per subagent (nice to have)

Requirement: when one account is nearly full, consecutive subagents should run on the
emptier one.

Subagents run inside the main Claude Code process: same environment, same connection pool,
so section 7's address trick cannot tell them apart. The proxy would have to recognise a
subagent's request itself.

- **Identify (unverified).** Candidates: the system prompt prefix and tool list in the
  request body differ for subagents; a header or the `metadata.user_id` may carry a session
  or agent id. Nothing is confirmed (S4).
- **Pin per subagent, never per request.** A subagent's conversation gets a key (hash of its
  system prompt and first user message). The first request with an unseen key picks an
  account by the rule; later requests with that key follow the pin (kept in memory with a
  file copy and a TTL). A subagent's context is small, so its own cache rebuild is cheap,
  unlike the main session's 260 to 300K tokens (section 6).
- **Rule options.** (a) threshold: if the main account's 5h is above X%, new subagents go to
  the account with the most headroom; (b) always the account with more headroom; (c) manual:
  a mark in a file or the bar ("subagents use account N"). Headroom comes from cswap's usage
  cache (`cache\usage.json`), which the addon can read.
- **Needs section 7's token publishing** for the non-active account.
- **Limits.** The main session stays on its account and can still hit the limit; only
  subagent load moves. A wrong identification would pin a main-session request to the other
  account and cost a cache rebuild, so the fingerprint must be conservative (pin only when
  sure). A subagent that hits a rate limit on its account fails like any other call.
- **Bar:** a count of subagents per account, and a way to see which account a subagent used
  (the ledger gets an `agent` key once identification exists).
- **Alternative that needs no proxy work:** the agent starts a separate headless run itself,
  `cswap run <account> -- -p "<task>"`, for work that should use the other account. Explicit
  rather than automatic, terminal-only, and it works today.

## 9. Open questions and spikes

Each is small and read-only except the restart noted.

- **S1** Does mitmproxy give the addon the local port a client connected to
  (`flow.client_conn.sockname`)? Start `mitmdump --mode regular@8899 --mode regular@8900`
  with a two-line addon that logs it. Needs a proxy restart.
- **S2** Does Claude Code turn `http://user@host:port` in `HTTPS_PROXY` into
  `Proxy-Authorization`? Log the CONNECT request headers' names.
- **S3** Per-environment variable precedence: with both the global `env` block and a Code
  tab environment setting `HTTPS_PROXY`, which does the session use? Check the port the
  proxy sees.
- **S4** What tells a subagent's request apart from the main session's? Log header names and
  body top-level keys, `metadata` keys and the first 60 characters of the system prompt for
  `/v1/messages` (not values of anything secret). Compare a main and a subagent call.
- **S5** Can cswap hand the proxy fresh tokens for inactive accounts through its existing
  locked refresh path without stranding a refresh token that `cswap run` also uses?
- Whether Claude Desktop is the writer of `oauthAccount` (design 4) is inferred, not seen.

## 10. Reading list

- `contrib/desktop-follow/README.md`: how to use it.
- Git history on `feat/windows-desktop-follow` (reliability fixes are the `fix(desktop-follow)`
  commits) and `fix/credential-owns-active`.
- Tests: `tests/test_desktop_follow.py`, `test_desktop_follow_addon.py`,
  `test_desktop_usage.py`, `test_credential_owns_active.py`.
