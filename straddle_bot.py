import os
import re
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from nautilus_trader.adapters.deribit import (
    DERIBIT,
    DERIBIT_VENUE,
    DeribitDataClientConfig,
    DeribitExecClientConfig,
    DeribitExecutionClient,
    DeribitLiveDataClientFactory,
    DeribitLiveExecClientFactory,
    DeribitProductType,
    get_cached_deribit_http_client,
    get_cached_deribit_instrument_provider,
)
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.trading.strategy import Strategy

try:
    from nautilus_trader.core.nautilus_pyo3 import DeribitEnvironment
except ImportError:
    from nautilus_trader.adapters.deribit.config import DeribitEnvironment

load_dotenv()

OPTION_RE = re.compile(r"^BTC-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])$")
MIN_HOURS_TO_EXPIRY = 24
ORDER_QTY = 0.1           # BTC per leg (Deribit minimum)
SL_PCT = 0.01             # stop-loss: combined buyback ask UP 1% vs baseline
TP_PCT = 0.02             # take-profit: combined buyback ask DOWN 2% vs baseline
ENTRY_BASIS = "ask"       # "ask": baseline = combined ask when both legs fill
                          # "fill": baseline = combined price actually sold at
ENTRY_TIMEOUT_SECS = 10   # both legs must fill within this time
MAX_EXIT_RETRIES = 5
EPS = 1e-9


# ======================================================================
# Workaround: nautilus_trader 1.231.0's Deribit exec client drops live
# OrderFilled messages ("unhandled message type"). We forward them.
# ======================================================================
class PatchedDeribitExecutionClient(DeribitExecutionClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._seen_fills: set = set()

    def _handle_ws_message(self, msg) -> None:
        if isinstance(msg, nautilus_pyo3.OrderFilled):
            try:
                key = (msg.client_order_id.value, str(msg.trade_id))
                if key in self._seen_fills:  # ignore duplicate delivery
                    return
                self._seen_fills.add(key)
                if self._cache.order(ClientOrderId(msg.client_order_id.value)) is None:
                    return
                self._send_order_event(OrderFilled.from_dict(msg.to_dict()))
            except Exception as e:
                self._log.exception("Error handling live OrderFilled", e)
            return
        super()._handle_ws_message(msg)


class PatchedExecFactory(DeribitLiveExecClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):  # type: ignore
        http_client = get_cached_deribit_http_client(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url_http,
            environment=config.environment or DeribitEnvironment.MAINNET,
            timeout_secs=config.http_timeout_secs,
            max_retries=config.max_retries,
            retry_delay_ms=config.retry_delay_initial_ms,
            retry_delay_max_ms=config.retry_delay_max_ms,
            proxy_url=config.proxy_url,
        )
        provider = get_cached_deribit_instrument_provider(
            client=http_client,
            product_types=config.product_types,
            config=config.instrument_provider,
        )
        return PatchedDeribitExecutionClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )


# ======================================================================
# Strategy (Level 1 & Level 2)
# ======================================================================
def parse_option(symbol: str):
    m = OPTION_RE.match(symbol)
    if not m:
        return None
    expiry = datetime.strptime(m.group(1), "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
    return expiry, int(m.group(2)), m.group(3)


class ShortStraddle(Strategy):
    """Level 1 & 2: short ATM straddle with automated stop-loss / take-profit."""

    def __init__(self) -> None:
        super().__init__()
        self.chain = {}
        self.expiry = None
        self.ref_price = None
        self.atm_selected = False
        self.call_id = None
        self.put_id = None
        self.legs = []
        self.ticks = {}
        self.phase = "WAIT"          # WAIT -> ENTERING -> OPEN -> EXITING -> DONE
        self.entry_ts = 0.0
        self.entry_order_ids = []
        self.sold = {}               # iid -> [qty, notional]
        self.bought = {}             # iid -> [qty, notional]
        self.pending_exit = {}       # iid -> qty of buy-back orders in flight
        self.exit_orders = {}        # client_order_id -> [iid, remaining_qty]
        self.exit_retries = 0
        self.fees = 0.0
        self.baseline = None
        self._last_status = 0.0

    # ---------------- startup: expiry, index, ATM ----------------
    def on_start(self) -> None:
        for inst in self.cache.instruments(venue=DERIBIT_VENUE):
            parsed = parse_option(inst.id.symbol.value)
            if parsed is None:
                continue
            expiry, strike, kind = parsed
            self.chain.setdefault((expiry, strike), {})[kind] = inst.id

        now = self.clock.utc_now()
        expiries = sorted({e for (e, _) in self.chain})
        valid = [e for e in expiries if (e - now).total_seconds() / 3600 >= MIN_HOURS_TO_EXPIRY]
        if not valid:
            self.log.error("No expiry with enough time left")
            return
        self.expiry = valid[0]
        self.log.info(f"Chosen expiry: {self.expiry:%d-%b-%Y %H:%M} UTC")

        strikes = sorted(
            k for (e, k), legs in self.chain.items()
            if e == self.expiry and "C" in legs and "P" in legs
        )
        probe_id = self.chain[(self.expiry, strikes[len(strikes) // 2])]["C"]
        self.subscribe_index_prices(probe_id)

    def on_index_price(self, index_price) -> None:
        self.ref_price = float(index_price.value)
        if not self.atm_selected:
            self._select_atm()

    def _select_atm(self) -> None:
        self.atm_selected = True
        strikes = [
            k for (e, k), legs in self.chain.items()
            if e == self.expiry and "C" in legs and "P" in legs
        ]
        atm = min(strikes, key=lambda k: (abs(k - self.ref_price), k))
        legs = self.chain[(self.expiry, atm)]
        self.call_id, self.put_id = legs["C"], legs["P"]
        self.legs = [self.call_id, self.put_id]
        self.log.info(f"Index {self.ref_price:.2f} -> ATM strike {atm}: {self.call_id} / {self.put_id}")
        self.subscribe_quote_ticks(self.call_id)
        self.subscribe_quote_ticks(self.put_id)

    # ---------------- market data drives the state machine ----------------
    def on_quote_tick(self, tick) -> None:
        self.ticks[tick.instrument_id] = tick
        if self.phase == "WAIT":
            self._try_enter()
        elif self.phase == "ENTERING":
            if time.monotonic() - self.entry_ts > ENTRY_TIMEOUT_SECS:
                self.log.error("ENTRY TIMEOUT - both legs did not fill")
                self._begin_exit("entry timeout")
        elif self.phase == "OPEN":
            self._check_exit_rules()
        elif self.phase == "EXITING":
            self._send_exits()   # idempotent: only sends what is still missing
        self._log_status()

    # ---------------- entry ----------------
    def _try_enter(self) -> None:
        c, p = self.ticks.get(self.call_id), self.ticks.get(self.put_id)
        if c is None or p is None:
            return
        if float(c.bid_price) <= 0 or float(p.bid_price) <= 0:
            return

        if self.cache.positions_open(venue=DERIBIT_VENUE):
            self.log.warning("Open positions already exist - NOT entering. Close them first.")
            self.phase = "DONE"
            return

        self.phase = "ENTERING"      # set BEFORE submitting so we can never enter twice
        self.entry_ts = time.monotonic()
        for tick in (c, p):
            inst = self.cache.instrument(tick.instrument_id)
            order = self.order_factory.limit(
                instrument_id=tick.instrument_id,
                order_side=OrderSide.SELL,
                quantity=inst.make_qty(ORDER_QTY),
                price=inst.make_price(float(tick.bid_price)),
                time_in_force=TimeInForce.GTC,
            )
            self.entry_order_ids.append(order.client_order_id)
            self.log.info(f"SUBMITTING SELL {order.instrument_id} qty={ORDER_QTY} @ {float(tick.bid_price)}")
            self.submit_order(order)

    def _net_short(self, iid) -> float:
        return round(self.sold.get(iid, [0.0, 0.0])[0] - self.bought.get(iid, [0.0, 0.0])[0], 8)

    def _on_straddle_open(self) -> None:
        sold_total = sum(self.sold[i][1] / self.sold[i][0] for i in self.legs)
        ask_total = sum(float(self.ticks[i].ask_price) for i in self.legs)
        self.baseline = ask_total if ENTRY_BASIS == "ask" else sold_total
        self.phase = "OPEN"
        self.log.info(
            f"BOTH LEGS FILLED - straddle OPEN | sold={sold_total:.4f} ask_now={ask_total:.4f} "
            f"baseline({ENTRY_BASIS})={self.baseline:.4f} | "
            f"STOP when ask >= {self.baseline * (1 + SL_PCT):.5f}, "
            f"TAKE-PROFIT when ask <= {self.baseline * (1 - TP_PCT):.5f}"
        )

    # ---------------- stop-loss / take-profit ----------------
    def _check_exit_rules(self) -> None:
        ticks = [self.ticks.get(i) for i in self.legs]
        if any(t is None for t in ticks):
            return
        asks = [float(t.ask_price) for t in ticks]
        if min(asks) <= 0:
            return                               # one-sided book, don't trade on it
        pct = (sum(asks) - self.baseline) / self.baseline
        if pct >= SL_PCT - EPS:
            self._begin_exit(f"STOP-LOSS ({pct:+.2%})")
        elif pct <= -TP_PCT + EPS:
            self._begin_exit(f"TAKE-PROFIT ({pct:+.2%})")

    def _begin_exit(self, reason: str) -> None:
        if self.phase in ("EXITING", "DONE"):    # duplicate trigger -> ignore
            return
        self.phase = "EXITING"
        self.log.warning(f"EXIT TRIGGERED: {reason}")
        for cid in self.entry_order_ids:         # cancel entry orders still resting
            o = self.cache.order(cid)
            if o is not None and not o.is_closed and o.venue_order_id is not None:
                self.cancel_order(o)
        self._send_exits()
        self._maybe_finish()

    def _send_exits(self) -> None:
        if self.exit_retries > MAX_EXIT_RETRIES:
            self.log.error("Too many failed exit orders - MANUAL INTERVENTION NEEDED")
            return
        for iid in self.legs:
            need = round(self._net_short(iid) - self.pending_exit.get(iid, 0.0), 8)
            if need <= EPS:
                continue
            tick = self.ticks.get(iid)
            ask = float(tick.ask_price) if tick else 0.0
            if ask <= 0:
                continue                         # no ask yet, retried on the next quote
            inst = self.cache.instrument(iid)
            tick_size = 0.0005 if ask >= 0.005 else 0.0001
            px = round(ask + tick_size, 4)
            order = self.order_factory.limit(
                instrument_id=iid,
                order_side=OrderSide.BUY,
                quantity=inst.make_qty(need),
                price=inst.make_price(px),
                time_in_force=TimeInForce.GTC,
                reduce_only=True,                # can never flip us into a long position
            )
            self.exit_orders[order.client_order_id] = [iid, need]
            self.pending_exit[iid] = round(self.pending_exit.get(iid, 0.0) + need, 8)
            self.log.info(f"SUBMITTING BUY-BACK {iid} qty={need} @ {px:.4f} (ask {ask:.4f})")
            self.submit_order(order)

    def _exit_failed(self, cid, why) -> bool:
        info = self.exit_orders.pop(cid, None)
        if info is None:
            return False                         # not an exit order
        iid, remaining = info
        self.pending_exit[iid] = max(0.0, round(self.pending_exit.get(iid, 0.0) - remaining, 8))
        self.exit_retries += 1
        self.log.error(f"EXIT ORDER FAILED ({why}) - retry {self.exit_retries}/{MAX_EXIT_RETRIES}")
        if self.phase == "EXITING":
            self._send_exits()
        return True

    def _maybe_finish(self) -> None:
        if self.phase != "EXITING":
            return
        if all(self._net_short(i) <= EPS for i in self.legs):
            self.phase = "DONE"
            sold_n = sum(v[1] for v in self.sold.values())
            bought_n = sum(v[1] for v in self.bought.values())
            self.log.info(
                f"STRADDLE CLOSED | sold={sold_n:.5f} bought back={bought_n:.5f} "
                f"fees={self.fees:.5f} NET P&L={sold_n - bought_n - self.fees:+.5f} BTC | "
                f"press Ctrl+C to stop"
            )

    # ---------------- order events ----------------
    def on_order_submitted(self, event) -> None:
        self.log.info(f"SUBMITTED  {event.instrument_id} {event.client_order_id}")

    def on_order_accepted(self, event) -> None:
        self.log.info(f"ACCEPTED   {event.instrument_id} venue_order_id={event.venue_order_id}")

    def on_order_rejected(self, event) -> None:
        self.log.error(f"REJECTED   {event.instrument_id}: {event.reason}")
        if not self._exit_failed(event.client_order_id, event.reason) and self.phase == "ENTERING":
            self._begin_exit("entry order rejected")

    def on_order_denied(self, event) -> None:
        self.log.error(f"DENIED     {event.instrument_id}: {event.reason}")
        if not self._exit_failed(event.client_order_id, event.reason) and self.phase == "ENTERING":
            self._begin_exit("entry order denied")

    def on_order_canceled(self, event) -> None:
        self.log.info(f"CANCELED   {event.instrument_id} {event.client_order_id}")
        self._exit_failed(event.client_order_id, "canceled")

    @staticmethod
    def _money(m) -> float:
        try:
            return float(m.as_double())
        except Exception:
            return 0.0

    def on_order_filled(self, event) -> None:
        iid = event.instrument_id
        q, px = float(event.last_qty), float(event.last_px)
        is_sell = event.order_side == OrderSide.SELL
        self.fees += self._money(event.commission)
        acc = (self.sold if is_sell else self.bought).setdefault(iid, [0.0, 0.0])
        acc[0] += q
        acc[1] += q * px
        self.log.info(f"FILLED     {iid} {'SELL' if is_sell else 'BUY'} qty={q} px={px}")

        if is_sell:
            if self.phase == "ENTERING":
                done = [i for i in self.legs if self._net_short(i) >= ORDER_QTY - EPS]
                if len(done) == 2:
                    self._on_straddle_open()
                else:
                    self.log.warning("LEGGING: only one leg fully filled so far")
            elif self.phase == "EXITING":
                self.log.warning("Late entry fill while exiting - closing it too")
                self._send_exits()
        else:
            self.pending_exit[iid] = max(0.0, round(self.pending_exit.get(iid, 0.0) - q, 8))
            info = self.exit_orders.get(event.client_order_id)
            if info:
                info[1] = round(info[1] - q, 8)
                if info[1] <= EPS:
                    del self.exit_orders[event.client_order_id]
            self._maybe_finish()

    # ---------------- position events ----------------
    def on_position_opened(self, event) -> None:
        self.log.info(f"POSITION OPENED  {event.instrument_id} qty={event.quantity}")

    def on_position_changed(self, event) -> None:
        self.log.info(f"POSITION CHANGED {event.instrument_id} qty={event.quantity}")

    def on_position_closed(self, event) -> None:
        self.log.info(f"POSITION CLOSED  {event.instrument_id}")

    # ---------------- monitoring ----------------
    def _log_status(self) -> None:
        if self.phase != "OPEN" or time.monotonic() - self._last_status < 5.0:
            return
        ticks = [self.ticks.get(i) for i in self.legs]
        if any(t is None for t in ticks):
            return
        self._last_status = time.monotonic()
        total = sum(float(t.ask_price) for t in ticks)
        pct = (total - self.baseline) / self.baseline
        self.log.info(
            f"STATUS OPEN ask_total={total:.4f} baseline={self.baseline:.4f} ({pct:+.2%}) "
            f"| SL>={self.baseline * (1 + SL_PCT):.5f} TP<={self.baseline * (1 - TP_PCT):.5f}"
        )

    def on_stop(self) -> None:
        if self.phase == "ENTERING":
            for cid in self.entry_order_ids:
                o = self.cache.order(cid)
                if o is not None and not o.is_closed and o.venue_order_id is not None:
                    self.cancel_order(o)
        if self.phase in ("OPEN", "EXITING"):
            self.log.warning("Stopping while positions may still be OPEN on Deribit!")
        for iid in self.legs:
            self.unsubscribe_quote_ticks(iid)


# ======================================================================
def main() -> None:
    api_key = os.getenv("DERIBIT_TESTNET_API_KEY")
    api_secret = os.getenv("DERIBIT_TESTNET_API_SECRET")
    assert api_key, "Missing DERIBIT_TESTNET_API_KEY in .env"
    assert api_secret, "Missing DERIBIT_TESTNET_API_SECRET in .env"

    provider = InstrumentProviderConfig(load_all=True)
    config = TradingNodeConfig(
        trader_id="STRADDLE-001",
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(reconciliation_lookback_mins=1),
        data_clients={
            DERIBIT: DeribitDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                product_types=(DeribitProductType.OPTION,),
                environment=DeribitEnvironment.TESTNET,
                instrument_provider=provider,
            ),
        },
        exec_clients={
            DERIBIT: DeribitExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                product_types=(DeribitProductType.OPTION,),
                environment=DeribitEnvironment.TESTNET,
                instrument_provider=provider,
            ),
        },
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(DERIBIT, DeribitLiveDataClientFactory)
    node.add_exec_client_factory(DERIBIT, PatchedExecFactory)
    node.trader.add_strategy(ShortStraddle())
    node.build()

    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
    # raw dat to catalog (nautilus) ,take any script from nautilus writtten in python ,and convert it into rust 