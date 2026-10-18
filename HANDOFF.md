# Handoff and Status (v1.6, 2026-10-18)

**Update this file every time the work changes, and prepare a fresh copy whenever usage is about to run out.**

## 1. Where things stand

- Repo: https://github.com/panthera2020/BOT_Project.git (`main`). Local copy: `C:\Users\HP\Downloads\My Bot project`.
- Stack: Python 3.10+ (`asyncio`), FastAPI, Uvicorn, HTTPX, Aiosqlite, Docker.
- The strategy, risk and exit logic exist and have unit tests. The web console is deployed, but **it does not run the strategy yet** (section 4).
- Admin login failed on the deployed page because the sign-in form described in the old handoff (v1.2) was never pushed to GitHub (section 3). The fix is on `main` (v1.3 commit `4a84047`).
- v1.4 fixed the demo gate (24h and 20 clean trades): the clock starts at the first "Start Demo Testing" and is not reset by pausing or restarting.
- v1.5 adds full state continuity (section 4): flags, kill switch, demo clock and count, open position, Telegram settings, closed trades and (encrypted) API keys are saved to a local SQLite file on every change and restored on startup, so a restart or a hard kill no longer wipes the console. The loop that actually places demo trades is still not built, so the trade count stays at 0 until it is.
- v1.6 makes the phone (or any always-on device) the home of the state: `scripts/run_on_phone.sh` keeps the state file on the device, restarts the app within seconds if it dies and the restarted app loads the last save; `src/tools/backup_state.py` makes verified backup copies and restores them; the dashboard has a **Saved State** card (saving on/off, last save time, what was restored, warnings). The hosted web version is unchanged and still works (below).
- Tests: `pytest -q` gives 116 passed with the `cryptography` package installed, and 103 passed + 13 skipped (skips are the encryption-dependent tests, with a visible reason) without it. Run in several test orders. A real server was also killed with `kill -9` and restarted: demo state, clock, 21 trades, keys and Telegram settings came back, no secret was readable on disk, and live trading did not restart by itself. The page was driven end to end in a simulated browser (jsdom: sign-in, start demo, save keys, kill switch, and again after `kill -9`), but **not in a real browser**. The launcher was tested on Linux (hard kill, restart, state reload, backups, refusing weak passwords); **it has not been run on a real Android phone or in Termux**.
- Hosted web version: checked by running the Dockerfile's command with `DATABASE_URL` unset: `/` and `/health` return 200 and the console shows "Saving is OFF". On a host with an ephemeral disk the state is lost on redeploy, as before. The Dockerfile sets `DATABASE_URL=/data/trading_bot.db`; that only persists if `/data` is a persistent volume.

## 2. What exists in the code

| Area | Files | Notes |
| --- | --- | --- |
| Strategy 1 state machine | `src/strategy/strategy1.py` | 1h bias, 15m sweep, 2m trigger |
| Risk schedule, sizing, net RR gate | `src/core/risk_schedule.py`, `position_sizing.py`, `net_rr_gate.py` | |
| Daily cap (2 trades, 2am Lagos = 01:00 UTC) | `src/core/daily_cap.py` | |
| Demo gate (24h and 20 clean trades) | `src/core/demo_gate.py` | Pure rule; `tests/test_demo_gate.py` |
| Exit policy | `src/core/exit_policy.py` | TP1 70%, TP2 5R, breakeven, early exit |
| Exchange adapters | `src/exchange/adapter.py`, `bybit_adapter.py` | Fake adapter and Bybit V5 REST |
| Alerts | `src/alerts/notifier.py` | Telegram via HTTPX; email dispatcher |
| State store | `src/storage/state_store.py` | SQLite (aiosqlite): key-value state, all-or-nothing multi-key saves, `trade_history` table |
| Secrets at rest | `src/storage/secrets_box.py` | Fernet encryption (optional `cryptography` package); never writes a secret in plain text |
| What is saved / resume rules | `src/core/console_state.py` | Pure functions: snapshot, restore with per-field validation, resume policy |
| Web console | `src/web/app.py`, `src/web/templates/index.html` | FastAPI; see sections 3 and 4 |
| Trade stats | `src/core/trade_stats.py` | In-memory, restored from `trade_history` on startup |

Not built yet: the bot wallet ledger and 24-hour withdrawal lock, a backtest runner, and the loop that connects the strategy to the exchange (section 4). The PRD (`PRD.md`, `PRD.pdf`) describes all of them. A longer PRD v0.1 also exists as a Claude doc: https://claude.ai/code/artifact/848b4084-07c8-4e3f-9057-85a781feed66 (its Strategy 1 section is still a placeholder).

## 3. Admin login: cause and fix

- **Cause:** `main` on GitHub (commit `e5749c1`) still used the browser's native Basic Auth popup and had no `POST /api/login`, no sign-in form and no `authFetch` in `index.html`. Free hosts' proxies hide that popup, so every API call returned 401 and nothing on the page worked. The v1.2 handoff described a form-based login, but that work was only ever on the local machine and was not committed.
- **Fix (in the v1.3 patch):**
  - `POST /api/login` checks the credentials.
  - `index.html` has a sign-in overlay and an `authFetch` wrapper that sends `Authorization: Basic ...` on every request. Credentials are kept in `sessionStorage` only, so closing the tab signs you out.
  - 401 responses no longer carry `WWW-Authenticate`, so browsers never show the native popup.
  - Comparison uses `secrets.compare_digest`.
  - Three new tests in `tests/test_web_dashboard.py`.
- **Credentials:** read from the environment variables `ADMIN_USER` and `ADMIN_PASS`. If they are not set, the defaults in `src/web/app.py` apply. **Those defaults are in a public repository, so set both variables on the host before putting real API keys into the page.** The app logs a warning while the default password is in use.
- Not done: login rate limiting or lockout, and a sign-out button.

## 4. Known gaps (read before trusting the dashboard)

- "Start Demo Testing" and "Start Live Trading" only flip flags in `RUNTIME_STATE`. Nothing in `src/` calls `Strategy1StateMachine`, so the Market Radar text and "last scan" time are static. `BotEngine` in `src/main.py` only has `start` and `stop`.
- **Demo gate (rule fixed in v1.4, but nothing feeds it yet).** Live unlocks at >= 24h since the first "Start Demo Testing" AND >= 20 clean demo trades. `record_demo_trade(...)` and `record_live_trade(...)` in `src/web/app.py` are the only places a closed trade is recorded; **the engine must call them when a trade closes**, and nothing does yet. A clean demo trade is saved together with the new count in one all-or-nothing write. What counts as "clean" is still an open PRD decision: the default counts every properly closed demo trade, and the caller can pass `clean=False`.
- **State continuity (v1.5).** Everything below is saved to one SQLite file on every change and restored on startup. If the process is killed at any moment, the last completed save is what comes back.
  - Saved: demo/live on-off flags, kill switch, current mode, demo clock, clean-trade count, open position, Telegram chat id, closed trades (Demo and Live), and API keys and the Telegram token (encrypted).
  - Settings (environment variables):
    - `DATABASE_URL`: path of the SQLite file (the Dockerfile sets `/data/trading_bot.db`). Unset means nothing is saved and the app logs a warning.
    - `STATE_ENCRYPTION_KEY`: passphrase of 12+ characters. Without it (or without the `cryptography` package) API keys and the Telegram token are **never written to disk** and must be re-entered after a restart; `/api/status` says so under `persistence`. If this key is lost, only the keys are lost, not the rest of the state.
    - `AUTO_RESUME_LIVE`: set to `1` to let live trading restart by itself. Default off.
  - Resume policy after a restart: demo testing that was running resumes by itself. **Live trading does NOT resume** unless `AUTO_RESUME_LIVE=1`, the 24h/20-trade gate is still unlocked and live keys are present; otherwise it stays paused and needs one press of Start Live Trading. This is deliberate: real money must not restart unattended before the engine can check Bybit for open positions. If the kill switch was engaged, everything stays stopped.
  - `/api/status` has a `persistence` block: whether saving is on, whether secrets are saved encrypted, when state was last saved, what resumed, and notes (for example "live trading was NOT resumed").
  - The file only survives restarts if it sits on storage that survives restarts. On Back4app and Render free tiers the filesystem is wiped on redeploy or restart, so this does not help there (section 5).
  - Not done: after a restart the open position is restored from the saved record but is **not yet checked against what Bybit actually has open** (the engine does not exist yet); there is no network retry/backoff (also the engine's job); API keys are held unencrypted in memory while the app runs; the dashboard page does not show the `persistence` block yet.
- The made-up seed trade was removed in v1.5, so stats only show real trades.

## 5. Hosting

- **Back4app Containers:** free containers last about 60 minutes per deploy. This matches what you saw, so it is not usable for a bot that must stay up.
- A third-party comparison (SnapDeploy, "state of free hosting", last verified 26-28 Sep 2026, self-reported by a vendor that sells hosting, so check each provider's own pricing page) says:
  - **Koyeb:** the card-free plan was removed in February 2026, a card is required, and the free instance scales to zero after 1 hour idle. The old v1.2 recommendation of Koyeb with no card is probably out of date.
  - **Render:** no card, 750 instance-hours per month, spins down after 15 minutes without inbound traffic and takes about a minute to wake.
  - **Fly.io:** no free plan for new customers; trial only.
- No free hosted option stays awake with a persistent disk. Render's own docs say a free web service has an ephemeral filesystem (SQLite files are lost on every redeploy, restart or spin-down) and that its free Postgres expires after 30 days with a 14-day grace period before deletion (https://render.com/docs/free). Koyeb and Fly.io need a card.
- **An always-on device you control keeps the state file**, with no card needed: a phone running Termux, a spare PC or a Raspberry Pi. Android may kill background apps, so battery optimization must be turned off for the terminal app and a wake lock held; this was not verified in this session, so check Termux's current docs. Open the dashboard through a private tunnel such as Tailscale rather than a public URL (free-tier terms not verified).
- **Phone setup (v1.6).** In Termux: install Python and git, `git clone` the repo, `pip install -e ".[secrets]"` (if `cryptography` will not build, install Termux's own package for it, or run without it: keys then stay in memory only). Then:
  ```bash
  cp scripts/phone.env.example ~/.trading-bot.env && chmod 600 ~/.trading-bot.env
  nano ~/.trading-bot.env          # your own ADMIN_USER / ADMIN_PASS / STATE_ENCRYPTION_KEY
  termux-setup-storage             # only if you want backups on shared storage
  bash scripts/run_on_phone.sh
  ```
  The state file is `~/trading-bot-state/trading_bot.db` (Termux's private storage; it survives the app being killed or the phone rebooting, but is **erased if Termux's data is cleared or the app is uninstalled**). For that, set `BOT_BACKUP_DIR` to a folder on shared storage (hourly verified copies, newest 24 kept). Restore on a new phone with `python -m src.tools.backup_state restore <backup file> <state file>` (add `--force` to overwrite; the old file is kept as `.before-restore`). The launcher refuses the public default password, one shorter than 10 characters, and warns if the env file is readable by others.
  Open the page at `http://127.0.0.1:8080` on the phone itself. To open it from another device, set `BOT_HOST=0.0.0.0` (plain HTTP, trusted Wi-Fi only) or use a private tunnel. The phone must also have battery optimization turned off for Termux; the script holds a wake lock if `termux-wake-lock` (Termux:API) exists. Neither was verified here. If the phone reboots, Termux does not start the script by itself (Termux:Boot can, not tested).
- Other options: Render free plus an external pinger and an external free database (for example Neon, which needs no card; the state layer would have to be moved to Postgres, which is not built), or a small paid VPS (about $4-5 a month, needs a payment method). Decide before wiring in the live loop.
- Repo has a `Dockerfile` (port 8080, `PORT` env var) and a `fly.toml`.

## 6. Run, test and push

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e . pytest pytest-asyncio
pytest -q
```

Get the latest code (the v1.3 login fix and the v1.4 demo gate are both on `main`):

```bash
git pull origin main
```

Run locally with your own credentials and saved state (Windows shown; use `export` on Linux or Android):

```bash
pip install -e ".[secrets]"        # adds the cryptography package; on Termux the platform's own package may be needed
set ADMIN_USER=you@example.com
set ADMIN_PASS=choose-a-long-password
set DATABASE_URL=C:\bot-state\trading_bot.db
set STATE_ENCRYPTION_KEY=a-long-passphrase-you-keep-safe
uvicorn src.web.app:app --port 8080
```

Keep the `DATABASE_URL` file on storage that survives restarts and back it up. Keep `STATE_ENCRYPTION_KEY` out of the repository and out of the state file's folder.

## 7. Next steps, in order

1. Choose where it runs (section 5). Set `ADMIN_USER`, `ADMIN_PASS`, `DATABASE_URL` and `STATE_ENCRYPTION_KEY`, run from `main`, and confirm you can sign in and that state survives a restart.
2. Wire the engine: candle polling with retry/backoff, `Strategy1StateMachine`, risk and net RR gates, demo orders on Bybit demo with stop and take-profit placed on Bybit itself, and a call to `record_demo_trade(...)` / `record_live_trade(...)` when each trade closes (this is what makes the gate unlock).
3. On startup the engine must ask Bybit for open positions and orders and reconcile them with the saved `active_position` before trading. Once that exists, `AUTO_RESUME_LIVE` can be reconsidered.
4. Done in v1.6: the `persistence` block is shown on the dashboard (Saved State card).
5. Build the wallet ledger and 24-hour withdrawal lock, then the backtest runner.
6. Open decisions from the PRD: risk guard below $50, leverage tiers (100x from $10 to $50, 50x above, below $10 no trading), alert channel, and how many clean demo trades before live.

## 8. When usage is about to run out

1. Update this file (status, what changed, next steps) and commit it.
2. Push to GitHub, or hand over a patch if you cannot push.
3. Back up `/data/trading_bot.db` if a host is running.
4. Note anything that was not run or verified, as section 1 does.
