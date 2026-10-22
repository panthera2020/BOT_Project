# Handoff and Status (v2.0, 2026-10-22)

**Update this file every time the work changes, and prepare a fresh copy whenever usage is about to run out.**

## 1. Where things stand

- Repo: https://github.com/panthera2020/BOT_Project.git (`main`). Local copy: `C:\Users\HP\Downloads\My Bot project`.
- Stack: Python 3.10+ (`asyncio`), FastAPI, Uvicorn, HTTPX, Aiosqlite, Docker.
- The strategy, risk and exit logic exist and have unit tests. **Since v1.7 the demo trading engine runs inside the web console** (section 4): pressing Start Demo Testing (with demo API keys saved), or restarting while demo was running, starts it. It has been tested against a simulated Bybit only; **it has never run against the real Bybit demo API** (the build workspace cannot reach Bybit).
- Admin login failed on the deployed page because the sign-in form described in the old handoff (v1.2) was never pushed to GitHub (section 3). The fix is on `main` (v1.3 commit `4a84047`).
- v1.4 fixed the demo gate (24h and 20 clean trades): the clock starts at the first "Start Demo Testing" and is not reset by pausing or restarting.
- v1.5 adds full state continuity (section 4): flags, kill switch, demo clock and count, open position, Telegram settings, closed trades and (encrypted) API keys are saved to a local SQLite file on every change and restored on startup, so a restart or a hard kill no longer wipes the console. The loop that actually places demo trades is still not built, so the trade count stays at 0 until it is.
- v1.6 makes the phone (or any always-on device) the home of the state: `scripts/run_on_phone.sh` keeps the state file on the device, restarts the app within seconds if it dies and the restarted app loads the last save; `src/tools/backup_state.py` makes verified backup copies and restores them; the dashboard has a **Saved State** card (saving on/off, last save time, what was restored, warnings). The hosted web version is unchanged and still works (below).
- v1.7 adds the demo engine (`src/engine/`), exchange-side stop and take-profits, closed-trade recording that feeds the demo gate, kill switch that closes the trade on Bybit, and an honest radar on the dashboard. The Bybit adapter now raises on errors instead of returning empty results.
- v1.8 adds the **short side** and fixes two faults in v1.7: it did not follow the PRD trend table (a long could be taken when the 15m trend was Down), and take-profit / breakeven prices were not rounded to Bybit's 0.1 tick, which Bybit would have rejected. Saved setups from v1.7 (no side) are read as long.
- v2.0 adds the **strategy selector** (section 4b): six strategies, switchable on the phone dashboard, each with its own results, 24h clock, 20-trade count and live unlock. The original strategy's rules are unchanged (its 53 engine tests pass untouched). Never run against the real Bybit; the new strategies' rules are my interpretations (section 4b).
- v1.9.1: the Bybit adapter now measures the gap between the device clock and Bybit's (`/v5/market/time`) and stamps requests with Bybit's time; on error 10002 it re-measures and retries once. A phone clock 1s fast had made every signed call fail (`tests/test_bybit_clock.py`). Still never run against real Bybit.
- v1.9 adds the **Supabase link and the Vercel remote dashboard** (section 5b). The engine still runs only on the phone. Supabase holds a copy of the phone's state and trades and a mailbox for commands; the Vercel page shows the phone's latest status and sends Start / Pause / Kill to it. **None of this was run against real Supabase or Vercel** (the build workspace cannot reach them); it was tested against an in-memory imitation of Supabase's REST API (`tests/fake_supabase.py`).
- Tests (v1.8 figures; v2.0 has 318 passing with `cryptography`, 305 + 13 skipped without it, 299 + 13 skipped on the phone's pydantic 1 stack excluding the launcher tests): `pytest -q` gave 188 passed with the `cryptography` package installed, and 175 passed + 13 skipped (skips are the encryption-dependent tests, with a visible reason) without it. Run in several test orders. A real server was also killed with `kill -9` and restarted: demo state, clock, 21 trades, keys and Telegram settings came back, no secret was readable on disk, and live trading did not restart by itself. The page was driven end to end in a simulated browser (jsdom: sign-in, start demo, save keys, kill switch, and again after `kill -9`), but **not in a real browser**. The launcher was tested on Linux (hard kill, restart, state reload, backups, refusing weak passwords); **it has not been run on a real Android phone or in Termux**.
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
| Demo trading engine | `src/engine/demo_engine.py`, `src/engine/market.py` | Candle polling, Strategy 1, risk gates, Bybit demo orders, trade management, recording. Tests: `tests/test_demo_engine.py`, `test_market_helpers.py`, `test_engine_wiring.py` with `tests/sim_exchange.py` (a scripted stand-in for Bybit) |
| Phone launcher, backups | `scripts/run_on_phone.sh`, `scripts/phone.env.example`, `src/tools/backup_state.py` | See section 5 |
| Web console | `src/web/app.py`, `src/web/templates/index.html` | FastAPI; see sections 3 and 4 |
| Trade stats | `src/core/trade_stats.py` | In-memory, restored from `trade_history` on startup |

Not built yet: the bot wallet ledger and 24-hour withdrawal lock, a backtest runner, and **live** order execution (the engine trades the demo account only). The PRD (`PRD.md`, `PRD.pdf`) describes all of them. A longer PRD v0.1 also exists as a Claude doc: https://claude.ai/code/artifact/848b4084-07c8-4e3f-9057-85a781feed66 (its Strategy 1 section is still a placeholder).

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

- **Demo engine (v1.7).** While demo testing is active, the demo keys are saved and the kill switch is off, a background task in the web app (`DemoEngine`) runs every `ENGINE_POLL_SECONDS` (default 15; 5s while in a trade). `BOT_ENGINE=off` disables it. It trades **BTCUSDT, both directions** (v1.8), on the Bybit **demo** account only. **"Start Live Trading" still only sets a flag; nothing trades live.**
  - **Direction follows the PRD trend table** (`market.authorized_direction`): the 15m trend picks the side (Up -> long, Down -> short), the 1h trend only decides full or half risk (same trends = full, different = half), and a Range on either = no trade. The side is chosen when each new 15m candle closes and re-checked at the trigger.
  - **Short = exact mirror of the long rules** (the PRD only spells out the long setup, so this mirroring is my interpretation): the 15m candle's high must trade strictly above the last unswept swing high and close back below it; then exactly three bearish 2m candles, none with a high above the sweep high; stop = sweep high + 15 buffer; entry is a market sell; TP1/TP2 below entry; breakeven and early exit mirrored (`exit_policy` already supported sells). `calculate_net_rr` is written for longs; for a short its exit fee is computed on entry + 3d instead of entry - 3d, a hair conservative.
  - Flow (long shown; short mirrored): read the wallet balance; refuse to trade if a position exists on Bybit that the bot did not open; take the last *closed* 15m candle (once each); sweep check against the most recent unswept swing low; then watch closed 2m candles (built from two 1m candles, Bybit has no 2m) for 3 bullish candles that never trade below the sweep low (window 60 minutes after the sweep closes); then trend / side check, risk schedule (`evaluate_risk_schedule`), sizing, net RR >= 2, margin check; then a market order with the stop-loss attached on Bybit, then reduce-only limit orders on the other side for TP1 (70% at 3R) and TP2 (30% at 5R) resting on Bybit (prices rounded to the 0.1 tick). If the stop or a take-profit cannot be placed, the trade is closed at once and recorded as not clean.
  - While in a trade: stop moves to breakeven on Bybit at 1.5R; early exit (market close) on a 50% giveback after 1R and before 1.5R. When Bybit shows the position flat, the result (net closed PnL, which includes fees) is read from `closed-pnl` and recorded through `record_demo_trade`, which is what feeds the 24h/20-trade gate. 1R = the risk budget (`effective_r`). A trade counts as clean unless it was closed by the kill switch, by a safety close, or its result could not be read.
  - Startup / restart: the open trade, the evaluated candle, the armed setup and the day's starting capital are saved with the rest of the state. The first tick after a restart compares the saved trade with Bybit (still open: keep managing it; closed while the app was off: record it). Bybit holds the stop and take-profits, so a trade stays protected while the phone or app is off.
  - Pause stops the engine but leaves an open trade (protected on Bybit); resume re-checks it. The kill switch stops the engine, cancels orders and closes the trade at market (if Bybit cannot be reached it says so and keeps the record).
  - Errors (network, Bybit rejections) are retried with a growing pause up to 2 minutes; after 5 failures in a row one Telegram alert is sent, and one when it recovers. The dashboard shows the engine's text and last decision.
  - **Interpretations I made where the PRD is silent** (all in `src/engine/market.py` and `demo_engine.py`, easy to change): swing high/low = 5-candle fractal; trend = last two swing highs and lows both rising (UP) or both falling (DOWN), else RANGE (which stands down); macro = 1h, micro = 15m; 2m window = 60 minutes; entry price = close of the last 2m candle, stop = sweep low minus 15; leverage = ceiling (50x above $50 equity, 100x from $10 to $50) with a refusal if margin would exceed 90% of equity; demo has no daily trade cap (as the Start Demo message says); "clean" as above. `day_start_capital` is the demo wallet balance at the start of each Lagos trading day (02:00), so a large demo balance means a large risk per trade (for example a 50,000 USDT demo wallet gives $500 risk per trade under the PRD schedule).
  - **Not verified against real Bybit:** request/response field names and endpoints were checked against Bybit's v5 docs (all used endpoints are listed as supported on demo trading), but no real call has been made. Known assumptions: one-way position mode (`positionIdx` 0; a short is a Sell in one-way mode); `retCode` 34040 on trading-stop is treated as success ("not modified"); if the account is in hedge mode or a request is rejected, the engine reports the Bybit message in the dashboard and Telegram instead of trading. Watch the first trades closely.
  - Strict rules mean trades can be rare: a swing-low sweep, a 2m trigger, a non-range trend and net RR >= 2 must all line up. "No trades yet" is not by itself a fault; check the dashboard's "Last decision" line.
  - Not done: live execution, a breakeven check that reads the stop back from Bybit, partial-fill handling beyond what Bybit reports in the position, backtesting the interpretations above.
- **Demo gate (rule fixed in v1.4, but nothing feeds it yet).** Live unlocks at >= 24h since the first "Start Demo Testing" AND >= 20 clean demo trades. `record_demo_trade(...)` and `record_live_trade(...)` in `src/web/app.py` are the only places a closed trade is recorded; **the demo engine calls `record_demo_trade` when a trade closes** (v1.7); live recording is not wired because live execution is not built. A clean demo trade is saved together with the new count in one all-or-nothing write. What counts as "clean" is still an open PRD decision: the default counts every properly closed demo trade, and the caller can pass `clean=False`.
- **State continuity (v1.5).** Everything below is saved to one SQLite file on every change and restored on startup. If the process is killed at any moment, the last completed save is what comes back.
  - Saved: demo/live on-off flags, kill switch, current mode, demo clock, clean-trade count, open position, Telegram chat id, closed trades (Demo and Live), and API keys and the Telegram token (encrypted).
  - Settings (environment variables):
    - `DATABASE_URL`: path of the SQLite file (the Dockerfile sets `/data/trading_bot.db`). Unset means nothing is saved and the app logs a warning.
    - `STATE_ENCRYPTION_KEY`: passphrase of 12+ characters. Without it (or without the `cryptography` package) API keys and the Telegram token are **never written to disk** and must be re-entered after a restart; `/api/status` says so under `persistence`. If this key is lost, only the keys are lost, not the rest of the state.
    - `AUTO_RESUME_LIVE`: set to `1` to let live trading restart by itself. Default off.
  - Resume policy after a restart: demo testing that was running resumes by itself. **Live trading does NOT resume** unless `AUTO_RESUME_LIVE=1`, the 24h/20-trade gate is still unlocked and live keys are present; otherwise it stays paused and needs one press of Start Live Trading. This is deliberate: real money must not restart unattended before the engine can check Bybit for open positions. If the kill switch was engaged, everything stays stopped.
  - `/api/status` has a `persistence` block: whether saving is on, whether secrets are saved encrypted, when state was last saved, what resumed, and notes (for example "live trading was NOT resumed").
  - The file only survives restarts if it sits on storage that survives restarts. On Back4app and Render free tiers the filesystem is wiped on redeploy or restart, so this does not help there (section 5).
  - Not done: API keys are held unencrypted in memory while the app runs; 
- The made-up seed trade was removed in v1.5, so stats only show real trades.

## 4b. Strategy selector (v2.0)

The engine runs one strategy at a time, chosen in the dashboard's **Strategy** card (or `POST /api/strategy`). Switching is refused while a trade is open or live is running, restarts the engine on the new strategy if demo is running, and never resets any strategy's results. The strategies are in `src/engine/strategies/` (add a class to `STRATEGY_CLASSES` in `__init__.py` and it appears in the list):

| id | Strategy | Needs |
|---|---|---|
| `sweep_reversal` | Liquidity sweep reversal: **the original, rule for rule** | 1h and 15m trend not Range |
| `sweep_reversal_loose` | Same idea, separate strategy: 3-candle swings, 15m trend alone picks the side, 1h Range allowed (half risk), 120 min trigger window | 15m trend not Range |
| `trend_pullback` | Bounce off the 15m EMA20 inside a 1h trend (EMA20 vs EMA50 must agree); stop = lowest/highest of last 5 candles -/+ 15 | 1h trend not Range |
| `breakout_retest` | 15m close beyond the prior 20-candle high/low, then a retest that holds within 8 candles; stop = retest candle extreme -/+ 15 | 1h trend must not oppose |
| `session_breakout` | First 15m close beyond the opening hour's range at London 08:00 / New York 14:30 **UTC** (no daylight saving), within 3 hours, one trade per session; stop = other side of the range -/+ 15 | none |
| `range_reversion` | 15m trend Range; candle pierces the Bollinger(20,2) band, closes back inside, bullish/bearish, RSI(14) <= 35 / >= 65; stop = candle extreme -/+ 15 | 15m Range |

**Same gates for all (in `DemoEngine._enter`):** capital and leverage tiers, position sizing, net RR >= 2, 90% margin limit, stop on Bybit with the order, TP1 70% at 3R and TP2 at 5R on Bybit, breakeven at 1.5R, early exit on a 50% giveback, kill switch, and the demo gate (24h AND 20 clean trades) **per strategy**. Risk: the original uses the PRD trend table exactly (Range stands down, half risk when 1h and 15m disagree). `trend_pullback`, `breakout_retest` and the loose sweep take full risk when the 1h and 15m trends agree and half otherwise; `session_breakout` and `range_reversion` take full risk. The original strategy's progress is still the saved `demo_run_start_time` / `clean_demo_trade_count`; the others are in `strategy_progress`. Every trade is stored with its strategy (`trade_history.strategy`, added to older files automatically, defaulting to the original).

Things to know:
- **All the new rules are my interpretations**, not the PRD's. They are plain and easy to change in the strategy files, but none has been tested against real data, and none is known to be profitable.
- **The shared net-RR gate is strict for tight stops.** With the PRD's slippage and fee model, a stop under roughly $300 on BTC often fails "NET_RR_BELOW_GATE", so the candle-based strategies (small stops) will skip many signals. It shows in "Last decision". Whether to relax this is a decision for you, not something I changed.
- The 3R/5R exit plan is the same for all, which suits trend strategies better than `range_reversion`.
- The Vercel remote page shows the selector read-only ("switch on the phone"); switching needs the phone console, because the Supabase command table only knows start / stop / kill.
- Tests: `tests/test_strategies.py` (rules on hand-built candles, risk modes, each strategy end to end on the simulated Bybit), `tests/test_strategy_selector.py` (selection, per-strategy results and gate, saving, database upgrade). The dashboard was driven in a simulated browser (select, switch, results table), not a real one.

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

## 5b. Supabase + Vercel (v1.9): remote dashboard and cloud backup

How it fits: the phone runs the bot and its SQLite file as before. If `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are set, the phone also (a) pushes a status snapshot every 10s, (b) checks every 5s for Start / Pause / Kill pressed on the Vercel page and runs them, (c) copies its saved state and each closed trade to Supabase every 60s (only what changed). The trading engine never waits on Supabase; if it is down the console shows the error in its `cloud` status block and trading carries on. **Bybit API keys never go through Vercel; the Vercel page refuses to save keys, Telegram settings and live controls ("do it on the phone").** The encrypted keys are copied to Supabase only if `SUPABASE_SYNC_SECRETS=1` (default off).

Recovery: a phone that starts with an **empty** state file and finds a backup in Supabase restores it (flags, demo clock, trade count, trades). A phone that already has saved state keeps its own (local wins). Open positions: a restored open position is reconciled with Bybit by the engine as after any restart.

Setup (all free tiers; Supabase may ask for no card, check their page):
1. Supabase: create a project, open SQL Editor, paste and run `supabase/schema.sql` (creates 4 tables with row-level security on and no policies, so the public key can do nothing). Project Settings > API: copy the project URL and the **secret** key (`sb_secret_...`, or the legacy `service_role` key). Never put it in the page, the repo or a chat.
2. Phone: add to `~/.trading-bot.env`: `SUPABASE_URL=https://<ref>.supabase.co` and `SUPABASE_SERVICE_KEY=<secret key>`. Restart the launcher. `/api/status` then shows `cloud.configured: true`, and `last_push_utc` fills in within ~10s.
3. Vercel: import the GitHub repo as a new project. Environment variables: `ADMIN_USER`, `ADMIN_PASS` (10+ characters, not the default; the page refuses to work otherwise), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`. Deploy. `pyproject.toml` has `[tool.vercel] entrypoint = "vercel_app.server:app"` and `vercel.json` includes the page template. Open the Vercel URL, sign in. A green "Remote view: phone online" banner means the phone is reporting; a red banner means no report for over 60s (the data shown is old).

Behaviour to know:
- Pressing a button when the phone is online waits up to 8s for the phone's answer. When it is offline the request is queued: **Start is dropped after 2 minutes** (so it cannot surprise you later), **Pause and Kill wait until the phone is back.** An offline phone cannot close a trade; Bybit-side stop-loss and take-profit orders stay in place, and the Bybit app is the fallback.
- Vercel Hobby has no background processes, so the Vercel app only reads and writes Supabase; it cannot run the bot.
- Supabase free projects can be paused after about a week of low activity; the phone's constant traffic should prevent that, but check the dashboard if the page goes red for no reason.
- Anyone with the Vercel URL and your `ADMIN_*` login can press Kill / Start remotely. Use a long password.

**Not verified:** Vercel's zero-config FastAPI detection with the `tool.vercel` entrypoint and `includeFiles` for the page template (if the page says "template missing", the deployment did not include `src/web/templates/index.html`); that Vercel installs the dependencies from `pyproject.toml` in reasonable time (they include some the page does not need); real Supabase behaviour (key header handling for `sb_secret_` keys follows its docs); the page in a real browser (only a simulated one showed the banner). Tests: `tests/test_cloud_sync.py`, `tests/test_vercel_app.py`.

## 7. Next steps, in order

1. Update the phone: `cd ~/BOT_Project && git pull origin main`, restart `bash scripts/run_on_phone.sh`. Demo testing with saved keys starts the engine by itself; watch the dashboard's Market Radar and Telegram for the first sweep.
1a. Optional (v1.9): set up Supabase and Vercel (section 5b) for a remote dashboard and cloud backup.
1b. Choose where it runs (section 5). Set `ADMIN_USER`, `ADMIN_PASS`, `DATABASE_URL` and `STATE_ENCRYPTION_KEY`, run from `main`, and confirm you can sign in and that state survives a restart.
2. Done in v1.7: the demo engine (see section 4). Next: run it on the real Bybit demo account, read the first trades against the strategy rules, and fix any API mismatch.
3. Before any live trading: build live execution, a Bybit reconcile of live positions at startup, and only then reconsider `AUTO_RESUME_LIVE`.
4. Done in v1.6: the `persistence` block is shown on the dashboard (Saved State card).
5. Build the wallet ledger and 24-hour withdrawal lock, then the backtest runner.
6. Open decisions from the PRD: risk guard below $50, leverage tiers (100x from $10 to $50, 50x above, below $10 no trading), alert channel, and how many clean demo trades before live.

## 8. When usage is about to run out

1. Update this file (status, what changed, next steps) and commit it.
2. Push to GitHub, or hand over a patch if you cannot push.
3. Back up `/data/trading_bot.db` if a host is running.
4. Note anything that was not run or verified, as section 1 does.
