# Bitcoin Short Straddle Algorithmic Trading Bot

An event-driven options trading system built on [NautilusTrader](https://nautilus-trader.io/) implementing an automated Bitcoin (BTC) short straddle strategy on Deribit Testnet.

---

## Overview

The strategy automates the complete lifecycle of a delta-neutral short straddle:

1. **Dynamic Strike Discovery:** Retrieves available Deribit option instruments, filters for valid expiries (at least 24 hours remaining), and selects the At-The-Money (ATM) strike using the live BTC perpetual mid-price.
2. **Synchronized Entry:** Submits simultaneous Limit Sell orders for both ATM Call and ATM Put legs at their current top-of-book bid prices.
3. **Execution Safety & Legging Protection:** Enforces a strict execution timeout of 10 seconds to limit naked directional exposure if one leg fails to fill.
4. **Real-Time Risk Management:** Continuously monitors the combined option buyback cost against an entry ask baseline, executing automated Stop-Loss (+1%) and Take-Profit (-2%) exits using marketable limit orders with `reduce_only=True`.

---

## Strategy Logic & State Machine

The bot uses a deterministic five-stage state machine to manage the trading lifecycle and prevent duplicate executions.

```text
       [ WAIT ]
           |
           | BTC perpetual quote received
           | ATM Call/Put bids available
           v
       [ ENTERING ]
           |
           | Both legs filled
           v
        [ OPEN ]
           |
           | Stop-Loss (+1%) / Take-Profit (-2%)
           v
       [ EXITING ]
           |
           | Net short exposure == 0
           v
        [ DONE ]
```

If an entry times out or an order is rejected, the bot initiates its exit-handling process to cancel outstanding entry orders and close any executed exposure.

### Entry Baseline and Exit Conditions

- **Baseline Metric (`ENTRY_BASIS = "ask"`):** Calculated as `Call_Ask + Put_Ask` when both entry legs have filled. This provides a consistent reference for monitoring the estimated buyback cost.

- **Stop-Loss (+1%):** Triggers when:

  `Combined_Ask >= Baseline * (1 + 0.01)`

- **Take-Profit (-2%):** Triggers when:

  `Combined_Ask <= Baseline * (1 - 0.02)`

---

## Architecture & Workarounds

### Custom Execution Adapter (`PatchedDeribitExecutionClient`)

The NautilusTrader 1.231.0 Deribit execution client was dropping certain raw `nautilus_pyo3.OrderFilled` WebSocket events as unhandled message types.

To address this without modifying the core dependency, the system subclasses `DeribitExecutionClient` and overrides `_handle_ws_message`.

The custom adapter:

- Intercepts `nautilus_pyo3.OrderFilled` messages directly from the WebSocket feed.
- Maintains a deduplication cache (`_seen_fills`) keyed by `(client_order_id, trade_id)`.
- Reconstructs and dispatches standard NautilusTrader `OrderFilled` events to the internal event-processing system.

A custom `PatchedExecFactory` is used to initialize this execution adapter.

---

## Project Structure

```text
MegaServe Assignment/
├── straddle_bot.py       # Main trading node, strategy implementation and adapter patch
├── .env                  # API keys and environment configuration (git-ignored)
├── .env.example          # Environment variable template
├── .gitignore            # Files excluded from Git
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation
```

---

## Getting Started

### 1. Prerequisites

- Python 3.12+
- An active [Deribit Testnet](https://test.deribit.com/) account
- Generated Deribit Testnet API credentials

### 2. Environment Setup

Create and activate a virtual environment:

```bash
python3.12 -m venv venv
source venv/bin/activate
```

Install the required packages:

```bash
pip install -r requirements.txt
```

Alternatively, install them manually:

```bash
pip install nautilus_trader==1.231.0 python-dotenv
```

### 3. API Configuration

Create a `.env` file in the project root:

```bash
touch .env
```

Add your Deribit Testnet credentials:

```env
DERIBIT_TESTNET_API_KEY=your_deribit_testnet_client_id
DERIBIT_TESTNET_API_SECRET=your_deribit_testnet_client_secret
```

**Important:** Never commit your actual `.env` file or expose your API credentials publicly.

---

## Running the Bot

Execute the strategy script inside the activated virtual environment:

```bash
python straddle_bot.py
```

### Expected Lifecycle Logs

The following are illustrative examples of the expected bot lifecycle.

**1. Initialization**

```text
[INFO] STRADDLE-001.TradingNode: Initialized in 20ms
[INFO] STRADDLE-001.DeribitInstrumentProvider: Loaded instruments
```

**2. ATM Strike Selection**

```text
[INFO] STRADDLE-001.ShortStraddle: Chosen expiry: 13-Nov-2026 08:00 UTC
[INFO] STRADDLE-001.ShortStraddle: Perpetual mid 89420.50 -> ATM strike 89500: BTC-13NOV26-89500-C.DERIBIT / BTC-13NOV26-89500-P.DERIBIT
```

**3. Order Entry**

```text
[INFO] STRADDLE-001.ShortStraddle: SUBMITTING SELL BTC-13NOV26-89500-C.DERIBIT qty=0.1 @ 0.0145
[INFO] STRADDLE-001.ShortStraddle: SUBMITTING SELL BTC-13NOV26-89500-P.DERIBIT qty=0.1 @ 0.0150
```

**4. Straddle Active (Status Logging)**

```text
[INFO] STRADDLE-001.ShortStraddle: BOTH LEGS FILLED - straddle OPEN | baseline(ask)=0.0300
[INFO] STRADDLE-001.ShortStraddle: STATUS OPEN ask_total=0.0298 baseline=0.0300 (-0.67%) | SL>=0.03030 TP<=0.02940
```

**5. Exit Execution**

```text
[INFO] STRADDLE-001.ShortStraddle: EXIT TRIGGERED: TAKE-PROFIT (-2.10%)
[INFO] STRADDLE-001.ShortStraddle: SUBMITTING BUY-BACK BTC-13NOV26-89500-C.DERIBIT qty=0.1 @ 0.0140
[INFO] STRADDLE-001.ShortStraddle: SUBMITTING BUY-BACK BTC-13NOV26-89500-P.DERIBIT qty=0.1 @ 0.0154
[INFO] STRADDLE-001.ShortStraddle: STRADDLE CLOSED | NET P&L=+0.00060 BTC
```

---

## Edge Case Handling

- **Exchange Maintenance (`code=10019: locked_by_admin`):** Deribit may temporarily restrict trading during maintenance. The bot handles order rejections through its order-event callbacks and initiates the appropriate exit-handling process.

- **Partial Leg Fills:** If both legs do not fill within `ENTRY_TIMEOUT_SECS` (10 seconds), outstanding entry orders are cancelled and any executed exposure is handled through `_begin_exit()`.

- **Exit Order Resilience:** Buyback orders are submitted at `Ask + 1 tick` using `reduce_only=True` to prevent accidental position inversion. Failed exit orders are retried up to `MAX_EXIT_RETRIES = 5`. If retries are exhausted, the bot logs that manual intervention is required.

---

## Risk Management Parameters

| Parameter | Value | Description |
|---|---:|---|
| `MIN_HOURS_TO_EXPIRY` | 24 | Minimum time remaining until expiry |
| `ORDER_QTY` | 0.1 BTC | Quantity per option leg |
| `SL_PCT` | 0.01 (1%) | Stop-loss threshold |
| `TP_PCT` | 0.02 (2%) | Take-profit threshold |
| `ENTRY_TIMEOUT_SECS` | 10 | Maximum entry synchronization time |
| `MAX_EXIT_RETRIES` | 5 | Maximum exit order retries |
| `ENTRY_BASIS` | `"ask"` | Combined ask-price baseline |

---

**Note:** This implementation is designed for Deribit Testnet. The displayed execution logs are illustrative; actual prices, fills, fees, and P&L depend on live testnet market conditions.
