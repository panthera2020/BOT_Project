# Handoff & Checklist (v0.1)

**Purpose** – This document captures everything needed to continue the trading‑bot project in a new environment or after the current usage quota is exhausted.  It also defines the testing gate that must be satisfied before any code is written.

---
## 1. Project Snapshot
- **Repository root**: `C:\Users\HP\Downloads\My Bot project`
- **Primary language / framework**: Python 3.10+ with `asyncio` and FastAPI (as chosen in the PRD).
- **Key artifacts**
  - `Design_Document.md` – architecture diagram, UML flow, use‑case diagram (see below).
  - `pyproject.toml` – package metadata and dependencies.
  - `src/` – will contain the package once we start building (not created yet).

---
## 2. UML & Use‑Case Diagrams (included in PRD)
The design document already embeds the following Mermaid diagrams:
- **Architecture diagram** – shows Core Engine, Exchange Adapter, Admin UI, Alerts, Scheduler.
- **Use‑Case / State diagram** – describes admin login, demo test, live enable flow, kill‑switch.
These diagrams are part of the PRD and should be copied into any hand‑off repository so the next team has visual context.

---
## 3. Testing Strategy (100% Coverage & Visible Test Cases)
Every single component will have dedicated, isolated test files stored in the `tests/` directory:
- `tests/test_risk_schedule.py`: Tests capital tier tables, risk per trade calculation, half-risk counter-trend rules.
- `tests/test_position_sizing.py`: Tests loss per BTC (`L`), lot-size floor rounding, minimum exchange notional.
- `tests/test_net_rr_gate.py`: Tests friction calculation (entry/exit fees + slippage) and the 2.0 Net RR filter.
- `tests/test_daily_cap.py`: Tests 2-trade limit and midnight 02:00 Lagos (01:00 UTC) boundary reset.
- `tests/test_strategy_fsm.py`: Tests 1h/15m/2m candle fixtures, liquidity sweep validation, and 3-candle entry triggers.
- `tests/test_exit_policy.py`: Tests TP1 (ceil 70% lots), TP2 (5R), Breakeven at 1.5R, and Early Exit at 1.0R / 50% pullback.
- `tests/test_demo_gate.py`: Tests the 24-hour runtime and ≥20 clean trades requirement before live trading unlock.
- `tests/test_fake_adapter.py`: Backtest replay engine simulating Bybit order matching.
- `tests/test_wallet_ledger.py`: Internal wallet transfers, PnL settlements, and 24-hour withdrawal lock timer.
- `tests/test_api_dashboard.py`: FastAPI endpoints, JWT/Basic authentication, credential vault, and Kill Switch.

All test runs will output detailed test reports and coverage figures visible in the repo (`htmlcov/`, `pytest.xml`).

---
## 4. Git Repository & Remote Setup
The local repository has been initialized with the `main` branch.

To push this repository to GitHub:
1. Create a new empty repository on your GitHub account (e.g., `trading-bot-bybit`).
2. Link your local project to your GitHub repository:
   ```bash
   git remote add origin https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git
   git push -u origin main
   ```
3. Whenever we begin building, every component and its corresponding test file will be committed and pushed immediately.

---
## 4. Handoff Checklist
| Item | Owner | Status |
|------|-------|--------|
| PRD finalized (Design_Document.md, UML, use‑case) | You | ✅ |
| Project skeleton (folders, `pyproject.toml`) | You | ✅ |
| Testing plan documented (this file) | You | ✅ |
| CI/CD pipeline stub (GitHub Actions) prepared – **do not run** until approval | — | ☐ |
| Secrets handling policy (env‑vars for API keys, Telegram token, SMTP) | — | ☐ |
| Hosting provider selection (Fly.io) confirmed | — | ✅ |
| Alert channel configuration (Telegram + Email) defined | — | ✅ |
| Demo‑only test period (≥ 20 clean trades) rule captured in UI spec | — | ✅ |
| Handoff file (`HANDOFF.md`) – **this file** | — | ✅ |

---
## 5. Continuation Instructions
1. **Clone the repository** to a new workstation or CI runner.
2. **Set up environment**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e .
   ```
3. **Create a `.env` file** with the required secrets (see `README.md`).
4. **Run the test suite** – it must pass **all** tests before any code is added:
   ```bash
   pytest -vv
   ```
5. **Review the generated coverage report** (open `htmlcov/index.html`).
6. **Only after you explicitly give the go‑ahead** ("start building"), proceed to implement the first concrete component (e.g., `strategy/strategy1.py`).
   - The repository contains a placeholder `README.md` with the “Ready to Build” flag.
   - When you are ready, flip the flag to `BUILD=YES` and push the commit.

---
## 6. When Usage is Exhausted
If the current compute quota (e.g., free tier on Fly.io) is about to be exhausted:
- Export the SQLite DB (`/data/trading_bot.db`) and store it in a backup bucket.
- Push the current repository state to a remote (GitHub/GitLab).
- Create a new Fly.io app with a fresh IP and point the CI pipeline to the backup DB.
- Update the `.env` on the new host with the same API keys and alert tokens.
- Continue from the **Handoff Checklist** on the new environment.

---
## 7. Sign‑off
*The project shall **not** start any implementation work until the requestor confirms readiness.*

---
*Prepared by Antigravity (AGY) – keep this file version‑controlled.*
