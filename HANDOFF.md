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
## 3. Testing Strategy (must be 100 % before any code is merged)
1. **Pure‑function unit tests** – for all modules that contain no external I/O:
   - `risk_schedule.py` – capital‑tier tables, risk per trade calculation.
   - `position_sizing.py` – loss‑per‑BTC (`L`), lot rounding, net‑RR gate.
   - `daily_cap.py` – reset at 02:00 Lagos (01:00 UTC).
   - `trend_extractor.py` – swing‑low, leg detection on synthetic candles.
2. **Integration tests** – using the **FakeAdapter** (backtest) to verify:
   - Signal → RiskEngine → OrderRequest flow.
   - Exit‑policy variants A‑D produce the expected order sequence.
   - Daily‑cap enforcement across midnight boundary.
3. **End‑to‑end scenario tests** – run the full FastAPI app against the fake adapter:
   - Submit a strategy signal via HTTP endpoint, assert correct DB entries and alert messages.
   - Restart the app with an open position, verify protective orders are re‑loaded and duplicate‑order protection works.
4. **Coverage & Reporting** – use `pytest --cov=src` and generate an HTML report.  The CI pipeline must fail if coverage < 95 %.
5. **Test Artefacts** – commit the following to the repository before any build:
   - `tests/` directory with all test modules.
   - `pytest.ini` (default timeout 30s, markers for integration).
   - `coverage.xml` (generated after a clean test run) – to be reviewed by the reviewer.

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
