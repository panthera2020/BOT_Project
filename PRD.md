# Professional Trading Bot Product Requirements Document (PRD v1.0)
**System:** Algorithmic Futures Trading Engine & Multi-Asset Portfolio Manager  
**Target Venue:** Bybit V5 Perpetual (Isolated Margin) & Simulated Sandboxes  
**Document Owner:** @Black  
**Date:** October 17, 2026  
**Status:** Approved Specification (Pre-Implementation Phase)

---

## Executive Summary & Design Tenets

The trading bot is an unattended, deterministic algorithmic execution system designed to eliminate emotional intervention in digital asset trading. It executes strict, written technical rules for position entry, risk sizing, trade monitoring, breakeven progression, and exits. 

The system treats capital preservation as primary and returns as secondary. Every action taken—including all order placements, cancellations, adjustments, and rejections—must map deterministically to a codified rule.

```
                      +-----------------------------+
                      |         Admin User          |
                      +-----------------------------+
                                     |
                         [Web UI / JWT Auth / REST]
                                     v
+------------------+      +---------------------+      +---------------------+
|  Market Data     | ---> |   Strategy Plugin   | ---> |     Risk Engine     |
| (WS / Replay)    |      | (Signals ONLY)      |      | (Gates & Sizing)    |
+------------------+      +---------------------+      +---------------------+
                                                                  |
                                                           [Validated Order]
                                                                  v
+------------------+      +---------------------+      +---------------------+
| Alerting Engine  | <--- |     State Store     | <--- |   Exchange Port     |
| (TG & Email)     |      | (SQLite WAL Ledger) |      | (Bybit V5 / Fake)   |
+------------------+      +---------------------+      +---------------------+
                                     ^                            |
                                     |                    [Execution Events]
                                     +----------------------------+
                                     |                            v
                                     |                 +---------------------+
                                     +---------------- |     Exit Policy     |
                                                       | (Stops, TPs, Early) |
                                                       +---------------------+
```

### Core Tenets
1. **Zero Emotional Drift**: No manual trade overrides on active positions. Protective stop-loss and take-profit orders are physically stationed on the exchange matching engine immediately upon fill confirmation.
2. **Deterministic Risk Budgeting**: Sizing is derived strictly from the mathematical risk-of-ruin capital schedule. Slippage, maker/taker exchange fees, and lot quantization are computed *inside* the risk allowance, never on top.
3. **Hexagonal Architecture (Ports & Adapters)**: Pure separation of business logic from infrastructure. Strategies produce signals; the Risk Engine authorizes orders; Exit Policies manage state; Exchange Adapters isolate network protocols.
4. **Isomorphic Execution**: Identical engine code executes in Historical Backtest, Bybit Demo Sandbox, and Live Mainnet without alteration.
5. **Fail-Closed Guardrails**: If network connectivity lapses, unhandled exceptions occur, or protective orders fail exchange validation, the system immediately flattens open exposure and alerts administrators.

---

## Architectural Correlation & Diagram Analysis

The system design correlates with the two formal system specifications:

### 1. Architectural Component Model (System Decomposition)
Based on the architecture diagram, the system is separated into four primary layers:
* **Control Layer**: 
  - **Scheduler**: Autonomous background timer enforcing the 02:00 Lagos (01:00 UTC) trading day boundary reset, daily PnL consolidation, and report delivery.
  - **Admin UI**: Lightweight FastAPI / Jinja2 / REST interface providing operational mode switching, credential vaulting, performance metrics, and the Emergency Kill Switch.
* **Core Logic Layer**:
  - **Strategy Plugin**: Consumes candle snapshots and indicators; outputs unidirectional signals (`LONG`, `SHORT`) and technical invalidation anchors. Never places or sizes orders directly.
  - **Risk Engine**: The central gatekeeper. Validates signals against daily quotas, capital tier tables, margin boundaries, fee friction, and net reward-to-risk (RR) requirements.
  - **Exit Policy**: Monitors active position lifecycle, calculating dynamic stop adjustments (breakeven moves, early momentum givebacks, and multi-stage target scalps).
* **Ports (Swappable Adapters)**:
  - **Market Data Port**: Ingests live WebSocket/REST feeds or replays historical 1h/15m/2m candle fixtures.
  - **Exchange Port**: Encapsulates external API schemas (`BybitAdapter` for V5 REST/WS and `FakeAdapter` for deterministic local backtests).
* **Storage and Alerts**:
  - **State Store & Wallet Ledger**: High-concurrency SQLite database (WAL mode) maintaining signal audit trails, rejection funnel logs, transaction ledgers, and order states.
  - **Notifier**: Multi-channel distribution (Telegram Bot & SMTP Email) delivering real-time lifecycle events and consolidated ledger reports.

### 2. Use Case Model & Operational State Flow
Based on the system use case specification, user interactions and system triggers follow a rigid operational hierarchy:
* **Admin Actors**:
  - `Log In`: Authenticate via secure email/password sessions.
  - `Run Demo Test`: Provision Bybit Demo credentials and execute unconstrained sandbox evaluation to observe raw strategy win rates.
  - `Enable Live Trading`: **Gated Operation**. Strictly locked by the system engine until the selected strategy completes **≥ 24 continuous hours** of live demo operation AND achieves **≥ 20 clean trades** without rule deviations.
  - `Trigger Kill Switch`: Instantaneous command that terminates new entry polling, cancels pending exchange orders, and issues a market reduce-only order to flatten open exposure.
  - `View Positions and Alerts`: Real-time dashboard monitoring active positions, fill slippage, fee deductions, and win rates.
* **Autonomous System Actors**:
  - `Strategy Actor`: Dispatches signals derived from multi-timeframe swing-point sweeps and momentum confirmations.
  - `Scheduler Actor`: Enforces midnight day resets at 02:00 Lagos (01:00 UTC), re-calculates the operational risk tier ($R$), and triggers daily reporting.

---

## System Scope & Operational Boundaries

### In Scope
1. **Instrument & Venue**: Bybit USDT Perpetual contracts (initial deployment: `BTCUSDT`), operating under **Isolated Margin**.
2. **Multi-Asset Extensibility**: Abstract `PairConfig` interfaces allowing plug-and-play addition of secondary pairs (e.g., `ETHUSDT`, `SOLUSDT`) with zero modifications to the core risk engine.
3. **Execution Modes**:
   - **Backtest**: Event-driven replay across historical 1h, 15m, and 2m OHLCV bars using a realistic fee and tick slippage model.
   - **Demo Trading**: Integration with `api-demo.bybit.com` via REST/WebSocket, evaluating live latency and order lifecycle handling.
   - **Live Production**: Bybit mainnet execution strictly limited to small capital tiers.
4. **Administrative Console**: Embedded web-based administrative dashboard with password-protected sessions, API key isolation, and trade auditing.
5. **Autonomous Risk Controls**: Daily trade limits, capital-dependent leverage ceilings, liquidation distance filters, and pre-trade friction gates.

### Explicitly Out of Scope (v1.0)
- Machine learning/AI dynamic prediction models.
- Decentralized exchange (DEX) on-chain contracts or automated smart contract routing.
- Multi-tenant pooled fund management or public copy-trading interfaces.
- Simultaneous concurrent open positions (v1.0 strictly enforces maximum 1 open trade per instrument).

---

## Multi-Asset Flexibility Component (`PairConfig`)

The engine abstracts pair-specific metadata into a declarative specification. The risk engine and exchange port query this configuration dynamically rather than hardcoding asset parameters:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PairConfig:
    symbol: str               # e.g., "BTCUSDT", "ETHUSDT"
    base_asset: str           # e.g., "BTC"
    quote_asset: str          # e.g., "USDT"
    tick_size: float          # Minimum price increment (e.g., 0.1 for BTC)
    lot_size: float           # Minimum quantity increment (e.g., 0.001 for BTC)
    min_order_qty: float      # Minimum allowed quantity step
    min_notional_value: float # Exchange minimum order notional (e.g., 100 USDT)
    default_stop_buffer: float# Points deducted below sweep level (e.g., 15.0)
    max_exchange_leverage: int# Maximum exchange-permitted leverage (e.g., 100)
    is_active: bool           # Master operational switch
```

---

## Strategy 1: Algorithmic Entry Specification

Strategy 1 is a multi-timeframe liquidity-sweep and momentum-confirmation model.

```
1h Trend Filter       15m Liquidity Sweep                   2m Entry Trigger
  (Macro Bias)          (Setup Formation)                  (Execution Trigger)

   [ Bullish ]            Swing Low                     Sweep Point
  Higher Highs               |                               |
                             v                               v
                        +---------+    Intra-candle     +---------+
                        |  15m    |    Wick Sweep       |  2m     |  3 Consecutive
                        | Candle  |  ===============>   | Candles |  Bullish Closes
                        +---------+                     +---------+  ==============> MARKET
                             |                          [C1][C2][C3]                 BUY
                             +---> Close > Swing Low
```

### 1. Macro Bias (1-Hour & 15-Minute Structural Trend)
The strategy polls 1h and 15m historical candles to define directional bias:
- **Trend Up**: Price prints sequential higher swing highs and higher swing lows.
- **Trend Down**: Price prints sequential lower swing lows and lower swing highs.
- **Range / Neutral**: Indeterminate structural progression.

| 1-Hour Trend Bias | 15-Minute Trend Bias | Authorized Trade Direction | Risk Modifier ($R_{trade}$) |
|---|---|---|---|
| **Up** | **Up** | Long Only | Full Risk ($1.0 \times R$) |
| **Down** | **Down** | Short Only | Full Risk ($1.0 \times R$) |
| **Up** | **Down** | Short Only (Counter-trend) | Half Risk ($0.5 \times R$) |
| **Down** | **Up** | Long Only (Counter-trend) | Half Risk ($0.5 \times R$) |
| **Range** | Any | No Trades (Stand Down) | Zero ($0.0 \times R$) |
| Any | **Range** | No Trades (Stand Down) | Zero ($0.0 \times R$) |

### 2. Setup Formation (15-Minute Candle Liquidity Sweep)
- The algorithm tracks prominent 15m swing pivot extremes (swing highs and swing lows).
- For a **Long setup**: A 15m candle's lower wick must trade strictly below the identified swing low (clearing resting liquidity), but the candle's **close** must print back **above** that swing low.
- If a 15m candle closes *below* the swing low, the liquidity sweep is classified as a structural breakdown; the setup is invalidated immediately.

### 3. Execution Trigger (2-Minute Microstructural Confirmation)
- Once a valid 15m liquidity sweep completes, the trigger engine monitors 2m candles.
- **Long Trigger**: Exactly 3 consecutive bullish 2m candles (Close > Open) printing immediately following the sweep event.
- **Entry Execution**: Market buy order submitted immediately upon the close of the 3rd consecutive bullish candle.
- **Trigger Timeout / Invalidation**: If the 2m price prints a new low below the 15m sweep extreme prior to the 3-candle confirmation, the entire setup aborts.

---

## Trade Management & Exit Policy Engine

Protective stop and target orders are dispatched to the exchange matching engine concurrently with the entry execution.

```
  Price
    ^
    |                                                [TP2: 5R Target] (Remaining Lots)
    |                                                       ^
    |                                                       |
    |                                                [TP1: 3R Target] (70% Quantized Lots)
    |                                                       ^
    |                                                       |
    |                                    [1.5R: Move Stop to Breakeven + Friction Fees]
    |                                           ^
    |                          [1.0R: Arm Early Exit] (Exit at Market if 50% Peak Lost)
    |                                 ^
    |                                 |
----+---------------------------------+----------------------------------------------------> Time
    |                           [Entry Price]
    |                                 |
    v                                 v
[Stop Loss: Sweep Low - Buffer] (Exchange Reduce-Only)
```

### Baseline Exit Rules (Variant A - Default)
1. **Initial Stop Loss**:
   - Longs: Set at $\text{Sweep Low} - \text{Buffer}$ (where buffer is configurable between 10.0 and 20.0 points; default: 15.0 points).
   - Order Type: Exchange-native `Stop Market` or `Stop Limit`, flagged as `reduce_only=True`.
2. **Take Profit 1 (TP1 - 3R Target)**:
   - Position allocation: $70\%$ of executed position size, rounded up to the nearest whole lot:
     $$\text{Lots}_{\text{TP1}} = \left\lceil \frac{0.70 \times \text{Total Quantity}}{\text{Lot Size}} \right\rceil \times \text{Lot Size}$$
   - If $\text{Lots}_{\text{TP1}} \ge \text{Total Quantity}$, the trade converts to a single-exit target at 3R.
3. **Take Profit 2 (TP2 - 5R Target)**:
   - Position allocation: $\text{Total Quantity} - \text{Lots}_{\text{TP1}}$ executed at 5R.
4. **Early Exit Policy (Momentum Protection)**:
   - **Arming Threshold**: Becomes active once unrealized profit touches or exceeds $1.0R$.
   - **Trigger**: If active unrealized profit gives back $\ge 50\%$ of its highest recorded peak gain prior to reaching $1.5R$, the bot immediately flattens the position via market order.
5. **Breakeven Adjustment**:
   - Once unrealized price touches $1.5R$, the stop loss is amended on the exchange to:
     $$\text{Stop}_{\text{BE}} = \text{Entry Price} + \text{Fees}_{\text{entry}} + \text{Fees}_{\text{est\_exit}} + \text{Slippage Buffer}$$
6. **Sweep Failure Invalidation**:
   - If any subsequent 15m candle closes beyond the structural swing anchor while the position remains active, the position closes immediately at market.

### Backtest Exit Variants
- **Variant A**: Baseline (TP1 at 3R, TP2 at 5R, Early Exit at 1.0R/50% pullback, Breakeven at 1.5R).
- **Variant B (Trailing Variant)**: TP1 at 3R; remaining lots managed by a trailing stop pegged beneath newly confirmed 15m swing pivots rather than a fixed 5R limit.
- **Variant C (Pure Target Variant)**: TP1 at 3R, TP2 at 5R, with breakeven moves disabled (testing whether premature breakeven triggers degrade overall expectancy).
- **Variant D (Legacy Discretionary Model)**: Breakeven shift strictly at 2.0R; early exit armed between 1.0R and 1.5R.

---

## Risk Engine & Quantitative Gates

The Risk Engine serves as an unbypassable gatekeeper. Capital $C$ is determined strictly at the **02:00 Lagos (01:00 UTC) day boundary** to guarantee that daily variance does not skew trade risk intra-day.

### 1. Capital Schedule & Base Risk ($R$)
| Day-Start Capital ($C$) | Base Risk per Trade ($R$) | Mathematical Formula |
|---|---|---|
| **$C < \$50.00$** | **\$1.00** | Fixed floor allowance |
| **$\$50.00 \le C < \$450.00$** | **\$1.00 to \$4.50** | $\$1.00 + \$0.50 \times \left\lfloor \frac{C - 50}{50} \right\rfloor$ |
| **$\$450.00 \le C < \$600.00$** | **\$5.00** | Strict consolidation cap |
| **$C \ge \$600.00$** | **\$6.00+** | $\$5.00 + \$1.00 \times \left\lfloor \frac{C - 500}{100} \right\rfloor$ |

*Half-Risk Modifier*: If macro trend and micro trend exhibit counter-trend characteristics (e.g., 1h Up, 15m Down), effective risk is scaled to $R_{\text{trade}} = 0.50 \times R$.

### 2. Leverage Ceiling & Margin Budgeting
| Current Real-Time Capital | Trading Status | Maximum Authorized Leverage |
|---|---|---|
| **$C < \$10.00$** | **Disabled** (System alert dispatched) | None |
| **$\$10.00 \le C \le \$50.00$** | Enabled | Up to $100\times$ (Subject to Bybit limits) |
| **$C > \$50.00$** | Enabled | Up to $50\times$ |

*Dynamic Leverage Derivation*: The bot assigns the minimum integer leverage level required to guarantee that total required initial margin does not exceed **50% of available wallet equity**:
$$\text{Margin}_{\text{req}} = \frac{\text{Qty} \times \text{Entry Price}}{\text{Leverage}} \le 0.50 \times \text{Equity}$$

### 3. Mathematical Position Sizing Model
Total monetary loss per asset unit ($L$) includes stop distance, slippage allowances, and round-turn exchange fees:
$$L = d + s_{\text{in}} + s_{\text{out}} + r_{\text{taker}} \cdot (E + S - s_{\text{out}})$$

Where:
- $E = \text{Best Ask (Entry Price)}$
- $S = \text{Stop Price} = \text{Sweep Low} - \text{Buffer}$
- $d = |E - S| \text{ (Nominal Stop Distance in Points)}$
- $s_{\text{in}}, s_{\text{out}} = \text{Estimated Slippage (Points; defaults: 5.0 in, 10.0 out)}$
- $r_{\text{taker}} = \text{Bybit Taker Fee Rate (0.055\% / 0.00055)}$

Quantity is calculated by quantizing the budget over total friction-adjusted loss, **strictly rounded down**:
$$\text{Qty} = \left\lfloor \frac{R_{\text{trade}}}{L \cdot \text{Lot Size}} \right\rfloor \cdot \text{Lot Size}$$

> [!CRITICAL]
> Quantity is **never rounded up**. If raw calculated quantity evaluates to less than the exchange minimum lot size (e.g., $0.001\text{ BTC}$), the setup is rejected immediately.

### 4. Net Reward-to-Risk (Net RR) Gate
Transaction friction severely degrades nominal payoffs on small stops. The Net RR gate verifies that the actual trade payoff covers all entry, exit, and slippage overhead:
$$\text{Net RR} = \frac{3d - w}{L} \ge 2.0$$

Where $w$ represents total nominal friction cost per coin:
$$w = (E \cdot r_{\text{taker}}) + s_{\text{in}} + ((E + 3d) \cdot r_{\text{maker}})$$
*(Assuming limit maker exit on target orders at $0.02\%$ fee rate).*

### 5. Deterministic Pre-Trade Gate Hierarchy
A setup must pass all 8 gates sequentially. The first failed condition logs a structured rejection event and aborts execution:
1. **Capital Status Gate**: Current equity $\ge \$10.00$.
2. **Daily Quota Gate**: Trades executed within current trading day $< 2$.
3. **Concurrency Gate**: Zero active positions or open orders on the instrument.
4. **Trend Consistency Gate**: Multi-timeframe trend matrix authorizes directional execution.
5. **Exchange Sizing Gate**: $\text{Qty} \ge \text{Min Lot Size}$ AND $\text{Notional Value} \ge \text{Min Notional (100 USDT)}$.
6. **Net Expectancy Gate**: $\text{Net RR} \ge 2.0$.
7. **Leverage & Margin Gate**: Derived leverage $\le \text{Tier Ceiling}$ AND $\text{Required Margin} \le 50\%$ equity.
8. **Liquidation Proximity Gate**: Estimated Liquidation Price satisfies:
   $$|E - \text{Price}_{\text{liq}}| \ge 3.0 \times |E - S|$$

---

## Bot Internal Wallet & Withdrawal Lock

To bypass on-chain transfer latency and network transaction fees, the system maintains a virtual ledger synchronized with Bybit's internal asset accounts.

```
+-------------------------------------------------------------------------+
|                              Bybit Account                              |
|                                                                         |
|  +--------------------+                     +------------------------+  |
|  |   Funding Wallet   | <=================> | Unified Futures Wallet |  |
|  |  (Capital Holding) |   Internal Transfer |  (Isolated Execution)  |  |
|  +--------------------+   (Zero Network Fee)| +------------------------+  |
+-------------------------------------------------------------------------+
           ^                                               |
           | Ledger Synchronization                        | Order Fills
           v                                               v
+-------------------------------------------------------------------------+
|                           Bot State Store                               |
|                                                                         |
|  - Virtual Wallet Ledger (Balance, Available, Locked Margin)            |
|  - 24-Hour Withdrawal Lock Countdown Timer                              |
|  - Pre-registered Whitelisted Destination Address                      |
+-------------------------------------------------------------------------+
```

1. **Trade Funding Lifecycle**:
   - Funds reside in Bybit **Funding Wallet**.
   - Upon pre-trade gate validation, the engine transfers *only* the specific trade margin to the **Trading/Futures Account**.
   - Upon trade resolution, realized PnL and returned margin are instantly transferred back to the Funding Account.
2. **24-Hour Withdrawal Security Lock**:
   - Withdrawal requests initiated via the Admin UI enter a mandatory **24-hour lock status**.
   - The UI displays an active countdown. Funds cannot be released until the timer expires.
   - Upon timer completion, the system permits dispatch to one single, pre-registered whitelisted cryptocurrency address.

---

## Exchange Adapter & Network Integration

The exchange layer communicates through a unified, asynchronous interface:

```python
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any

class ExchangePort(ABC):
    @abstractmethod
    async def get_candles(self, symbol: str, interval: str, limit: int) -> List[Dict[str, Any]]: ...
    
    @abstractmethod
    async def get_order_book(self, symbol: str) -> Dict[str, Any]: ...
    
    @abstractmethod
    async def get_instrument_spec(self, symbol: str) -> Dict[str, Any]: ...
    
    @abstractmethod
    async def get_fee_rates(self, symbol: str) -> Dict[str, float]: ...
    
    @abstractmethod
    async def place_order(self, order_request: Dict[str, Any]) -> Dict[str, Any]: ...
    
    @abstractmethod
    async def amend_order(self, order_id: str, symbol: str, params: Dict[str, Any]) -> Dict[str, Any]: ...
    
    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...
    
    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]: ...
    
    @abstractmethod
    async def get_balances(self) -> Dict[str, float]: ...
    
    @abstractmethod
    async def set_leverage_and_margin(self, symbol: str, leverage: int, is_isolated: bool = True) -> bool: ...
    
    @abstractmethod
    async def transfer_internal(self, amount: float, from_account: str, to_account: str) -> bool: ...
```

### Bybit V5 Specifics (BTCUSDT Perpetual)
- **API Domain**: `api-demo.bybit.com` (Demo Sandbox), `api.bybit.com` (Live Production).
- **Tick Size**: `0.1 USDT`.
- **Minimum Order Quantity**: `0.001 BTC`.
- **Minimum Order Notional**: `100.0 USDT`. (Closing orders are exempt from minimum notional requirements).
- **Execution Rate Limits**: REST requests operate under exponential backoff with jitter upon receiving HTTP 429 errors.
- **Client Order ID (ClOrdId)**: Every order payload embeds a deterministic hash derived from:
  $$\text{ClOrdId} = \text{MD5}(\text{StrategyName} + \text{Symbol} + \text{CandleTimestamp} + \text{OrderType})$$
  Preventing duplicate execution across network interruptions.

---

## Admin Web Console & Operations Interface

The system incorporates an embedded **FastAPI + Jinja2** administration console secured by email/password HTTP Basic/JWT credentials.

```
+-----------------------------------------------------------------------------------+
|  TRADING BOT OPERATIONS CONSOLE                                  [Admin: @Black]  |
+-----------------------------------------------------------------------------------+
|  SYSTEM STATUS: [ RUNNING - DEMO MODE ]               ENGINE CLOCK: 11:42 UTC     |
|                                                                                   |
|  [ METRIC SUMMARY ]                                                               |
|  Wallet Capital: $128.50 USDT   | Day Start R: $1.50   | Trades Today: 1 / 2      |
|  Total Realized PnL: +4.2R      | Win Rate: 65.0%      | Demo Progress: 21 Trades |
|                                                                                   |
|  +-------------------------------------+  +------------------------------------+  |
|  | ACTIVE POSITION: BTCUSDT LONG       |  | DEMO QUALIFICATION GATE            |  |
|  | Size: 0.002 BTC (Entry: 99,655.0)   |  | Target: >= 20 Clean Trades         |  |
|  | Current Price: 100,210.0 (+1.25R)   |  | Completed: 21 Trades [PASS]        |  |
|  | Active Stop: 99,640.0 (Reduce-Only) |  | Demo Run Time: 26.4 Hours [PASS]   |  |
|  | Targets: TP1 @ 100,735 | TP2 @ ...   |  | Status: [ ENABLE LIVE TRADING ]    |  |
|  +-------------------------------------+  +------------------------------------+  |
|                                                                                   |
|  [ API KEY VAULT ]                                                                |
|  Demo Keys: [ BybitDemo_Key_**** ] [ Status: Connected ]                          |
|  Live Keys: [ BybitLive_Key_**** ] [ Status: Stored / Standby ]                   |
|                                                                                   |
|  [ EMERGENCY ACTIONS ]                                                            |
|  [ FLATTEN POSITION & KILL ENGINE ]                                               |
+-----------------------------------------------------------------------------------+
```

### Dashboard Core Views
1. **Live Performance View**: Visualizes wallet equity curves, running drawdowns, aggregate win/loss ratios, active trade positions, and live unhedged exposure.
2. **Credential Vault**:
   - Isolated inputs for **Bybit Demo** API Key & Secret.
   - Isolated inputs for **Bybit Live** API Key & Secret.
   - Keys are stored encrypted in the local SQLite database.
3. **Demo Strategy Sandbox & Live Lock Engine**:
   - Tracks strategy evaluation in demo mode.
   - Displays real-time progress toward the **24-hour elapsed time** and **≥ 20 clean trade executions**.
   - The "Enable Live Strategy" toggle remains physically disabled until the criteria evaluate to `True`.
4. **Emergency Controls**: One-click kill switch providing immediate, un-prompted position flattening and process halt.

---

## Monitoring, Alerts & Failure Handling

### Multi-Channel Alert Pipelines (Telegram & Email)
- **Engine Lifecycle Events**: Engine startup, shutdown, authentication failures, and network reconnection attempts.
- **Trade Execution Events**: Order fills, partial target scaling (TP1), breakeven stop amendments, and trade closures with net return expressed in $R$.
- **Setup Rejection Events**: Real-time notification of invalid setups, explicitly citing the failing gate (e.g., `GATE_5_NET_RR_INSUFFICIENT (1.68 < 2.0)`).
- **Capital Protection Events**: Triggered when equity breaches the minimum $\$10.00$ threshold.

### Daily Report Specification (Dispatched 02:00 Lagos / 01:00 UTC)
```
==================================================
DAILY BOT LEDGER REPORT: 2026-10-17
Trading Venue: Bybit BTCUSDT Perpetual
==================================================
Beginning Equity:       $102.40 USDT
Ending Equity:          $105.10 USDT
Daily Net Realized PnL: +$2.70 USDT (+1.80R)
Daily Trades Executed:  2 / 2 (Daily Limit Reached)

TRADE SUMMARY:
- Trade #1: BTCUSDT LONG | Result: +2.10R (TP1 Hit, Remainder Closed BE)
  Expected Fill: 99,655.0 | Actual Fill: 99,657.2 | Slippage: 2.2 pts
  Exchange Fees Incurred: $0.18 USDT
- Trade #2: BTCUSDT SHORT | Result: -1.00R (Stop Loss Hit)
  Expected Fill: 98,200.0 | Actual Fill: 98,201.0 | Slippage: 1.0 pts
  Exchange Fees Incurred: $0.11 USDT

NEXT DAY PARAMETERS:
Assigned Capital Tier:   Tier 2 ($50 - $449)
Base Trade Risk (R):     $1.50 USDT
Leverage Cap:            50x Isolated
==================================================
```

### Rejection Funnel Telemetry
The state store tracks where candidate setups terminate. Weekly aggregation reviews identify structural bottlenecks:
```
[Candidate Setups Evaluated: 142]
  ├── [Rejected] Ineligible Micro Trend / Bias: 68
  ├── [Rejected] 15m Candle Failed Sweep Close: 34
  ├── [Rejected] Microstructure 2m Trigger Timeout: 18
  ├── [Rejected] Gate 5: Net RR < 2.0: 12
  ├── [Rejected] Gate 2: Daily 2-Trade Quota Exceeded: 7
  └── [Authorized & Executed]: 3 Trades
```

---

## Deployment & Hosting Model (Fly.io)

```
                       [ Internet ]
                            |
                   (Static Public IPv4)
                            v
+-------------------------------------------------------+
| Fly.io Micro Container (1x Shared CPU, 512MB RAM)     |
|                                                       |
|  +-------------------------------------------------+  |
|  | Docker Environment (Python 3.10+ / asyncio)     |  |
|  |                                                 |  |
|  |  - FastAPI Web Server (Port 8080)               |  |
|  |  - Execution Engine Loop                        |  |
|  |  - Alert Dispatcher                             |  |
|  +-------------------------------------------------+  |
|                           |                           |
|                           v (POSIX File Lock)         |
|  +-------------------------------------------------+  |
|  | Persistent Encrypted Volume Mount (/data)       |  |
|  |  - trading_bot.db (SQLite WAL Mode)             |  |
|  +-------------------------------------------------+  |
+-------------------------------------------------------+
```

1. **Host Selection**: Fly.io container platform utilizing dedicated outbound static IP allocations. This ensures IP-whitelisted Bybit API keys remain active indefinitely and do not trigger Bybit's 90-day un-whitelisted key expiration policy.
2. **Data Persistence**: A persistent volume mounted to `/data` encapsulates the SQLite database, ensuring state, trade counters, and API vaults persist across container restarts.
3. **Environment Isolation**: Secrets (`TELEGRAM_BOT_TOKEN`, `SMTP_PASSWORD`, `APP_SECRET_KEY`) are managed via encrypted platform secrets, never committed to source repositories.

---

## Test-Driven Development (TDD) Acceptance Matrix

Implementation must proceed strictly from pure functions outward. No external exchange connections will be constructed until all pure-logic tests pass with **$\ge 95\%$ test coverage**.

| Phase | Module Under Test | Test Scenario / Input | Expected Asserted Output |
|---|---|---|---|
| **Phase 1** | `risk_schedule.py` | Capital $C = \$49.99, \$100.00, \$449.00, \$450.00, \$600.00$ | $R = \$1.00, \$1.50, \$4.50, \$5.00, \$6.00$ |
| **Phase 1** | `risk_schedule.py` | Capital $C = \$9.99, \$10.00, \$50.00, \$50.01$ | Disabled; Ceiling $100\times$; $100\times$; $50\times$ |
| **Phase 1** | `risk_schedule.py` | Trend Mismatch (1h Up, 15m Down) at $C = \$50.00$ | $R_{\text{trade}} = \$0.50$ (Half Risk) |
| **Phase 2** | `sizing_engine.py` | PRD Example: $E=99,655, S=99,640, s=(5, 10), R=\$1.00$ | $\text{Qty}=0.002\text{ BTC}, \text{Net RR}=2.06$ (Pass) |
| **Phase 2** | `sizing_engine.py` | Stop distance too wide for budget ($\text{Raw Qty} = 0.0008\text{ BTC}$) | Setup Rejected (`ERR_BELOW_MIN_LOT`) |
| **Phase 2** | `exit_quant.py` | Position size = 2, 3, 4, 5, 10 lots | Single 3R; Single 3R; 3+1; 4+1; 7+3 lots |
| **Phase 3** | `daily_cap.py` | 3rd entry signal received within same trading day | Setup Rejected (`ERR_DAILY_CAP_REACHED`) |
| **Phase 3** | `daily_cap.py` | Signals executed at 00:59 UTC and 01:01 UTC | Recorded in different trading days |
| **Phase 4** | `strategy_fsm.py` | 15m candle wicks below swing low, closes above | Setup Validated (`STATE_SWEEP_CONFIRMED`) |
| **Phase 4** | `strategy_fsm.py` | 15m candle closes strictly below swing low | Setup Cancelled (`STATE_INVALIDATED`) |
| **Phase 4** | `strategy_fsm.py` | 2 consecutive bullish 2m candles followed by bearish candle | No Entry Triggered; Counter Reset |
| **Phase 5** | `exit_policy.py` | Unrealized profit reaches 1.1R then retraces to 0.5R | Market Close Executed (Early Exit) |
| **Phase 5** | `exit_policy.py` | Price touches 1.5R | Stop Loss moved to Entry + Friction |
| **Phase 6** | `demo_gate.py` | Attempt live activation with 19 clean trades | Operation Blocked by UI Engine |
| **Phase 6** | `demo_gate.py` | Attempt live activation after 21 trades in 25 hours | Activation Authorized |

---

## Comprehensive Implementation Plan & Handoff Roadmap

```
+-----------------------------------------------------------------------------------+
|                           PHASED IMPLEMENTATION ROADMAP                           |
+-----------------------------------------------------------------------------------+
|  [PHASE 1] CORE QUANTITATIVE SUITE (TDD)                                          |
|  - Implement pure functions: risk_schedule, position_sizing, net_rr_calculator    |
|  - Write 100% coverage unit test suite                                            |
|                                                                                   |
|  [PHASE 2] TECHNICAL STRATEGY & STATE MACHINE                                    |
|  - Multi-timeframe candle parser (1h, 15m, 2m)                                     |
|  - Strategy 1 FSM: swing detection, sweep validator, 3-bar micro confirmation     |
|                                                                                   |
|  [PHASE 3] EXIT POLICY & ORDER LIFECYCLE                                          |
|  - Exit policy engine (TP1/TP2 quantization, breakeven mover, early-exit tracker) |
|  - FakeExchangeAdapter implementation for local deterministic backtesting         |
|                                                                                   |
|  [PHASE 4] STORAGE & LEDGER SUBSYSTEM                                             |
|  - SQLite database schemas, transaction ledger, rejection funnel logger          |
|  - 24-Hour withdrawal lock timer state engine                                     |
|                                                                                   |
|  [PHASE 5] FASTAPI WEB OPERATIONS DASHBOARD                                       |
|  - Administrative authentication, responsive monitoring layout                   |
|  - Bybit Demo vs Bybit Live credential management                                 |
|  - 24-Hour / 20-Trade Demo Qualification Gate Controller                         |
|  - Emergency Kill Switch integration                                              |
|                                                                                   |
|  [PHASE 6] BYBIT V5 EXCHANGE PORT                                                 |
|  - Bybit REST / WebSocket adapter implementation                                  |
|  - Connection resilience, client order ID de-duplication, exponential backoff     |
|                                                                                   |
|  [PHASE 7] ALERTING & DEPLOYMENT                                                  |
|  - Telegram & SMTP notification dispatchers                                       |
|  - Fly.io Dockerfile, volume mounting, static IP configuration                    |
+-----------------------------------------------------------------------------------+
```

> [!IMPORTANT]
> **Build Gate Notice**: As specified in the project governance rules, construction of Phase 1 code will begin only when you explicitly issue the instruction: **"Start building"**.
