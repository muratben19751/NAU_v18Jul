"""NautilusTrader BacktestEngine wrapper.

Builds a fresh engine per call. Runs the chosen strategy over Bybit BTCUSDT
bars and extracts summary metrics + equity curve.

Wiki References
---------------
See: [[backtest_node]], [[backtesting_guide]], [[environment_contexts]], [[data_wranglers]], [[precision_modes]], [[portfolio]], [[v1_to_v2_migration_lessons]], [[index_backtest_via_equity_proxy]], [[nau_deepr_toplu_sertlestirme_2026_08]]

Low-level API path; matches the [[backtesting_guide]] "choose BacktestEngine if:" list. `_bars_from_df` is a helper written after `BarDataWrangler.process` was removed in v2 — see [[data_wranglers]] v1→v2 section. Sharpe nan bug: see [[portfolio]] and [[v1_to_v2_migration_lessons]].

Instrument precision is a correctness surface, not a formatting detail: an
unlisted sub-cent symbol used to get the 2-decimal Bybit default and backtest
an all-zero OHLC series without a single error. `price_precision_for` /
`price_scale_hint` derive it from the data and `_bars_from_df` raises
`PricePrecisionError` rather than let a positive price round to 0 — the
sibling of the Equity `size_precision=0` trap in
[[index_backtest_via_equity_proxy]].

`_metrics()`'s three `except Exception: pass` blocks (duration/commission/
slippage) now log a warning instead of silently zeroing/NaN-ing a metric
(2026-08-08 DeepR finding) — the slippage one is the exact "$0 slippage
reads as none" regression class the surrounding code comment already warns
about. The 2026-08-11 round closed the same hole one level deeper: the
PER-ITEM commission handlers swallowed the error before the logging one could
fire, so an unparsable fee line silently became 0 and net P&L looked better
than it was. Unparsable items are now counted (`commission_unparsed`) and the
total is flagged as a lower bound (`commission_total_is_partial`) — see
[[auto_kapi_ve_geri_bildirim]].
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FeeModel, FillModel, MakerTakerFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model import (
    Bar,
    BarType,
    Currency,
    InstrumentId,
    Money,
    Price,
    Quantity,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.instruments import CurrencyPair, Equity

from state import IterationResult
from strategies import STRATEGY_REGISTRY

BTC = Currency.from_str("BTC")
USD = Currency.from_str("USD")
USDT = Currency.from_str("USDT")


# L43: single source app_constants — composer reads from there too. For
# backward compat the name backtest.STARTING_CASH is preserved (re-export).
from app_constants import STARTING_CASH  # noqa: E402

POLYGON = Venue("POLYGON")

# One Venue per Bybit product category. Distinct venues keep spot/linear/inverse
# InstrumentId + BarType — and therefore the ParquetDataCatalog keys they map to —
# disjoint, so a write for one category can never clobber another's data.
_BYBIT_VENUES: dict[str, Venue] = {
    "spot": Venue("BYBIT_SPOT"),
    "linear": Venue("BYBIT_LINEAR"),
    "inverse": Venue("BYBIT_INVERSE"),
}
# Back-compat alias: the linear USDT-perp venue was the historical single "BYBIT".
BYBIT = _BYBIT_VENUES["linear"]


def _bybit_venue(category: str) -> Venue:
    """Venue for a Bybit product category (defaults to linear for unknowns)."""
    return _BYBIT_VENUES.get((category or "linear").lower(), _BYBIT_VENUES["linear"])


def _bybit_quote(category: str) -> str:
    """Inverse (coin-margined) contracts quote in USD; spot/linear quote in USDT."""
    return "USD" if (category or "").lower() == "inverse" else "USDT"


def _bybit_market_symbol(symbol: str, category: str) -> str:
    """Real exchange symbol for a category. Inverse perps are coin-margined and
    drop the trailing 'T' of USDT (BTCUSDT → BTCUSD); spot/linear keep the symbol."""
    if (category or "").lower() == "inverse" and symbol.upper().endswith("USDT"):
        return symbol[:-1]  # ...USDT → ...USD
    return symbol


# M5: commissions ARE NOW MODELED — instrument maker/taker fee +
# MakerTakerFeeModel works on the pinned 1.230.0 (verified live; the earlier
# 'engine-level fee injection not supported' comment was WRONG). Bybit
# venues deduct commission at these rates; PnL is now net-of-commission.
BYBIT_TAKER_FEE_BPS: float = 5.5  # 0.055% Bybit taker fee
BYBIT_MAKER_FEE_BPS: float = 1.0  # 0.01%  Bybit maker fee

# Per-symbol commission overrides (maker_bps, taker_bps). Same pattern as
# _BYBIT_SPECS: symbols not listed here fall back to the global BYBIT_*_FEE_BPS
# above. Key is the canonical (USDT) symbol; the inverse ...USD market symbol
# maps to the same rate too.
_BYBIT_FEES_BPS: dict[str, tuple[float, float]] = {
    "BTCUSDT": (BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS),
    "ETHUSDT": (BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS),
    "SOLUSDT": (BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS),
    "XRPUSDT": (BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS),
}


def _bybit_fees_bps(symbol: str, market_symbol: str) -> tuple[float, float]:
    """(maker_bps, taker_bps) for a symbol — canonical key first, then market
    symbol (inverse ...USD), then the global BYBIT_*_FEE_BPS default."""
    return _BYBIT_FEES_BPS.get(
        symbol.upper(),
        _BYBIT_FEES_BPS.get(
            market_symbol.upper(), (BYBIT_MAKER_FEE_BPS, BYBIT_TAKER_FEE_BPS)
        ),
    )


# AUTO enables Nautilus' adverse one-tick slippage on every aggressive fill.
# ``prob_slippage=1`` plus a pinned seed is deterministic; a probabilistic
# unseeded FillModel would make ranking/replay irreproducible. This bps constant
# remains an external-analysis approximation, not the engine's tick-sized input.
SLIPPAGE_BPS: float = 2.0
SLIPPAGE_RANDOM_SEED: int = 42

# Interactive Brokers — US stock/ETF (e.g. QQQ) commission. IBKR Pro "Fixed"
# plan: fixed fee per share, minimum per order, and a cap as a percentage of
# trade value. Unlike Bybit's bps-rate (MakerTakerFeeModel), it is a per-share
# + min/max clamp structure; so IBFixedFeeModel is used instead of the built-in
# fee model. Figures from the IBKR Pro Fixed tariff.
IB_FIXED_PER_SHARE_USD: float = 0.005  # $0.005 / share
IB_FIXED_MIN_PER_ORDER_USD: float = 1.0  # minimum $1.00 per order
IB_FIXED_MAX_PCT_OF_TRADE: float = 0.01  # cap: 1% of trade value


class IBFixedFeeModel(FeeModel):
    """IBKR Pro 'Fixed' commission: per-share fee, bounded by a minimum per
    order and a trade-value percentage cap.

    Nautilus' built-in ``PerContractFeeModel``/``FixedFeeModel`` do not support
    min/max clamping; since IB's real structure is ``max(per_share*qty, min)``
    then ``min(…, max_pct*notional)``, a custom ``FeeModel`` is required. The
    ``FeeModel`` subclass is verified live on the pinned 1.230.0.

    ``get_commission`` is called for every fill; the commission is returned in
    the instrument's quote currency (US equity → USD).
    """

    def __init__(
        self,
        per_share_usd: float = IB_FIXED_PER_SHARE_USD,
        min_per_order_usd: float = IB_FIXED_MIN_PER_ORDER_USD,
        max_pct_of_trade: float = IB_FIXED_MAX_PCT_OF_TRADE,
    ) -> None:
        super().__init__()
        self._per_share = per_share_usd
        self._min = min_per_order_usd
        self._max_pct = max_pct_of_trade

    def get_commission(self, order, fill_qty, fill_px, instrument) -> Money:
        qty = float(fill_qty)
        px = float(fill_px)
        notional = qty * px
        commission = qty * self._per_share
        # Minimum per order (applied per fill — Nautilus calls per-fill)
        commission = max(commission, self._min)
        # 1% cap of trade value; not applied on zero/negative notional.
        if notional > 0:
            commission = min(commission, self._max_pct * notional)
        return Money(commission, instrument.quote_currency)


def _fee_model_for(instrument) -> FeeModel | None:
    """Commission model by instrument type.

    - CurrencyPair (Bybit crypto): MakerTakerFeeModel — reads the instrument's
      maker/taker fees (bps).
    - Equity (US stock/ETF, e.g. QQQ): IBFixedFeeModel — since it is traded on
      Interactive Brokers, IB Pro Fixed commission (per-share + min + 1% cap).
      Both the POLYGON index proxy and the external NASDAQ identity fall in this
      scope.
    - Other: None (commission not modeled).
    """
    if isinstance(instrument, CurrencyPair):
        return MakerTakerFeeModel()
    if isinstance(instrument, Equity):
        return IBFixedFeeModel()
    return None


# Set NAUTILUS_DEBUG_LOG=1 to enable NautilusTrader's internal logging.
# Reveals order rejections, strategy on_bar() exceptions, and account state errors
# that are otherwise swallowed silently.
_BYPASS_LOGGING = not os.getenv("NAUTILUS_DEBUG_LOG")


# Bybit crypto instrument specs, pinned to NAU's app/universe/universe.yaml
# (single source of truth). Per-symbol precision matches Bybit's real contract
# specs — unlike a one-size-fits-all default, so backtests here use the same
# instrument spec as the NAU catalog. {symbol: (price_precision, size_precision, tick)}
_BYBIT_SPECS: dict[str, tuple[int, int, str]] = {
    "BTCUSDT": (2, 3, "0.01"),
    "ETHUSDT": (2, 3, "0.01"),
    "SOLUSDT": (3, 2, "0.001"),
    "XRPUSDT": (4, 1, "0.0001"),
}
# NAU fallback for unlisted symbols (app/universe/__init__.py → size_precision 3).
_BYBIT_DEFAULT_SPEC: tuple[int, int, str] = (2, 3, "0.01")

# ── Sub-cent (meme-coin) price scale ────────────────────────────────────────
# DeepR 2026-08-11 [YÜKSEK]: the 2-decimal fallback above silently destroyed
# every sub-cent symbol. `Price(0.000009, 2)` is `0.00`, so a PEPEUSDT /
# SHIBUSDT / FLOKIUSDT backtest ran on an all-zero OHLC series and still
# reported a "valid" result (no error, meaningless PnL). This is the exact
# same class of trap as the Equity `size_precision=0` one recorded in the wiki
# ([[index_backtest_via_equity_proxy]] "Kritik Trap"): a precision the user
# never chose quietly turns a real number into 0. That one is handled by
# clamping + a visible warning; this one is handled the same way — derive the
# precision from the instrument's REAL price scale, and make the silent case
# impossible by failing loudly in `_bars_from_df` if a positive price still
# lands on 0 after conversion.
#
# Nautilus' standard build caps Price precision at 9 decimals.
_MAX_PRICE_PRECISION = 9
# How many significant digits the derived precision preserves. 5 keeps
# 0.000009 as 0.000009000 and still yields pp=2 for a 60_000 BTC price.
_PRICE_SIGNIFICANT_DIGITS = 5


def price_precision_for(price: float) -> int:
    """Decimal places needed to carry ``price`` without collapsing it to 0.

    Order-of-magnitude based, so it is stable across data windows (the same
    symbol always yields the same precision, whether it is derived on the
    catalog-write path or on the backtest path). Clamped to
    [2, ``_MAX_PRICE_PRECISION``]: never coarser than the 2-decimal Bybit
    default and never past what Nautilus' `Price` accepts.
    """
    if not math.isfinite(price) or price <= 0:
        return _BYBIT_DEFAULT_SPEC[0]
    exp = math.floor(math.log10(price))
    return max(2, min(_MAX_PRICE_PRECISION, _PRICE_SIGNIFICANT_DIGITS - exp))


def price_scale_hint(df) -> float | None:
    """Smallest strictly-positive OHLC price in ``df`` — the precision driver.

    The MINIMUM (not the mean) is what matters: precision must be enough for
    the smallest bar in the frame, otherwise exactly those bars round to 0.
    Returns None when the frame carries no usable price column.
    """
    if df is None:
        return None
    try:
        cols = [c for c in ("low", "open", "close", "high") if c in df.columns]
        if not cols:
            return None
        vals = df[cols].to_numpy(dtype=float)
        positive = vals[np.isfinite(vals) & (vals > 0)]
        if positive.size == 0:
            return None
        return float(positive.min())
    except Exception:
        # A price hint is an optimisation of honesty, not a hard dependency —
        # a weird frame must not break instrument construction. The
        # _bars_from_df guard still catches the zeroing case.
        return None


def _make_bybit_instrument(
    symbol: str = "BTCUSDT",
    base: str = "BTC",
    quote: str | None = None,
    category: str = "linear",
    fee_bps_override: float | None = None,
    price_hint: float | None = None,
) -> CurrencyPair:
    """Bybit crypto pair for a product category (spot/linear/inverse).

    Each category maps to a distinct Venue (BYBIT_SPOT/BYBIT_LINEAR/BYBIT_INVERSE)
    so their InstrumentId + BarType — and thus catalog keys — stay disjoint.
    Inverse (coin-margined) contracts quote in USD and use the ...USD symbol
    (BTCUSDT → BTCUSD); spot/linear quote in USDT. When ``quote`` is None it is
    derived from the category. Precision is pinned per-symbol to NAU's
    universe.yaml (BTCUSDT→sp3, SOLUSDT→sp2, XRPUSDT→sp1 ...) so backtests here
    use the same instrument spec as the NAU catalog.

    ``price_hint`` (a real observed price for this symbol, typically the
    smallest positive OHLC value of the frame about to be backtested) is used
    ONLY for symbols missing from ``_BYBIT_SPECS``: instead of blindly handing
    a sub-cent meme coin the 2-decimal fallback — which turns every price into
    0.00 — the precision is derived from that magnitude. Pinned symbols ignore
    the hint so the NAU catalog identity never drifts.
    """
    market_symbol = _bybit_market_symbol(symbol, category)
    if quote is None:
        quote = _bybit_quote(category)
    sym = Symbol(market_symbol)
    iid = InstrumentId(symbol=sym, venue=_bybit_venue(category))
    # Precision is keyed by the canonical (USDT) symbol; inverse BTCUSD shares
    # BTC precision, so fall back to the market symbol then the default.
    _pinned = _BYBIT_SPECS.get(symbol.upper(), _BYBIT_SPECS.get(market_symbol.upper()))
    if _pinned is not None:
        price_prec, size_prec, tick = _pinned
    else:
        _, size_prec, tick = _BYBIT_DEFAULT_SPEC
        price_prec = (
            price_precision_for(price_hint)
            if price_hint is not None
            else _BYBIT_DEFAULT_SPEC[0]
        )
        if price_prec != _BYBIT_DEFAULT_SPEC[0]:
            # Loud on the server side too: a precision nobody configured is a
            # fact the operator should be able to see in the log.
            tick = "0." + "0" * (price_prec - 1) + "1"
            logging.getLogger(__name__).info(
                "%s not in _BYBIT_SPECS — price_precision derived from the data "
                "(smallest price %.12g → %d decimals, tick %s)",
                market_symbol,
                price_hint,
                price_prec,
                tick,
            )
    size_step = "1" if size_prec == 0 else "0." + "0" * (size_prec - 1) + "1"
    maker_bps, taker_bps = _bybit_fees_bps(symbol, market_symbol)
    if fee_bps_override is not None:
        maker_bps = taker_bps = fee_bps_override
    return CurrencyPair(
        instrument_id=iid,
        raw_symbol=sym,
        base_currency=Currency.from_str(base),
        quote_currency=Currency.from_str(quote),
        price_precision=price_prec,
        size_precision=size_prec,
        price_increment=Price.from_str(tick),
        size_increment=Quantity.from_str(size_step),
        min_quantity=Quantity.from_str(size_step),
        # M5: real Bybit commission rates — MakerTakerFeeModel reads these.
        # Per-symbol override from _BYBIT_FEES_BPS; otherwise falls to global rate.
        maker_fee=Decimal(str(maker_bps / 10_000)),
        taker_fee=Decimal(str(taker_bps / 10_000)),
        ts_event=0,
        ts_init=0,
    )


def _make_bybit_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
    """`interval` matches Bybit kline codes: "1","5","15","30","60","240","720","D"."""
    step = {
        "1": "1-MINUTE",
        "5": "5-MINUTE",
        "15": "15-MINUTE",
        "30": "30-MINUTE",
        "60": "1-HOUR",
        "240": "4-HOUR",
        "720": "12-HOUR",
        "D": "1-DAY",
    }.get(interval)
    if step is None:
        raise ValueError(f"unsupported bybit interval {interval!r}")
    return BarType.from_str(f"{instrument_id}-{step}-LAST-EXTERNAL")


def _make_index_instrument(ticker: str) -> Equity:
    """Model an index/US-equity as a tradable Equity proxy.

    Nautilus' native `IndexInstrument` is documented as "not directly
    tradable" — the engine won't fill orders on it. Using `Equity` with
    USD currency lets the backtester open/close positions off the index
    close, which is what a user backtesting an index-tracking strategy
    would expect.

    L40 (venue duality, intentional): here venue=POLYGON — this is the app's
    OWN index-CSV proxy identity and NEVER cross-matches the NAU catalog's
    real-exchange identities (SPY.ARCA, QQQ.NASDAQ…). The external (NAU)
    catalog path already works with real venue ids
    (load_external_bars + EXTERNAL_PEER_BASKET); the two worlds are
    intentionally separate.

    Precision follows NAU's QLAB equity standard (universe.yaml: every
    equity is USD, price_precision 2, tick 0.01, lot 1) so backtests here
    use the same instrument spec as the NAU catalog. Venue stays POLYGON —
    this project's index-CSV data source label.
    """
    sym = Symbol(ticker)
    iid = InstrumentId(symbol=sym, venue=POLYGON)
    return Equity(
        instrument_id=iid,
        raw_symbol=sym,
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


def _make_index_bar_type(instrument_id: InstrumentId, granularity: str) -> BarType:
    from data import _GRAN_BARSPEC

    if granularity not in _GRAN_BARSPEC:
        raise ValueError(
            f"unsupported granularity {granularity!r}; supported: {list(_GRAN_BARSPEC)}"
        )
    step, aggregation = _GRAN_BARSPEC[granularity]
    return BarType.from_str(f"{instrument_id}-{step}-{aggregation}-LAST-EXTERNAL")


def _make_external_bar_type(instrument_id: InstrumentId, granularity: str) -> BarType:
    """``granularity`` is the external catalog's own DSL step ('1-MINUTE',
    '4-HOUR', '1-DAY', ...) so the BarType matches the catalog's bar dirs."""
    return BarType.from_str(f"{instrument_id}-{granularity}-LAST-EXTERNAL")


def _prepare_df(bars_df: pd.DataFrame) -> pd.DataFrame:
    """Ensure required columns and datetime index.

    Also drops rows where OHLC constraints are violated — some data sources
    occasionally emit a partial bar where low > close.
    """
    required = ["open", "high", "low", "close", "volume"]
    df = bars_df[required].copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    # Drop rows violating Nautilus OHLC invariant: low <= open/close <= high
    valid = (
        (df["low"] <= df["open"])
        & (df["low"] <= df["close"])
        & (df["high"] >= df["open"])
        & (df["high"] >= df["close"])
        & df["volume"].notna()
        & (df["volume"] >= 0)
    )
    dropped = int(len(df) - valid.sum())
    if dropped > 0:
        # L21: make silent data loss observable — all three call paths
        # (run_backtest, composed, trend-filter) are logged from this one point.
        logging.getLogger(__name__).warning(
            "_prepare_df: dropped %d row(s) violating OHLC invariant (%d kept)",
            dropped,
            int(valid.sum()),
        )
    return df[valid]


def _bar_interval_ns(bar_type: BarType) -> int:
    """Nanoseconds spanned by one bar of ``bar_type`` (its open→close offset).

    Bybit klines (and the index CSVs, which pandas ``resample`` labels by the
    left/open edge) are timestamped by the bar's OPEN time. Nautilus convention
    is that a *completed* bar's ``ts_event`` is its CLOSE time (open + interval).
    ``_bars_from_df`` adds this offset so catalog bars and live-engine bars are
    stamped consistently at close.

    Uses ``BarSpecification.timedelta`` (present in nautilus_trader 1.230, verified
    for MINUTE/HOUR/DAY). Non-time aggregations (TICK/VOLUME) have no fixed span
    and are rejected — every bar type this app builds is time-based.
    """
    try:
        td = bar_type.spec.timedelta
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"cannot derive close-time offset for non-time bar spec: {bar_type.spec!r}"
        ) from exc
    return int(td.total_seconds() * 1_000_000_000)


class PricePrecisionError(ValueError):
    """A positive price collapses to 0 at the instrument's price_precision.

    Its own type (not a bare ValueError) so callers — the /data catalog-write
    route, the backtest worker — can recognise the case and say something
    useful instead of surfacing it as an anonymous crash.
    """


def _assert_price_precision_survives(instrument, pp: int, *columns) -> None:
    """Refuse a conversion that would silently zero out real prices.

    DeepR 2026-08-11 [YÜKSEK]: a sub-cent symbol (PEPEUSDT ~9e-6) on the
    2-decimal Bybit default produced `Price(0.00)` for every bar; the backtest
    then reported a "successful" run over an all-zero series. Rounding a real
    price to nothing is never a result the user asked for, so it is an error,
    not a warning — the same stance the wiki takes on the Equity
    ``size_precision=0`` trap.
    """
    smallest = None
    for col in columns:
        arr = np.asarray(col, dtype=float)
        positive = arr[np.isfinite(arr) & (arr > 0)]
        if positive.size == 0:
            continue
        m = float(positive.min())
        if smallest is None or m < smallest:
            smallest = m
    if smallest is None:
        return  # no positive price at all — nothing to lose to rounding
    if float(Price(smallest, pp)) != 0.0:
        return
    needed = price_precision_for(smallest)
    raise PricePrecisionError(
        f"{instrument.id} price_precision={pp} cannot represent this data: the "
        f"smallest price ({smallest:.12g}) rounds to 0.00, which would make the "
        f"whole OHLC series zero and the backtest meaningless. "
        f"This symbol needs about {needed} decimals — add it to "
        f"backtest._BYBIT_SPECS with its real contract tick, or pass a "
        f"price_hint so the precision is derived from the data."
    )


def _bars_from_df(bar_type: BarType, instrument, df: pd.DataFrame) -> list[Bar]:
    """Build Nautilus Bar objects directly from an OHLCV DataFrame.

    v2 dropped BarDataWrangler.process(df); Arrow-only ingest doesn't fit our
    pandas pipeline. Direct Bar() construction is the simplest port.

    ts_event/ts_init MUST be integer nanoseconds (Nautilus universal timestamp
    unit). DatetimeIndex may have ms, us, or ns resolution depending on the
    data source — always normalize to ns before passing.

    The source index holds the bar's OPEN time; we shift it by one interval so
    ts_event/ts_init are the bar CLOSE time (see ``_bar_interval_ns``). This is
    the single funnel for both catalog writes and live-engine bars, so the shift
    is applied exactly once here.

    Being that single funnel, this is also where the sub-cent trap is closed:
    if the instrument's ``price_precision`` cannot represent a positive price
    in the frame, the conversion is REFUSED (``PricePrecisionError``) instead
    of quietly producing an all-zero OHLC series that the engine happily
    "backtests". See ``price_precision_for``.
    """
    pp = instrument.price_precision
    sp = instrument.size_precision
    # Convert index to nanoseconds regardless of its stored resolution (ms/us/ns).
    # Timezone-aware indices must be stripped first (values are already UTC).
    idx = df.index
    if hasattr(idx, "tz") and idx.tz is not None:
        idx = idx.tz_localize(None)
    ts_ns = idx.astype("datetime64[ns]").astype("int64").to_numpy()
    interval_ns = _bar_interval_ns(bar_type)
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    _assert_price_precision_survives(instrument, pp, o, h, lo, c)
    ts_close = ts_ns + interval_ns
    return [
        Bar(
            bar_type,
            Price(float(o[i]), pp),
            Price(float(h[i]), pp),
            Price(float(lo[i]), pp),
            Price(float(c[i]), pp),
            Quantity(float(v[i]), sp),
            int(ts_close[i]),
            int(ts_close[i]),
        )
        for i in range(len(df))
    ]


def _parse_money_column(series: pd.Series) -> np.ndarray:
    """Nautilus writes realized_pnl as strings like '-3.38 USD'. Strip and cast."""
    _bad_count = 0

    def _one(v):
        nonlocal _bad_count
        if v is None:
            return 0.0
        s = str(v).strip()
        if not s:
            return 0.0
        try:
            return float(s.split()[0])
        except (ValueError, IndexError):
            _bad_count += 1
            return 0.0

    result = np.array([_one(v) for v in series], dtype=float)
    if _bad_count:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "_parse_money_column: %d unparseable value(s) silently set to 0.0",
            _bad_count,
        )
    return result


def _periods_per_year(bar_type=None, instrument=None, bars_df=None) -> int:
    """H5/M35/L42: derive the annualization base FROM SOURCE + BAR INTERVAL.

    Returns are at bar frequency; the correct factor is sqrt(bars per year).
    The old code used 365 unconditionally — on 1m data Sharpe was crushed ~38×,
    on 1h ~4.9×, and cross-TF comparisons were invalid.

    - Crypto (24/7): 365 days × 86400s / bar_s
    - Equity/Index (weekdays): intraday → 252 days × 6.5 hours / bar_s;
      daily+ → 252 (NAU convention)
    - If not derivable, returns a conservative 365 (daily assumption).
    """
    bar_sec = 0.0
    try:
        # The spec str is the most robust version-independent surface: '1-MINUTE-LAST'
        # (the aggregation field is a pyo3 int enum — its name doesn't come out of str()).
        txt = str(bar_type.spec) if bar_type is not None else ""
        parts = txt.split("-")
        step = int(parts[0])
        unit_sec = {
            "SECOND": 1,
            "MINUTE": 60,
            "HOUR": 3600,
            "DAY": 86400,
            "WEEK": 604800,
            "MONTH": 2_592_000,
        }
        bar_sec = step * unit_sec.get(parts[1], 0)
    except (ValueError, IndexError, AttributeError):
        bar_sec = 0.0
    if bar_sec <= 0:
        return 365
    is_crypto = isinstance(instrument, CurrencyPair) if instrument is not None else True
    if is_crypto:
        return max(1, int(365 * 86400 / bar_sec))
    # M387: equity/index — 252 was wrong for WEEK/MONTH (weekly Sharpe ~2.2×,
    # monthly ~4.6× inflated). Correct annual period counts: day 252, week 52, month 12.
    if bar_sec >= 27 * 86400:  # ~monthly
        return 12
    if bar_sec >= 6 * 86400:  # ~weekly
        return 52
    if bar_sec >= 86400:  # daily+
        return 252  # NAU 252 trading days
    # External catalogs are the authority on their own session shape. QQQ 1H,
    # for example, contains seven hourly bars on a normal session; the old
    # hard-coded 6.5h estimate returned 1638 instead of the observed 1764.
    try:
        if bars_df is not None and len(bars_df) >= 20:
            idx = pd.DatetimeIndex(bars_df.index)
            per_day = pd.Series(1, index=idx.normalize()).groupby(level=0).sum()
            observed = int(round(float(per_day.median()))) * 252
            if observed > 0:
                return observed
    except (TypeError, ValueError, AttributeError):
        pass
    return max(1, int(252 * 6.5 * 3600 / bar_sec))  # intraday equity fallback


def _sharpe_manual(equity_series: list[float], annualization: int = 365) -> float:
    """Sharpe from a bar-resolution equity series.

    Returns are consecutive-bar percent changes (NO resample — H5: the old
    docstring's 'daily resampling' claim was false); annualization is done with
    sqrt(annualization), and ``annualization`` must be the NUMBER OF BARS PER
    YEAR (see _periods_per_year). Fewer than 2 points / zero std → NaN.
    """
    if len(equity_series) < 2:
        return float("nan")
    arr = np.array(equity_series, dtype=float)
    # Daily returns from bar-level equity (pct change, drop first NaN)
    returns = np.diff(arr) / arr[:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 2:
        return float("nan")
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    if sigma == 0.0:
        return float("nan")
    return mu / sigma * math.sqrt(annualization)


def _sortino_manual(equity_series: list[float], annualization: int = 365) -> float:
    """Sortino from the same bar-resolution return series as ``_sharpe_manual``.

    The downside deviation uses zero as the target return and all observations
    (non-negative returns contribute zero), which keeps its denominator on the
    same bar-frequency basis as Sharpe.  Returning NaN for no downside risk is
    deliberate: a finite score would fabricate a ranking signal.
    """
    if len(equity_series) < 2:
        return float("nan")
    arr = np.asarray(equity_series, dtype=float)
    returns = np.diff(arr) / arr[:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 2:
        return float("nan")
    downside = np.minimum(returns, 0.0)
    downside_dev = float(np.sqrt(np.mean(np.square(downside))))
    if downside_dev == 0.0:
        return float("nan")
    return float(np.mean(returns)) / downside_dev * math.sqrt(annualization)


def _max_dd_from_series(equity_series: list[float]) -> float:
    """Compute max drawdown from any equity series (bar or trade resolution).

    CONTRACT (H11/L36): the return is a NEGATIVE FRACTION (12% drawdown →
    -0.12; no drawdown → 0.0). All in-app consumers (junk gate max_dd>=0, MC
    threshold dd_p50<-25, wfo abs(dd)) assume this convention. NAU stores a
    POSITIVE PERCENTAGE — at the NAU boundary use ``nau_max_drawdown`` /
    ``metrics['max_dd_pct']``; don't copy the patterns verbatim.
    """
    if len(equity_series) < 2:
        return 0.0
    arr = np.array(equity_series, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    return float(dd.min())


def nau_max_drawdown(max_dd: float | None) -> float | None:
    """H11: in-app negative-fraction max_dd → NAU positive-percentage conversion.

    The single official bridge for record merging / cross-system comparison.
    """
    if max_dd is None:
        return None
    try:
        if math.isnan(max_dd):
            return None
    except TypeError:
        return None
    return round(abs(float(max_dd)) * 100.0, 4)


def _metrics(
    engine: BacktestEngine,
    positions_df: pd.DataFrame | None,
    mtm_equity: list[float] | None = None,
    annualization: int | None = None,
    mtm_ts: list[int] | None = None,
    starting_cash: float | None = None,
    slippage_model_active: bool = False,
    price_increment: float | None = None,
) -> dict:
    """Compute backtest metrics.

    Args:
        engine: The BacktestEngine after run().
        positions_df: Closed positions report.
        mtm_equity: Optional bar-resolution mark-to-market equity snapshots
            captured by ComposedStrategy._mtm_equity. When provided, max_dd
            and Sharpe are computed from MTM rather than realized PnL.
        annualization: H5 — bars per year (_periods_per_year); if None,
            365 (daily assumption, old behavior).
        mtm_ts: L19 — bar timestamps (ns) aligned with mtm_equity; if
            provided, an ``equity_curve_mtm`` [(iso_ts, eq)] field is produced.
    """
    if annualization is None:
        annualization = 365
    _sc = starting_cash if starting_cash is not None else STARTING_CASH
    if positions_df is None or positions_df.empty:
        return {
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "sharpe": float("nan"),
            "sortino": float("nan"),
            "win_rate": 0.0,
            "max_dd": 0.0,
            "max_dd_pct": 0.0,
            "n_trades": 0,
            "n_wins": 0,
            "n_losses": 0,
            "n_breakeven": 0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            # L33: on an empty run use 0.0 instead of NaN (NAU rule; NaN breaks JSON).
            "profit_factor": 0.0,
            "profit_factor_returns": 0.0,
            "avg_return": float("nan"),
            "volatility": float("nan"),
            "max_winner": 0.0,
            "max_loser": 0.0,
            "starting_cash": _sc,
            "long_ratio": float("nan"),
            "avg_duration_mins": float("nan"),
            "commission_total": 0.0,
            "commission_unparsed": 0,
            "commission_total_is_partial": False,
            "slippage_total": 0.0,
            "slippage_reported_total": 0.0,
            "slippage_estimated_total": 0.0,
            "slippage_fill_count": 0,
            "slippage_model_active": bool(slippage_model_active),
            "runner": "BacktestEngine",
            # Phase 1 additions
            "sharpe_nautilus": float("nan"),
            "sortino_nautilus": float("nan"),
            "sharpe_per_trade": float("nan"),
            "annualization": annualization,
            "max_dd_mtm": 0.0,
            "equity_curve_realized": [_sc],
            "equity_curve_mtm": [],
        }

    pnls = _parse_money_column(positions_df["realized_pnl"])
    n_trades = int(len(pnls))
    total_pnl = float(pnls.sum())
    n_wins = int((pnls > 0).sum())
    n_losses = int((pnls < 0).sum())
    n_breakeven = n_trades - n_wins - n_losses  # pnl == 0
    win_rate = n_wins / n_trades if n_trades else 0.0

    # Duration in minutes
    avg_duration_mins = float("nan")
    if "duration_ns" in positions_df.columns:
        try:
            dur_ns = pd.to_numeric(positions_df["duration_ns"], errors="coerce")
            avg_duration_mins = float(dur_ns.mean()) / 60e9  # ns → minutes
        except Exception:
            # DeepR 2026-08-08 [ORTA]: was a bare `pass` — a schema drift here
            # silently reported NaN as if duration were simply unavailable,
            # not broken.
            logging.getLogger(__name__).warning(
                "_metrics: avg_duration_mins computation failed", exc_info=True
            )

    # Commissions
    commission_total = 0.0
    # DeepR 2026-08-11 [ORTA]: KALEM BAZLI sessiz sıfırlama. Dıştaki handler bir
    # önceki turda loglanır hâle gelmişti ama içteki ikisi hâlâ sessizdi —
    # ve iç handler hatayı yuttuğu için dıştaki LOGLAYAN handler hiç
    # tetiklenmiyordu. 5 kalemin 2'si ayrıştırılamayınca komisyon sessizce
    # eksik, net P&L olduğundan İYİ çıkıyordu; kapı kalibrasyonu tam da
    # net/brüt tutarlılığı üzerine kurulu olduğu için bu karar bozan bir hata.
    # Ayrıştırılamayan kalem artık 0 SAYILMAZ: sayılır, loglanır ve toplam
    # "alt sınır" olarak metriklerde işaretlenir.
    commission_unparsed = 0
    commission_bad_samples: list[str] = []
    commission_partial = False
    if "commissions" in positions_df.columns:
        try:

            def _is_blank(v) -> bool:
                """None/NaN/boş dize = 'komisyon yok' — hata DEĞİL.

                Bunları hata saymak her temiz koşuyu kırmızıya boyardı ve
                bayrak hiçbir şey anlatmaz hâle gelirdi.
                """
                if v is None:
                    return True
                if isinstance(v, float) and math.isnan(v):
                    return True
                return isinstance(v, str) and not v.strip()

            def _note_bad(v) -> None:
                nonlocal commission_unparsed
                commission_unparsed += 1
                if len(commission_bad_samples) < 3:
                    commission_bad_samples.append(repr(v)[:60])

            def _parse_commissions(v):
                if isinstance(v, (list, tuple, set)):
                    s = 0.0
                    for item in v:
                        if _is_blank(item):
                            continue
                        try:
                            s += float(str(item).split()[0])
                        except (ValueError, IndexError, TypeError):
                            _note_bad(item)
                    return s
                if _is_blank(v):
                    return 0.0
                try:
                    return float(str(v).split()[0])
                except (ValueError, IndexError, TypeError):
                    _note_bad(v)
                    return 0.0

            commission_total = float(
                positions_df["commissions"].apply(_parse_commissions).sum()
            )
        except Exception:
            # DeepR 2026-08-08 [ORTA]: was a bare `pass` — a positions_df schema
            # change silently zeroed commission_total instead of surfacing that
            # the P&L breakdown is now wrong.
            commission_partial = True
            logging.getLogger(__name__).warning(
                "_metrics: commission_total computation failed", exc_info=True
            )
    if commission_unparsed:
        commission_partial = True
        logging.getLogger(__name__).warning(
            "_metrics: %d commission item(s) could not be parsed (%s) — "
            "commission_total %.4f is a LOWER BOUND, so net P&L looks better "
            "than it is",
            commission_unparsed,
            ", ".join(commission_bad_samples),
            commission_total,
        )

    # Slippage from order fills
    slippage_reported_total = 0.0
    slippage_estimated_total = 0.0
    slippage_fill_count = 0
    try:
        fills_df = engine.trader.generate_order_fills_report()
        slippage_fill_count = int(len(fills_df))
        if not fills_df.empty and "slippage" in fills_df.columns:
            slippage_reported_total = float(
                pd.to_numeric(fills_df["slippage"], errors="coerce").sum()
            )
        # Some Nautilus reports omit/zero the slippage column even though the
        # deterministic FillModel moved market fills by one tick. Preserve the
        # reported number separately and expose a conservative one-tick audit
        # estimate so "model active + 636 fills + $0" is no longer invisible.
        if slippage_model_active and slippage_reported_total == 0 and price_increment:
            qty_col = next(
                (
                    c
                    for c in ("last_qty", "quantity", "qty", "filled_qty")
                    if c in fills_df
                ),
                None,
            )
            if qty_col:
                quantities = np.abs(_parse_money_column(fills_df[qty_col]))
                slippage_estimated_total = float(quantities.sum()) * float(
                    price_increment
                )
    except Exception:
        # DeepR 2026-08-08 [ORTA]: was a bare `pass` — this is the exact class
        # of regression the surrounding comment describes ($0 slippage
        # reading as "no slippage" instead of "the audit estimate failed").
        logging.getLogger(__name__).warning(
            "_metrics: slippage computation failed", exc_info=True
        )
    slippage_total = slippage_reported_total or slippage_estimated_total

    # Realized equity curve for max drawdown (backward-compat) + MTM if available
    dd_pnls = pnls
    if "ts_closed" in positions_df.columns:
        try:
            _order = np.argsort(positions_df["ts_closed"].to_numpy(dtype="int64"))
            dd_pnls = pnls[_order]
        except Exception:
            dd_pnls = pnls
    realized_equity = list(_sc + np.cumsum(dd_pnls))
    realized_equity_full = [_sc] + realized_equity

    # MTM max drawdown: use bar-resolution snapshots when available (Phase 1)
    if mtm_equity and len(mtm_equity) > 1:
        max_dd = _max_dd_from_series(mtm_equity)
        max_dd_mtm = max_dd
    else:
        # Fall back to realized-PnL drawdown
        max_dd = _max_dd_from_series(realized_equity_full)
        max_dd_mtm = max_dd

    # Portfolio statistics (PortfolioAnalyzer API of the pinned nautilus_trader)
    _analyzer = engine.portfolio.analyzer
    ret = _analyzer.get_performance_stats_returns()
    gen = _analyzer.get_performance_stats_general()
    _pnls_by_ccy: dict[str, dict] = {}
    for _ccy in _analyzer.currencies:
        try:
            _pnls_by_ccy[_ccy.code] = _analyzer.get_performance_stats_pnls(_ccy)
        except Exception:
            pass
    pnl_stats = _pnls_by_ccy.get("USDT", _pnls_by_ccy.get("USD", {}))

    def _stat(d: dict, key: str) -> float:
        """NaN if key missing; preserves a real 0.0 value (no falsy-zero trap)."""
        v = d.get(key)
        if v is None:
            return float("nan")
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    sharpe_nautilus = _stat(ret, "Sharpe Ratio (252 days)")
    sortino_nautilus = _stat(ret, "Sortino Ratio (252 days)")
    volatility = _stat(ret, "Returns Volatility (252 days)")
    avg_return = _stat(ret, "Average (Return)")
    profit_factor = _stat(ret, "Profit Factor")
    avg_win_ret = _stat(ret, "Average Win (Return)")
    avg_loss_ret = _stat(ret, "Average Loss (Return)")

    # H5: bar-frequency manual Sharpe — annualization comes FROM THE CALLER
    # (_periods_per_year: source + bar interval). When the MTM curve
    # (bar-resolution) is used, bar-frequency annualization is CORRECT.
    _using_mtm = bool(mtm_equity and len(mtm_equity) > 5)
    if _using_mtm:
        sharpe_manual = _sharpe_manual(mtm_equity, annualization=annualization)
        sharpe = sharpe_manual if not math.isnan(sharpe_manual) else sharpe_nautilus
        sortino_manual = _sortino_manual(mtm_equity, annualization=annualization)
        sortino = sortino_manual if not math.isnan(sortino_manual) else sortino_nautilus
    else:
        # H610: no MTM (registry strategies — MACrossover/RSIMeanReversion).
        # realized_equity_full is TRADE-resolution; applying bar-frequency
        # annualization to it (sqrt(525600) on 1m) was INFLATING Sharpe ~725×.
        # Make the frequency-correct Nautilus 252-day value primary; if that's
        # missing too, the per-trade sqrt(n) base.
        sharpe = sharpe_nautilus
        sortino = sortino_nautilus

    # M35: per-trade Sharpe for NAU comparison (NAU_ev: mean/std × sqrt(n_trades)
    # from per-TRADE returns). M620: NAU uses population std (ddof=0) and in the
    # degenerate (n<2 / std=0) case returns 0.0 instead of NaN — aligned.
    # (The return base in NAU is running-equity; here it's fixed STARTING_CASH —
    # a small scale difference on long compounding runs, consider it in
    # cross-system comparison.)
    sharpe_per_trade = 0.0
    if n_trades >= 2:
        _tr = np.asarray(pnls, dtype=float) / _sc
        _std = float(np.std(_tr, ddof=0))  # M620: population std (NAU paritesi)
        if _std > 0:
            sharpe_per_trade = float(np.mean(_tr)) / _std * math.sqrt(n_trades)
    if not _using_mtm and (sharpe is None or math.isnan(sharpe)):
        # If Nautilus Sharpe is also missing, fall to the per-trade base (frequency-correct).
        sharpe = sharpe_per_trade

    # L33: on a lossless run Nautilus returns PF=inf — NAU's finite cap (99.0);
    # inf/NaN produces non-standard JSON and breaks sorting.
    if math.isinf(profit_factor):
        profit_factor = 99.0 if profit_factor > 0 else 0.0

    # 2026-08-04: `profit_factor` above is Nautilus' RETURNS-series statistic. It
    # is not what a trader reads off a backtest card: an audited AUTO iteration
    # showed PF 20.27 beside a 36% win rate and Sharpe 0.19 — three numbers that
    # cannot describe the same 671 trades under the usual definition. Compute the
    # TRADE-level profit factor (gross wins / gross losses over closed positions,
    # net of commission since `pnls` is realized PnL) and publish that as
    # `profit_factor`; the returns-series value stays as `profit_factor_returns`
    # so nothing that depended on it is silently rewritten.
    profit_factor_returns = profit_factor
    # `pnls` is a numpy array here (_parse_money_column) — `if pnls:` on an array
    # raises "truth value ... is ambiguous", so length is checked explicitly.
    _pnl_arr = np.asarray(pnls, dtype=float)
    if _pnl_arr.size:
        _wins = float(_pnl_arr[_pnl_arr > 0].sum())
        _losses = float(-_pnl_arr[_pnl_arr < 0].sum())
        if _losses > 0:
            profit_factor = min(99.0, _wins / _losses)
        else:
            # No losing trade at all: the ratio is unbounded → NAU's finite cap,
            # and 0.0 when there were no wins either (flat book, not perfection).
            profit_factor = 99.0 if _wins > 0 else 0.0
    if math.isnan(profit_factor):
        profit_factor = 0.0

    # L19: bar-resolution MTM equity curve (ts, eq) — the realized step-curve
    # in the UI didn't show intra-position dips; max_dd comes from this series,
    # and now the curve can be shown too. >5000 points are uniformly thinned.
    equity_curve_mtm: list[list] = []
    if mtm_equity and mtm_ts and len(mtm_equity) == len(mtm_ts):
        _n = len(mtm_equity)
        _stride = max(1, _n // 5000)
        for _i in range(0, _n, _stride):
            equity_curve_mtm.append(
                [
                    pd.Timestamp(mtm_ts[_i], unit="ns", tz="UTC").isoformat(),
                    round(float(mtm_equity[_i]), 2),
                ]
            )
        # Ensure the curve ends on the true final equity (stride usually skips n-1).
        if (_n - 1) % _stride != 0:
            equity_curve_mtm.append(
                [
                    pd.Timestamp(mtm_ts[_n - 1], unit="ns", tz="UTC").isoformat(),
                    round(float(mtm_equity[_n - 1]), 2),
                ]
            )

    # $ amounts from pnl stats
    avg_win_usd = (
        float(pnl_stats["Avg Winner"])
        if "Avg Winner" in pnl_stats
        else (avg_win_ret * _sc if not np.isnan(avg_win_ret) else float("nan"))
    )
    avg_loss_usd = (
        float(pnl_stats["Avg Loser"])
        if "Avg Loser" in pnl_stats
        else (avg_loss_ret * _sc if not np.isnan(avg_loss_ret) else float("nan"))
    )
    max_winner = _stat(pnl_stats, "Max Winner")
    max_loser = _stat(pnl_stats, "Max Loser")
    long_ratio = _stat(gen, "Long Ratio")

    return {
        "starting_cash": _sc,
        "pnl": total_pnl,
        "pnl_pct": total_pnl / _sc,
        "sharpe": sharpe,
        "sortino": sortino,
        "volatility": volatility,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "profit_factor_returns": profit_factor_returns,
        "win_rate": win_rate,
        "max_dd": max_dd,
        # H11: positive-percentage field for the NAU boundary comparison (the
        # internal convention STAYS as a negative fraction — see the
        # _max_dd_from_series docstring).
        "max_dd_pct": nau_max_drawdown(max_dd),
        "n_trades": n_trades,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_breakeven": n_breakeven,
        "avg_win": avg_win_usd,
        "avg_loss": avg_loss_usd,
        "max_winner": max_winner,
        "max_loser": max_loser,
        "long_ratio": long_ratio,
        "avg_duration_mins": avg_duration_mins,
        "commission_total": commission_total,
        # DeepR 2026-08-11 [ORTA]: komisyon toplamının DÜRÜSTLÜK alanları.
        # `commission_total_is_partial` True iken toplam bir ALT SINIRDIR:
        # net P&L olduğundan iyi görünür, tüketici bunu bilmek zorunda.
        "commission_unparsed": commission_unparsed,
        "commission_total_is_partial": commission_partial,
        "slippage_total": slippage_total,
        "slippage_reported_total": slippage_reported_total,
        "slippage_estimated_total": slippage_estimated_total,
        "slippage_fill_count": slippage_fill_count,
        "slippage_model_active": bool(slippage_model_active),
        "runner": "BacktestEngine",
        # Phase 1 additions
        "sharpe_nautilus": sharpe_nautilus,  # Nautilus 252-day value kept for audit
        "sortino_nautilus": sortino_nautilus,  # same 252-day audit reference
        "sharpe_per_trade": sharpe_per_trade,  # M35: NAU per-trade base
        "annualization": annualization,  # H5: real bars/year base
        "max_dd_mtm": max_dd_mtm,  # MTM drawdown (== max_dd when MTM available)
        "equity_curve_realized": realized_equity_full,
        "equity_curve_mtm": equity_curve_mtm,  # L19: [(iso_ts, eq)] bar-MTM
    }


def comparable_metrics(m: dict) -> dict:
    """Project a metrics dict onto the fields that mean the same thing across
    the Engine and Node runners, for reconciliation/parity checks.

    Deliberately excludes the primary ``sharpe`` (annualized differently per
    runner: Engine = manual 365-day, Node = Nautilus 252-day) and ``max_dd``
    (the Node path can't compute it). Use ``sharpe_nautilus`` (Nautilus 252-day,
    present on both) as the comparable risk-adjusted return.
    """
    m = m or {}
    return {
        "pnl": m.get("pnl"),
        "pnl_pct": m.get("pnl_pct"),
        "win_rate": m.get("win_rate"),
        "n_trades": m.get("n_trades"),
        "sharpe_nautilus": m.get("sharpe_nautilus"),
    }


_EXIT_KIND_LABELS = {
    "sl": "Stop-Loss",
    "tp": "Take-Profit",
    "flip": "Reverse signal (flip)",
    "eob": "Backtest end",
}

_DR_TAG_RE = re.compile(r"dr:(\d+)")
_XR_TAG_RE = re.compile(r"xr:(\d+)")


def _reason_text(decision: dict | None) -> str | None:
    """Convert a decision record to a single human-readable line.

    E.g.: "MA Cross (fast=10, slow=30, up) · fast 42.1 / slow 41.8".
    In AND logic, multiple blocks are joined with " + ".
    """
    if not decision or not decision.get("blocks"):
        return None
    parts = []
    for b in decision["blocks"]:
        params = ", ".join(f"{k}={v}" for k, v in (b.get("params") or {}).items())
        s = b.get("label") or b.get("type") or "?"
        if params:
            s += f" ({params})"
        values = b.get("values")
        if values:
            s += " · " + " / ".join(f"{k} {v}" for k, v in values.items())
        parts.append(s)
    return " + ".join(parts)


def _reason_fills_report(trader) -> tuple[pd.DataFrame | None, str | None]:
    """Fills raporunu getir → ``(frame, error)``; alınamazsa ``(None, sebep)``.

    DeepR 2026-08-11 [ORTA]: bu çağrı sessizce ``= None`` yapıyordu. Fills
    frame'i olmadan HER trade'in ``exit_kind``/``entry_reason``/``exit_reason``
    alanı boş kalıyor; tear sheet'te "bu trade neden kapandı" sütunu tüm
    satırlarda boş görünüyor — sanki strateji hiç SL/TP kullanmamış gibi.
    Metrikler ve grafik normal olduğu için bozulma fark edilmiyordu.

    Sebep metnini DÖNDÜRMEK şart: çağıran onu ``metrics["exit_reasons_error"]``
    olarak damgalar ve arayüz "çıkış sebepleri bu koşu için alınamadı: <sebep>"
    diyebilir — boş bir liste "sebep yok" diye okunmaz. Aynı fonksiyonda
    ayrıştırılamayan pozisyon satırları için zaten bir sayaç + ``logging.warning``
    vardı; bu yol o standarda getirildi.
    """
    try:
        return trader.generate_order_fills_report(), None
    except Exception as exc:
        logging.warning(
            "order fills report unavailable — trade exit reasons will be "
            "missing for this run: %s",
            exc,
            exc_info=True,
        )
        return None, f"{type(exc).__name__}: {exc}"


def _fills_lookup(fills_df: pd.DataFrame | None) -> tuple[dict, dict]:
    """Convert the fills report to {order_id: tags/type} dicts in a single pass.

    The old version did a pandas ``.loc`` for every trade — on 1k trades the
    profile showed ~0.5s. On multi-fill orders the FIRST row wins (identical to
    the old ``iloc[0]`` semantics).

    A parse failure here empties the whole reason join (every trade's exit shows
    as "—"), so it is logged instead of swallowed: the partially built dicts are
    still returned, because half the reasons are better than none and the caller
    has no fallback source (DeepR 2026-08-11 [ORTA]).
    """
    tags_by_id: dict[str, str] = {}
    type_by_id: dict[str, str] = {}
    if fills_df is None or fills_df.empty:
        return tags_by_id, type_by_id
    try:
        if "tags" in fills_df.columns:
            for oid, t in fills_df["tags"].items():
                tags_by_id.setdefault(str(oid), str(t or ""))
        if "type" in fills_df.columns:
            for oid, t in fills_df["type"].items():
                type_by_id.setdefault(str(oid), str(t or ""))
    except Exception:
        logging.warning(
            "fills report could not be indexed — trade exit reasons will be "
            "partially or fully missing",
            exc_info=True,
        )
    return tags_by_id, type_by_id


def _extract_trades(
    positions_df: pd.DataFrame | None,
    marker_shift_s: int = 0,
    fills_df: pd.DataFrame | None = None,
    decisions: list[dict] | None = None,
) -> list[dict]:
    """Extract per-trade entry/exit data for chart markers.

    Fills are stamped at bar CLOSE time (see ``_bars_from_df``), but chart
    candles use the bar's OPEN time (Bybit/TradingView convention). Subtract
    ``marker_shift_s`` (one bar interval, in seconds) so a fill on bar *i* snaps
    onto candle *i* instead of the next candle. Clamped so times never go
    negative.

    When ``fills_df`` + ``decisions`` are provided, an entry/exit reason is
    added to each trade: opening/closing_order_id → fills tags ("dr:<seq>"/
    "xr:<seq>"/"sl"/"tp"/"flip"/"eob") → decision-log join. If there is no tag,
    it falls back to the order type (STOP_MARKET→sl, LIMIT→tp); if that's also
    absent the fields stay None.
    """
    if positions_df is None or positions_df.empty:
        return []

    by_seq: dict[int, dict] = {}
    for d in decisions or []:
        if d.get("seq") is not None:
            by_seq[int(d["seq"])] = d
    tags_by_id, type_by_id = _fills_lookup(fills_df)

    def _to_unix(v) -> int:
        """Convert Nautilus timestamp (Timestamp, int-ns, or str) to Unix seconds.

        L5: the thresholds also cover the µs band — the old >1e18/>1e12 pair
        mistook microseconds for ms and stamped ~55,000 years forward (latent).
        Post-2001 real values: s ~1e9, ms ~1e12, µs ~1e15, ns ~1e18.
        """
        if v is None:
            return 0
        if hasattr(v, "timestamp"):  # pd.Timestamp
            return int(v.timestamp())
        iv = int(v)
        if iv > 100_000_000_000_000_000:  # > 1e17 → nanoseconds
            return iv // 1_000_000_000
        if iv > 100_000_000_000_000:  # > 1e14 → microseconds
            return iv // 1_000_000
        if iv > 100_000_000_000:  # > 1e11 → milliseconds
            return iv // 1_000
        return iv  # already seconds

    trades = []
    _dropped = 0
    for _, row in positions_df.iterrows():
        try:
            ts_open = _to_unix(row.get("ts_opened"))
            ts_close = _to_unix(row.get("ts_closed"))
            entry_px = float(str(row.get("avg_px_open", "0")).split()[0])
            exit_px = float(str(row.get("avg_px_close", "0")).split()[0])
            side = str(row.get("entry", "BUY"))
            pnl_raw = str(row.get("realized_pnl", "0"))
            pnl = float(pnl_raw.split()[0]) if pnl_raw else 0.0
            dur_ns = row.get("duration_ns", 0)
            dur_min = (int(dur_ns) // 60_000_000_000) if dur_ns else 0
            if marker_shift_s:
                ts_open = max(0, ts_open - marker_shift_s)
                ts_close = max(0, ts_close - marker_shift_s)

            # ── Entry reason: opening_order_id → "dr:<seq>" → decision record ──
            entry_reason = None
            entry_detail = None
            m = _DR_TAG_RE.search(tags_by_id.get(str(row.get("opening_order_id")), ""))
            if m:
                entry_detail = by_seq.get(int(m.group(1)))
                entry_reason = _reason_text(entry_detail)

            # ── Exit reason: closing_order_id tags / type fallback ──
            exit_reason = None
            exit_detail = None
            exit_kind = None
            close_id = row.get("closing_order_id")
            close_tags = tags_by_id.get(str(close_id), "")
            m = _XR_TAG_RE.search(close_tags)
            if m:
                exit_kind = "signal"
                exit_detail = by_seq.get(int(m.group(1)))
                exit_reason = _reason_text(exit_detail)
            elif "sl" in close_tags:
                exit_kind = "sl"
            elif "tp" in close_tags:
                exit_kind = "tp"
            elif "flip" in close_tags:
                exit_kind = "flip"
            elif "eob" in close_tags:
                exit_kind = "eob"
            else:
                ctype = type_by_id.get(str(close_id), "")
                if ctype == "STOP_MARKET":
                    exit_kind = "sl"
                elif ctype == "LIMIT":
                    exit_kind = "tp"
            if exit_reason is None and exit_kind:
                exit_reason = _EXIT_KIND_LABELS.get(exit_kind)

            trades.append(
                {
                    "entry_time": ts_open,
                    "exit_time": ts_close,
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "side": side,
                    "pnl": pnl,
                    "dur_min": dur_min,
                    "entry_reason": entry_reason,
                    "exit_reason": exit_reason,
                    "exit_kind": exit_kind,
                    "entry_detail": entry_detail,
                    "exit_detail": exit_detail,
                }
            )
        except Exception:
            # An unparseable position row — if silently dropped, the trades
            # list stays BELOW n_trades (metric↔chart inconsistency, no trace).
            _dropped += 1
    if _dropped:
        logging.getLogger(__name__).warning(
            "_extract_trades: %d position row(s) could not be parsed (dropped from trade list)",
            _dropped,
        )
    return trades


def _equity_curve(
    positions_df: pd.DataFrame | None,
    starting_cash: float | None = None,
) -> tuple[list[float], list[str]]:
    """Return (equity_values, iso_date_labels) aligned by trade close time."""
    _sc = starting_cash if starting_cash is not None else STARTING_CASH
    if positions_df is None or positions_df.empty:
        return [_sc], [""]
    pnls = _parse_money_column(positions_df["realized_pnl"])
    dates_ns = None
    if "ts_closed" in positions_df.columns:
        # M905: if an OPEN position remains at the end of the run, ts_closed
        # carries pd.NA and the column becomes object dtype → to_numpy(dtype='int64')
        # raised TypeError, turning the WHOLE run into 'error' (_metrics had a
        # try/except here, this didn't). Safe conversion: keep the NAs, leave unsorted.
        try:
            dates_ns = positions_df["ts_closed"].to_numpy(dtype="int64")
            order = np.argsort(dates_ns)
            pnls = pnls[order]
            dates_ns = dates_ns[order]
        except (TypeError, ValueError):
            dates_ns = None  # open position / NA → curve without dates (metrics fine)
    curve = _sc + np.cumsum(pnls)
    values = [_sc] + [float(x) for x in curve]
    if dates_ns is not None:
        labels = [""] + [
            pd.Timestamp(int(t), unit="ns", tz="UTC").strftime("%Y-%m-%d")
            for t in dates_ns
        ]
    else:
        labels = [""] * len(values)
    return values, labels


def run_backtest(
    strategy_name: str,
    params: dict,
    bars_df: pd.DataFrame,
    iteration_id: int = 0,
    rationale: str = "",
) -> IterationResult:
    if strategy_name not in STRATEGY_REGISTRY:
        return IterationResult(
            id=iteration_id,
            strategy=strategy_name,
            params=params,
            metrics={},
            equity_curve=[],
            rationale=rationale,
            error=f"Unknown strategy: {strategy_name}",
            timestamp=datetime.now(UTC),
        )

    strat_cls, cfg_cls = STRATEGY_REGISTRY[strategy_name]
    engine: BacktestEngine | None = None
    try:
        instrument = _make_bybit_instrument()  # BTCUSDT.BYBIT_LINEAR (default)
        active_bar_type = _make_bybit_bar_type(instrument.id, "1")
        active_venue = instrument.id.venue

        config = BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(bypass_logging=_BYPASS_LOGGING),
        )
        engine = BacktestEngine(config=config)
        # allow_short=True requires a MARGIN account so the engine accepts
        # SELL orders that open short positions (CASH otherwise).
        _allow_short = bool(params.get("allow_short", False))
        engine.add_venue(
            venue=active_venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN if _allow_short else AccountType.CASH,
            starting_balances=[Money(STARTING_CASH, USDT)],
            base_currency=None,
            fee_model=_fee_model_for(instrument),  # crypto→Bybit maker/taker
        )
        engine.add_instrument(instrument)

        df = _prepare_df(bars_df)
        bars = _bars_from_df(active_bar_type, instrument, df)
        if not bars:
            raise RuntimeError("bar builder returned no bars")
        engine.add_data(bars)

        strat_kwargs = dict(params)
        strat_kwargs.setdefault("trade_size", Decimal("0.01"))
        if "trade_size" in strat_kwargs and not isinstance(
            strat_kwargs["trade_size"], Decimal
        ):
            strat_kwargs["trade_size"] = Decimal(str(strat_kwargs["trade_size"]))

        cfg = cfg_cls(
            instrument_id=instrument.id, bar_type=active_bar_type, **strat_kwargs
        )
        strategy = strat_cls(config=cfg)
        engine.add_strategy(strategy)

        engine.run()

        positions_df = engine.trader.generate_positions_report()
        # Collect MTM equity snapshots from strategy (Phase 1)
        mtm_equity = getattr(strategy, "_mtm_equity", None)
        metrics = _metrics(
            engine,
            positions_df,
            mtm_equity=mtm_equity,
            annualization=_periods_per_year(active_bar_type, instrument, df),
            mtm_ts=getattr(strategy, "_mtm_ts", None),
        )
        equity, equity_dates = _equity_curve(positions_df)
        trades = _extract_trades(
            positions_df,
            marker_shift_s=_bar_interval_ns(active_bar_type) // 1_000_000_000,
        )

        return IterationResult(
            id=iteration_id,
            strategy=strategy_name,
            params=params,
            metrics=metrics,
            equity_curve=equity,
            equity_dates=equity_dates,
            trades=trades,
            rationale=rationale,
            error=None,
            timestamp=datetime.now(UTC),
        )
    except Exception as e:
        return IterationResult(
            id=iteration_id,
            strategy=strategy_name,
            params=params,
            metrics={},
            equity_curve=[],
            rationale=rationale,
            error=f"{type(e).__name__}: {e}",
            timestamp=datetime.now(UTC),
        )
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def run_composed_backtest(
    spec,
    bars_df: pd.DataFrame,
    iteration_id: int = 0,
    rationale: str = "",
    *,
    instrument=None,
    bar_type: BarType | None = None,
    venue: Venue | None = None,
    progress_fn=None,
    initial_capital: float | None = None,
    commission_bps_override: float | None = None,
) -> IterationResult:
    """Run a user-composed strategy (SignalBlock list) through Nautilus.

    When `instrument`/`bar_type`/`venue` are all None, defaults to
    BTCUSDT.BYBIT (1-minute bars). Passing an alternative instrument
    (e.g. an index Equity proxy) reroutes the same pipeline onto that
    instrument without touching the strategy or composer layers.

    `progress_fn(msg: str)` is called at each major stage boundary so callers
    can surface real-time progress (e.g. via a polling endpoint).
    """
    import json as _json

    from composer import ComposedStrategy, ComposedStrategyConfig

    def _p(msg: str) -> None:
        if progress_fn is not None:
            try:
                progress_fn(msg)
            except Exception:
                pass

    err = spec.validate()
    if err:
        return IterationResult(
            id=iteration_id,
            strategy=f"composed:{spec.name}",
            params={"blocks": len(spec.blocks)},
            metrics={},
            equity_curve=[],
            rationale=rationale,
            error=f"Invalid spec: {err}",
            timestamp=datetime.now(UTC),
        )

    n_entry = sum(1 for b in spec.blocks if getattr(b, "role", "") == "entry")
    n_exit = sum(1 for b in spec.blocks if getattr(b, "role", "") == "exit")
    _p(
        f"Spec validated · {len(spec.blocks)} blocks ({n_entry} entry, {n_exit} exit) · {spec.name}"
    )

    use_custom = instrument is not None
    if use_custom and (bar_type is None or venue is None):
        return IterationResult(
            id=iteration_id,
            strategy=f"composed:{spec.name}",
            params={"spec_id": spec.id},
            metrics={},
            equity_curve=[],
            rationale=rationale,
            error="instrument, bar_type, and venue must be passed together",
            timestamp=datetime.now(UTC),
        )

    engine: BacktestEngine | None = None
    try:
        if use_custom:
            active_instrument = instrument
            active_bar_type = bar_type
            active_venue = venue
            active_instrument_id = instrument.id
        else:
            active_instrument = _make_bybit_instrument(
                fee_bps_override=commission_bps_override,
            )
            active_bar_type = _make_bybit_bar_type(active_instrument.id, "1")
            active_venue = active_instrument.id.venue
            active_instrument_id = active_instrument.id

        # Data info
        n_bars = len(bars_df)
        try:
            date_start = bars_df.index[0].date()
            date_end = bars_df.index[-1].date()
            _p(f"Preparing data · {n_bars:,} bars · {date_start} → {date_end}")
        except Exception:
            _p(f"Preparing data · {n_bars:,} bars")

        config = BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(bypass_logging=_BYPASS_LOGGING),
        )
        engine = BacktestEngine(config=config)
        # Determine account type from instrument type, not from use_custom flag.
        # Equity (index proxy) → USD CASH; CurrencyPair (crypto) → USDT CASH/MARGIN.
        is_equity = use_custom and isinstance(active_instrument, Equity)
        # M1128: inverse (coin-margined) crypto is USD-quoted (BTCUSD); if the
        # account is opened with USDT the RiskEngine SILENTLY rejects all entries
        # (0 trades). Use the instrument's real quote currency.
        _quote_ccy = getattr(active_instrument, "quote_currency", None)
        _is_usd_quote = _quote_ccy is not None and str(_quote_ccy) == "USD"
        _crypto_ccy = USD if _is_usd_quote else USDT
        _cap = initial_capital if initial_capital is not None else STARTING_CASH
        if is_equity:
            _account_type = AccountType.CASH
            _balances = [Money(_cap, USD)]
            _base = USD
        elif getattr(spec, "allow_short", False):
            _account_type = AccountType.MARGIN
            _balances = [Money(_cap, _crypto_ccy)]
            _base = None
        else:
            _account_type = AccountType.CASH
            _balances = [Money(_cap, _crypto_ccy)]
            _base = None

        _p(
            f"Setting up engine · {str(active_venue)} · {_account_type.name} · starting: ${_cap:,.0f}"
        )

        # Commission: override fee already baked into active_instrument via fee_bps_override;
        # MakerTakerFeeModel reads maker_fee/taker_fee from the instrument at fill time.
        _fee = _fee_model_for(active_instrument)

        engine.add_venue(
            venue=active_venue,
            oms_type=OmsType.NETTING,
            account_type=_account_type,
            starting_balances=_balances,
            base_currency=_base,
            bar_adaptive_high_low_ordering=getattr(spec, "use_bracket", False),
            # Commission by instrument type: crypto→Bybit maker/taker,
            # US stock/ETF (QQQ etc.)→Interactive Brokers Fixed (see _fee_model_for).
            fee_model=_fee,
            fill_model=(
                FillModel(prob_slippage=1.0, random_seed=SLIPPAGE_RANDOM_SEED)
                if getattr(spec, "model_slippage", False)
                else None
            ),
        )
        engine.add_instrument(active_instrument)

        df = _prepare_df(bars_df)
        _p(
            f"Building bar objects · {len(df):,} bars (OHLC validation: {n_bars - len(df)} rows dropped)"
        )
        bars = _bars_from_df(active_bar_type, active_instrument, df)
        if not bars:
            raise RuntimeError("bar builder returned no bars")
        engine.add_data(bars)

        # Multi-timeframe trend filter: load and add secondary bars if requested
        #
        # FAIL CLOSED (DeepR 2026-08-17 [YÜKSEK]). Buradaki her arıza
        # yutuluyordu: `secondary_bar_type_obj = None` ile koşu sürüyor,
        # strateji trend bias'ı olmadığı için filtreyi HİÇ uygulamıyor, sonuç
        # yine "başarılı" olarak ve trend filtreli spec adıyla kaydediliyordu.
        # Sonra o sonuç aday seçimine ve yayına giriyordu — yani ekranda yazan
        # strateji ile ölçülen strateji farklıydı ve hiçbir yerde iz yoktu.
        # Progress mesajı iz DEĞİL: akış geçici, kayıt kalıcı.
        #
        # `to_nautilus`'ın `UnsupportedStrategy` duruşu ile aynı: çeviremediği
        # kuralı sessizce düşürmek yerine reddediyor, çünkü aksi hâlde EKRANDA
        # OLANDAN BAŞKA bir stratejinin metriklerini döndürürdü.
        secondary_bar_type_obj = None
        if getattr(spec, "trend_filter", False) and getattr(
            spec, "trend_interval", None
        ):
            _trend_interval = spec.trend_interval
            # Ana TF ile aynı olduğu için BİLEREK atlandı mı, yoksa veri mi
            # gelmedi — ikisi de `trend_df is None` ile bitiyordu, ayırt
            # edilmeleri şart (biri meşru, öbürü arıza).
            _same_tf = False
            trend_df = None
            try:
                _p(f"Loading trend filter data · interval={_trend_interval}…")
                _trend_start = bars_df.index[0].to_pydatetime()
                _trend_end = bars_df.index[-1].to_pydatetime()
                if _trend_start.tzinfo is None:
                    _trend_start = _trend_start.replace(tzinfo=UTC)
                if _trend_end.tzinfo is None:
                    _trend_end = _trend_end.replace(tzinfo=UTC)
                # External-catalog Equity (venue != POLYGON — not the index
                # proxy): trend bars also come from the same external catalog.
                # Katalogda trend dilimi yoksa ValueError → koşu BAŞARISIZ olur
                # (eskiden sessizce tek-TF'e düşüyordu; bkz. yukarıdaki not).
                _is_ext_equity = (
                    isinstance(active_instrument, Equity)
                    and active_instrument.id.venue != POLYGON
                )
                if _is_ext_equity:
                    _BYBIT_TO_DSL = {
                        "1": "1-MINUTE",
                        "5": "5-MINUTE",
                        "15": "15-MINUTE",
                        "30": "30-MINUTE",
                        "60": "1-HOUR",
                        "240": "4-HOUR",
                        "720": "12-HOUR",
                        "D": "1-DAY",
                    }
                    _gran = (
                        _trend_interval
                        if "-" in _trend_interval
                        else _BYBIT_TO_DSL[_trend_interval]
                    )
                    _sec_bar_type = _make_external_bar_type(active_instrument.id, _gran)
                else:
                    _sec_bar_type = _make_bybit_bar_type(
                        active_instrument.id, _trend_interval
                    )

                # If the trend TF equals the main TF, no second feed is added:
                # the strategy treats every bar as a "trend bar" and never opens
                # a trade (0 trades).
                if _sec_bar_type.spec == active_bar_type.spec:
                    _p(
                        f"Trend TF ({_trend_interval}) is the same as the main TF — "
                        "skipping trend filter, continuing with single TF"
                    )
                    _same_tf = True
                elif _is_ext_equity:
                    from data import load_external_bars as _load_ext

                    trend_df = _load_ext(
                        str(active_instrument.id),
                        _gran,
                        start=_trend_start,
                        end=_trend_end,
                    )
                else:
                    from data import load_bybit_bars as _load_bybit

                    trend_sym = getattr(
                        active_instrument.id.symbol,
                        "value",
                        str(active_instrument.id.symbol),
                    )
                    # M1234: the category is derived from the main run's venue —
                    # a fixed 'linear' mixed linear-perp price into a spot run,
                    # and on inverse BTCUSD wasn't found in linear, silently
                    # disabling the filter.
                    _vname = str(active_venue)
                    if "SPOT" in _vname:
                        _trend_cat = "spot"
                    elif "INVERSE" in _vname:
                        _trend_cat = "inverse"
                    else:
                        _trend_cat = "linear"
                    trend_df = _load_bybit(
                        symbol=trend_sym,
                        interval=_trend_interval,
                        category=_trend_cat,
                        start=_trend_start,
                        end=_trend_end,
                    )
            except Exception as _te:
                raise RuntimeError(
                    f"trend filter ({_trend_interval}) was requested but its data "
                    f"could not be loaded: {_te}"
                ) from _te

            # Doğrulama try'ın DIŞINDA: içeride olsaydı kendi `raise`'imi kendi
            # `except`'im yakalar ve düzeltme hiç yürürlüğe girmezdi.
            if not _same_tf:
                if trend_df is None or trend_df.empty:
                    raise RuntimeError(
                        f"trend filter ({_trend_interval}) was requested but the "
                        "loader returned no bars for the run's date range"
                    )
                trend_bars = _bars_from_df(
                    _sec_bar_type, active_instrument, _prepare_df(trend_df)
                )
                if not trend_bars:
                    # Eskiden `secondary_bar_type_obj` BURADA da atanıyordu:
                    # strateji hiç gelmeyecek bir ikinci akışı bekliyordu.
                    raise RuntimeError(
                        f"trend filter ({_trend_interval}) was requested but no "
                        "valid bars survived OHLC validation"
                    )
                secondary_bar_type_obj = _sec_bar_type
                engine.add_data(trend_bars)
                _p(f"Trend bars added · {len(trend_bars):,} bars · {_trend_interval}")

        cfg = ComposedStrategyConfig(
            instrument_id=active_instrument_id,
            bar_type=active_bar_type,
            spec_json=_json.dumps(spec.to_dict()),
            trade_size=Decimal(str(spec.trade_size)),
            secondary_bar_type=secondary_bar_type_obj,
        )
        _composed_strategy = ComposedStrategy(config=cfg)
        engine.add_strategy(_composed_strategy)

        _p(
            f"Simulation started · {len(bars):,} bars to process · order_type={spec.order_type}"
        )
        engine.run()
        _p("Simulation completed · collecting results…")

        positions_df = engine.trader.generate_positions_report()
        n_trades = (
            len(positions_df)
            if positions_df is not None and not positions_df.empty
            else 0
        )
        _p(f"Calculating metrics · {n_trades} positions found")

        # Collect MTM equity snapshots from strategy (Phase 1)
        mtm_equity = getattr(_composed_strategy, "_mtm_equity", None)
        metrics = _metrics(
            engine,
            positions_df,
            mtm_equity=mtm_equity,
            annualization=_periods_per_year(active_bar_type, active_instrument, df),
            mtm_ts=getattr(_composed_strategy, "_mtm_ts", None),
            starting_cash=_cap,
            slippage_model_active=bool(getattr(spec, "model_slippage", False)),
            price_increment=float(active_instrument.price_increment),
        )
        equity, equity_dates = _equity_curve(positions_df, starting_cash=_cap)
        # Entry/exit reasons: decision log (same lifecycle as _mtm_equity) +
        # fills report tag join
        reason_fills_df, _fills_error = _reason_fills_report(engine.trader)
        if _fills_error:
            metrics["exit_reasons_error"] = _fills_error
        trades = _extract_trades(
            positions_df,
            marker_shift_s=_bar_interval_ns(active_bar_type) // 1_000_000_000,
            fills_df=reason_fills_df,
            decisions=getattr(_composed_strategy, "_decision_log", None),
        )

        _p(
            f"Completed · PnL={metrics.get('pnl', 0):+.2f} USDT · win_rate={metrics.get('win_rate', 0) * 100:.1f}%"
        )

        return IterationResult(
            id=iteration_id,
            strategy=f"composed:{spec.name}",
            params={"spec_id": spec.id, "n_blocks": len(spec.blocks)},
            metrics=metrics,
            equity_curve=equity,
            equity_dates=equity_dates,
            trades=trades,
            rationale=rationale,
            error=None,
            timestamp=datetime.now(UTC),
        )
    except Exception as e:
        _p(f"An error occurred: {type(e).__name__}: {e}")
        return IterationResult(
            id=iteration_id,
            strategy=f"composed:{spec.name}",
            params={"spec_id": spec.id},
            metrics={},
            equity_curve=[],
            rationale=rationale,
            error=f"{type(e).__name__}: {e}",
            timestamp=datetime.now(UTC),
        )
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


if __name__ == "__main__":
    from datetime import datetime, timedelta

    from data import load_bybit_bars

    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    bars = load_bybit_bars("BTCUSDT", interval="1", start=start, end=end)
    print(f"loaded {len(bars)} bars")
    r = run_backtest("ma_crossover", {"fast": 10, "slow": 30}, bars)
    print(r)
