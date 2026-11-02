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
| 7 | **An account per session (and so per project)** | **designed; spikes S1, S4, S6 done, S5 answered, build not started** | section 7, mockup `mockups/account-bar.html` |
| 8 | **Subagents on another account** | **designed (nice to have); identification verified** | section 8 |

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
| `uv tool install --force` fails with "Access is denied" and leaves cswap broken (`No module named claude_swap`) | The minute task's `pythonw.exe` lives in the tool's `Scripts` folder and holds it while it runs | Retry the install a few times; the proxy and the task come back once it succeeds |
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

## 7. An account per session (and so per project)

Decided with the user while reviewing the mockup (`mockups/account-bar.html`):

- Several projects run at once, each on its own account. Switching one must not move the others.
- The bar acts on **this session only**. There is no "set the global default" control and no
  threshold rule (the above-80% idea was dropped). A session shows as its own filled dot; other
  sessions show only as a hollow dot with a count.
- `cswap switch` and `cswap auto` still exist. Their only effect on the Code tab is the
  account a *new* session starts on (below).

### What the proxy can see (spike S4, verified)

Every model request (`/v1/messages`) carries, as plain headers and body fields:

| What | Where | Use |
| --- | --- | --- |
| Session id | header `x-claude-code-session-id`, and `metadata.user_id` (a JSON string with `device_id`, `account_uuid`, `session_id`) | Identifies the session. The same id as `sessionId` in the transcripts, so the ledger joins to a project |
| Request class | header `x-claude-code-request-class`: `main` or `subagent` | Tells a subagent's calls from the session's own |
| Prompt id | header `x-claude-code-prompt-id` | Probably per user prompt: subagents spawned by one prompt share it (not confirmed) |
| Listening port | `flow.client_conn.sockname` | Which proxy port the client used (spike S1, verified) |

Observed with this session: the main loop, its helper calls and its subagents all carry one
session id; subagent calls have class `subagent` and, in the first call, an extra system block
"You are an agent for Claude Code" with 46 tools. Helper calls Desktop makes for a session
(titles, classifiers) carry the session id too, with class `main`.

### Design: one proxy port, a table keyed by session id

- The proxy keeps `sessions.json`: `session id -> account`. The addon reads it (cached by mtime,
  like the credential file).
- **First sight pins.** A request from an unknown session id records that session on the account
  cswap has active at that moment. From then on the session stays there, so a global
  `cswap switch` or `cswap auto` moves only sessions started afterwards. This is what makes
  several projects independent without any per-project setup.
- **The bar moves a session.** A click writes this session's id and the chosen account into
  `sessions.json`. The next request goes out on the new account. It costs a cache rebuild (section 6),
  so the bar should say so. The band must know its own session id (spike S6).
- Entries expire (for example 7 days idle), and `cswap desktop-follow --sessions` lists them.
- **Tokens for accounts that are not active.** Still the hard part. The addon reads the live
  credential file, which holds only the active account. A pinned session on another account needs
  that account's fresh access token. cswap refreshes only through its locked machinery (refresh
  tokens rotate; a second refresher strands one), so cswap publishes `desktop-follow\tokens.json`
  (0600; per account: access token, `expiresAt`) from the minute task; the addon reads it and, as
  today, never substitutes an expired token (the request keeps the app's login). Cost: tokens of
  inactive accounts sit in plaintext, as the active one already does. A pinned account whose token
  could not be refreshed bills the app's account, and the bar must show that (state "unknown").
- **Ledger:** add `session` (full id) and `class` to each line. The report then breaks down by
  session and project, and the bar's counts come from the last 60 seconds of the ledger (distinct
  session ids per account), published as `activity.json`.
- **Unchanged:** Remote Control and Artifacts stay on the app's account.

Superseded: labelling sessions by proxy port or proxy login (`HTTPS_PROXY=http://127.0.0.1:8901`
per Code tab environment). It would work for launch-time choices but cannot move a running
session, and needs per-project setup. The port is still useful as an escape hatch.
Rejected: a different proxy process per account; `cswap run` for the Code tab (terminal only; it
and `cswap map` remain the way to bind a folder to an account for terminal sessions).

## 8. Subagents on another account (nice to have)

Requirement: a session's consecutive subagents can use the other account, for example when the
session's own account is nearly full.

Identification is solved: header `x-claude-code-request-class: subagent` (section 7). No
fingerprinting of system prompts is needed.

- **Rule:** the table gets an optional `subagents -> account` per session (default: same account as
  the session). A subagent request resolves to that account, otherwise to its session's.
- **Why this is cheap:** a subagent has a small context of its own, so its cache rebuild is small
  compared with the main session's 260 to 300K tokens (section 6), and the session's own account
  and cache are untouched. Only the subagent load moves.
- **Setting it:** manual, from the bar ("send subagents to gordian-claude"), shown as the
  `↳ 1 agent` chip on the account they run on (as in the mockup). The above-threshold trigger was
  dropped by the user; an automatic rule can be added later without changing the table.
- **Individual subagents:** `x-claude-code-prompt-id` may group the subagents of one prompt. A
  per-subagent id was not found in the headers; the first request of a subagent also carries
  the extra system block. If per-subagent pins are ever needed, key them on a hash of that
  conversation's first message.
- **Visibility:** ledger lines with `class: subagent` give per-account subagent counts for the bar
  and a "by subagent" block in the usage report.
- **Limits:** the main session stays on its account and can still hit the limit. A subagent that
  hits a limit on its account fails like any other call. Needs the token publishing from section 7.
- **No proxy needed today:** an agent can run `cswap run <account> -- -p "<task>"` for work that
  should use the other account. Explicit, terminal-only.

## 9. Open questions and spikes

- **S1 done.** `flow.client_conn.sockname` gives the listening port (logged as 8899).
- **S4 done.** Session id, request class and prompt id are headers (section 7).
- **S2 not needed** unless the proxy-login labelling comes back.
- **S3 open** (only matters for the port escape hatch): which `HTTPS_PROXY` wins when both the
  global `env` block and a Code tab environment set it.
- **S5 mostly answered.** cswap already refreshes every stored slot under its own locks whenever it
  collects usage (`_collect_usage_entries`, which `cswap list`, the TUI, the menu bar and `cswap auto`
  all run, and the band runs `cswap list --json` every minute). Checked on this machine: both
  stored accounts hold a non-expired access token (about 2.3 h left) and a refresh token. So
  `tokens.json` needs no new refresh path: after the collection, read each slot's backup
  credential (`_read_account_credentials`) and write access token plus `expiresAt`; the active
  slot comes from the live credential. Remaining risk: an inactive slot's token is only as fresh
  as the last collection, and the proxy must treat an expired one as "unknown" (the request keeps
  the app's login) and the bar must say so.
- **S6 done.** The plugin API has `$.session.id()`, "the session's id (the transcript file's
  name)". This session's id hashes to the same tag the proxy logged for its requests (`01b3a2`),
  so the bar can identify its own session and write the right row of `sessions.json`.
- **S7 open:** is `x-claude-code-prompt-id` per prompt, and is there a per-subagent id anywhere
  (headers, body `thread`)?
- Whether Claude Desktop is the writer of `oauthAccount` (design 4) is inferred, not seen.

Debugging aid used for S1 and S4: create the file `desktop-follow\shape` and the proxy logs, per
model request, the header names, body keys, the first words of the system blocks, counts, a hashed
session tag and the listening port (ids are masked, message text and credentials are never logged).
Delete the file when done.

## 10. Reading list

- `contrib/desktop-follow/README.md`: how to use it.
- Git history on `feat/windows-desktop-follow` (reliability fixes are the `fix(desktop-follow)`
  commits) and `fix/credential-owns-active`.
- Tests: `tests/test_desktop_follow.py`, `test_desktop_follow_addon.py`,
  `test_desktop_usage.py`, `test_credential_owns_active.py`.
