# NautilusTrader Assignment: Task 1 and Task 2

This part of the repo covers two tasks built on [NautilusTrader](https://nautilustrader.io):

1. **Task 1:** convert raw market data (CSV) into a NautilusTrader Parquet data catalog.
2. **Task 2:** take a Python NautilusTrader script and convert it to Rust.

## Project layout

```
.
├── convert.py              # Task 1: raw CSV -> NautilusTrader catalog
├── data/
│   └── raw_deribit_quotes.csv   # Task 1: sample raw quote data
├── ema_cross_python.py     # Task 2: Python EMA-crossover backtest
└── ema_cross_rust/         # Task 2: the same backtest in Rust
    ├── Cargo.toml
    ├── Cargo.lock
    └── src/main.rs
```

The generated `catalog/` folder is not committed. Running `convert.py` recreates it.

---

## Task 1: Raw data to NautilusTrader catalog

### What it does

`convert.py` takes a raw CSV of option quotes and stores it in a NautilusTrader `ParquetDataCatalog`:

1. Defines the instrument, a Deribit BTC call option, `BTC-20NOV26-74000-C.DERIBIT` (strike 74,000, expiry 20 Nov 2026).
2. Reads `data/raw_deribit_quotes.csv` into a pandas DataFrame with a UTC timestamp index.
3. Converts each row into a `QuoteTick` with `QuoteTickDataWrangler`.
4. Writes the instrument definition and the quote ticks into the catalog.
5. Reads the ticks back from the catalog and prints the count as a check.

### Input data

The raw data is **synthetic sample data**: 10,000 quotes, 100 ms apart, starting 2026-11-13 08:00 UTC. The script works the same way with a real export as long as the CSV has these columns:

```
timestamp, bid_price, ask_price, bid_size, ask_size
```

### Output

```
catalog/
└── data/
    ├── crypto_option/   # instrument definition (what the contract is)
    └── quote_tick/      # 10,000 quote ticks (bid/ask over time)
```

The instrument and the ticks are stored separately and linked by instrument ID. A backtest later loads both: the instrument says what is being traded, and the ticks supply the prices. The script deletes and rebuilds the catalog on every run, so it can be run repeatedly with the same result.

### Run

```
pip install nautilus_trader pandas
python convert.py
```

Expected: the script prints `10000 ticks written`.

---

## Task 2: Python script converted to Rust

### What it does

The chosen script is an **EMA-crossover strategy backtest**. It runs on 745 synthetic AUD/USD quotes (25 flat ticks, then 6 up/down price cycles) on a simulated venue `SIM` with 1,000,000 USD, a trade size of 100,000, a fast EMA of 10 and a slow EMA of 20, using the hedging OMS. The strategy buys when the fast EMA crosses above the slow EMA and sells on the reverse.

The Python and Rust versions use the same data and settings, so their results can be compared.

### Python version

```
python ema_cross_python.py
```

### Rust version

NautilusTrader's core is written in Rust, and the Python package is a layer on top of it, so the same backtest can be written in either language.

The Rust project depends on the NautilusTrader source code through local paths, so the Nautilus repo must be cloned **next to** this project:

```
# from the folder that contains ema_cross_rust/
git clone https://github.com/nautechsystems/nautilus_trader.git

# install Rust if needed: https://rustup.rs
cd ema_cross_rust
cargo run
```

The first build compiles many crates and takes a while. Later runs start quickly. It was built and run against NautilusTrader 0.69.0. The Rust API is under active development, so other versions may need changes.

### Expected result (both versions)

- 745 quotes processed
- 12 orders
- 12 positions

The summary statistics print `NaN` for some values, which is expected here. With the hedging OMS each order opens a new position and none are closed, so there are no winning or losing trades to average.

### How the Python code maps to Rust

| Step | Python | Rust |
|---|---|---|
| Engine | `BacktestEngine(config=BacktestEngineConfig())` | `BacktestEngine::new(BacktestEngineConfig::default())?` |
| Venue | `engine.add_venue(venue=..., oms_type=..., ...)` | `engine.add_venue(SimulatedVenueConfig::builder()...build()?)?` |
| Instrument | `TestInstrumentProvider.default_fx_ccy("AUD/USD", ...)` | `InstrumentAny::CurrencyPair(audusd_sim())` |
| Data | `generate_quotes` returns `list[QuoteTick]` | `generate_quotes` returns `Vec<Data>` |
| Strategy | `EmaCross` class written in the script | built-in `EmaCross::new(...)` from `nautilus_trading` |
| Run | `engine.run()` | `engine.run(None, None, None, false)?` |

### Notes

- The Rust file is adapted from NautilusTrader's official `engine_ema_cross.rs` example and keeps its license header (LGPL-3.0).
- In Python the strategy class is written in the script (EMAs on the quote mid price, trading on each crossover). In Rust the strategy is the built-in `EmaCross` from the `nautilus_trading` crate. The two produce the same number of orders and positions on this data, but the Python strategy is a separate implementation, not a line-by-line port of the Rust one.
- All market data in both tasks is synthetic and used only to demonstrate the pipeline.
