"""Python version of the Rust engine_ema_cross example.

Same setup as the Rust file: synthetic AUD/USD quotes, simulated venue SIM,
1,000,000 USD, trade size 100,000, fast EMA 10, slow EMA 20, hedging OMS.
Run with: python ema_cross_python.py
"""
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.strategy import Strategy

VENUE = "SIM"
STARTING_BALANCE = 1_000_000
TRADE_SIZE = 100_000
EMA_FAST_PERIOD = 10
EMA_SLOW_PERIOD = 20


class EmaCrossConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    trade_size: Decimal
    fast_period: int = 10
    slow_period: int = 20


class EmaCross(Strategy):
    """Buys when the fast EMA crosses above the slow EMA, sells on the reverse."""

    def __init__(self, config: EmaCrossConfig) -> None:
        super().__init__(config)
        self.fast_ema = ExponentialMovingAverage(config.fast_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_period)
        self.instrument = None
        self.prev_fast_above = None  # was fast EMA above slow EMA on the last quote?

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        mid = (tick.bid_price.as_double() + tick.ask_price.as_double()) / 2.0
        self.fast_ema.update_raw(mid)
        self.slow_ema.update_raw(mid)

        if not (self.fast_ema.initialized and self.slow_ema.initialized):
            return

        fast_above = self.fast_ema.value > self.slow_ema.value
        if self.prev_fast_above is not None and fast_above != self.prev_fast_above:
            side = OrderSide.BUY if fast_above else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=side,
                quantity=self.instrument.make_qty(self.config.trade_size),
            )
            self.submit_order(order)
        self.prev_fast_above = fast_above

    def on_stop(self) -> None:
        self.unsubscribe_quote_ticks(self.config.instrument_id)


def generate_quotes(instrument_id: InstrumentId) -> list[QuoteTick]:
    """Same synthetic data as the Rust version: 25 flat ticks, then 6 up/down cycles."""
    spread = 0.00020
    base_ts = 1_735_689_600_000_000_000  # 2025-01-01T00:00:00Z
    interval = 1_000_000_000  # 1 second
    quotes: list[QuoteTick] = []

    def add(mid: float) -> None:
        ts = base_ts + len(quotes) * interval
        quotes.append(
            QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price.from_str(f"{mid:.5f}"),
                ask_price=Price.from_str(f"{mid + spread:.5f}"),
                bid_size=Quantity.from_int(100_000),
                ask_size=Quantity.from_int(100_000),
                ts_event=ts,
                ts_init=ts,
            )
        )

    for _ in range(25):  # flat start
        add(0.65000)

    for cycle in range(6):
        base = 0.65000 + cycle * 0.00100
        for i in range(40):  # ramp up
            add(base + i * 0.00050)
        peak = base + 39 * 0.00050
        for i in range(80):  # ramp down
            add(peak - i * 0.00050)

    return quotes


def main() -> None:
    engine = BacktestEngine(config=BacktestEngineConfig())

    engine.add_venue(
        venue=Venue(VENUE),
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(STARTING_BALANCE, USD)],
        book_type=BookType.L1_MBP,
    )

    instrument = TestInstrumentProvider.default_fx_ccy("AUD/USD", Venue(VENUE))
    engine.add_instrument(instrument)

    engine.add_strategy(
        EmaCross(
            EmaCrossConfig(
                instrument_id=instrument.id,
                trade_size=Decimal(TRADE_SIZE),
                fast_period=EMA_FAST_PERIOD,
                slow_period=EMA_SLOW_PERIOD,
            )
        )
    )

    quotes = generate_quotes(instrument.id)
    engine.add_data(quotes)
    engine.run()

    print(f"Quotes: {len(quotes)}")
    print(f"Orders: {len(engine.cache.orders())}")
    print(f"Positions: {len(engine.cache.positions())}")
    engine.dispose()


if __name__ == "__main__":
    main()