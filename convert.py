import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import QuoteTickDataWrangler

SYMBOL = "BTC-20NOV26-74000-C"
RAW_CSV = Path("data/raw_deribit_quotes.csv")
CATALOG_DIR = Path("catalog")


def ns(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1e9)


# 1. Instrument definition
instrument = CryptoOption(
    instrument_id=InstrumentId(Symbol(SYMBOL), Venue("DERIBIT")),
    raw_symbol=Symbol(SYMBOL),
    underlying=BTC,
    quote_currency=BTC,
    settlement_currency=BTC,
    is_inverse=False,
    option_kind=OptionKind.CALL,
    strike_price=Price.from_str("74000.0"),
    activation_ns=ns(2026, 11, 1, 8),
    expiration_ns=ns(2026, 11, 20, 8),
    price_precision=4,
    size_precision=1,
    price_increment=Price.from_str("0.0005"),
    size_increment=Quantity.from_str("0.1"),
    multiplier=Quantity.from_int(1),
    lot_size=Quantity.from_str("0.1"),
    maker_fee=Decimal("0.0003"),
    taker_fee=Decimal("0.0003"),
    ts_event=ns(2026, 11, 1, 8),
    ts_init=ns(2026, 11, 1, 8),
)

# 2. Load raw CSV (index must be a UTC timestamp)
df = pd.read_csv(RAW_CSV, parse_dates=["timestamp"], index_col="timestamp")
df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index

# 3. Convert to QuoteTicks
ticks = QuoteTickDataWrangler(instrument).process(df)

# 4. Write to catalog
shutil.rmtree(CATALOG_DIR, ignore_errors=True) 
CATALOG_DIR.mkdir(exist_ok=True)
catalog = ParquetDataCatalog(CATALOG_DIR)
catalog.write_data([instrument])
catalog.write_data(ticks)

# Quick check
print(len(catalog.quote_ticks()), "ticks written")