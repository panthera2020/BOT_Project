# Handoff and Status (v1.3, 2026-10-17)

**Update this file every time the work changes, and prepare a fresh copy whenever usage is about to run out.**

## 1. Where things stand

- Repo: https://github.com/panthera2020/BOT_Project.git (`main`). Local copy: `C:\Users\HP\Downloads\My Bot project`.
- Stack: Python 3.10+ (`asyncio`), FastAPI, Uvicorn, HTTPX, Aiosqlite, Docker.
- The strategy, risk and exit logic exist and have unit tests. The web console is deployed, but **it does not run the strategy yet** (section 4).
- Admin login failed on the deployed page because the sign-in form described in the old handoff (v1.2) was never pushed to GitHub (section 3). A fix is in the patch that came with this file.
- The test suite was **not run in the session that wrote v1.3** (the sandbox could not reach PyPI). Run `pytest -q` locally before pushing (section 6).

## 2. What exists in the code

| Area | Files | Notes |
| --- | --- | --- |
| Strategy 1 state machine | `src/strategy/strategy1.py` | 1h bias, 15m sweep, 2m trigger |
| Risk schedule, sizing, net RR gate | `src/core/risk_schedule.py`, `position_sizing.py`, `net_rr_gate.py` | |
| Daily cap (2 trades, 2am Lagos = 01:00 UTC) | `src/core/daily_cap.py` | |
| Exit policy | `src/core/exit_policy.py` | TP1 70%, TP2 5R, breakeven, early exit |
| Exchange adapters | `src/exchange/adapter.py`, `bybit_adapter.py` | Fake adapter and Bybit V5 REST |
| Alerts | `src/alerts/notifier.py` | Telegram via HTTPX; email dispatcher |
| State store | `src/storage/state_store.py` | SQLite (aiosqlite) |
| Web console | `src/web/app.py`, `src/web/templates/index.html` | FastAPI; see sections 3 and 4 |
| Trade stats | `src/core/trade_stats.py` | In-memory |

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
- `clean_demo_trade_count` is never incremented, so the 24h and 20-trade gate cannot unlock live trading from real trades.
- `src/web/app.py` seeds one made-up demo trade (about +2.16R) so the stats are not empty. Remove it when real trades are wired in.
- All console state (API keys, Telegram settings, flags, trades) lives in memory and is lost on every restart or sleep. `DATABASE_URL` is set in the Dockerfile, but the console does not use the SQLite store for these yet. API keys are stored unencrypted in memory.

## 5. Hosting

- **Back4app Containers:** free containers last about 60 minutes per deploy. This matches what you saw, so it is not usable for a bot that must stay up.
- A third-party comparison (SnapDeploy, "state of free hosting", last verified 26-28 Sep 2026, self-reported by a vendor that sells hosting, so check each provider's own pricing page) says:
  - **Koyeb:** the card-free plan was removed in February 2026, a card is required, and the free instance scales to zero after 1 hour idle. The old v1.2 recommendation of Koyeb with no card is probably out of date.
  - **Render:** no card, 750 instance-hours per month, spins down after 15 minutes without inbound traffic and takes about a minute to wake.
  - **Fly.io:** no free plan for new customers; trial only.
- No free option stays awake with a persistent disk. Choices: Render free plus an external pinger (state is still lost on restarts), an always-on PC, or a small paid VPS. Decide before wiring in the live loop.
- Repo has a `Dockerfile` (port 8080, `PORT` env var) and a `fly.toml`.

## 6. Run, test and push

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e . pytest pytest-asyncio
pytest -q
```

Apply and push the v1.3 patch:

```bash
git am 0001-*.patch      # or: git apply 0001-*.patch
git push origin main
```

Run locally with your own credentials:

```bash
set ADMIN_USER=you@example.com
set ADMIN_PASS=choose-a-long-password
uvicorn src.web.app:app --port 8080
```

## 7. Next steps, in order

1. Run the tests, apply the patch, push, set `ADMIN_USER` and `ADMIN_PASS` on the host, redeploy, and confirm you can sign in.
2. Choose a host that can stay up (section 5).
3. Wire the engine: candle polling, `Strategy1StateMachine`, risk and net RR gates, demo orders on Bybit demo, trade logging, and the clean-trade counter. Remove the seeded trade.
4. Persist keys (encrypted) and console state in SQLite.
5. Build the wallet ledger and 24-hour withdrawal lock, then the backtest runner.
6. Open decisions from the PRD: risk guard below $50, leverage tiers (100x from $10 to $50, 50x above, below $10 no trading), alert channel, and how many clean demo trades before live.

## 8. When usage is about to run out

1. Update this file (status, what changed, next steps) and commit it.
2. Push to GitHub, or hand over a patch if you cannot push.
3. Back up `/data/trading_bot.db` if a host is running.
4. Note anything that was not run or verified, as section 1 does.
