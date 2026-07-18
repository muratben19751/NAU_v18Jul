"""NautilusTrader BacktestEngine wrapper.

Builds a fresh engine per call. Runs the chosen strategy over Bybit BTCUSDT
bars and extracts summary metrics + equity curve.

Wiki References
---------------
Bkz: [[backtest_node]], [[backtesting_guide]], [[environment_contexts]], [[data_wranglers]], [[precision_modes]], [[portfolio]], [[v1_to_v2_migration_lessons]], [[index_backtest_via_equity_proxy]]

Low-level API path; [[backtesting_guide]] "BacktestEngine seç eğer:" listesine uyar. `_bars_from_df` v2'de `BarDataWrangler.process` kaldırılınca yazılan helper — [[data_wranglers]] v1→v2 bölümü. Sharpe nan bug: bkz. [[portfolio]] ve [[v1_to_v2_migration_lessons]].
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
from nautilus_trader.backtest.models import MakerTakerFeeModel
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


# L43: tek kaynak app_constants — composer da oradan okur. Geriye uyum için
# backtest.STARTING_CASH adı korunur (yeniden ihraç).
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


# M5: komisyonlar ARTIK MODELLENIYOR — instrument maker/taker fee +
# MakerTakerFeeModel pinli 1.230.0'da çalışıyor (canlı doğrulandı; önceki
# 'engine-level fee injection not supported' yorumu YANLIŞTI). Bybit
# venue'ları bu oranlarla komisyon keser; PnL artık net-komisyon.
BYBIT_TAKER_FEE_BPS: float = 5.5  # 0.055% Bybit taker fee
BYBIT_MAKER_FEE_BPS: float = 1.0  # 0.01%  Bybit maker fee
# Slippage BİLEREK modellenmedi: Nautilus FillModel'i olasılıksal (deterministik
# backtest'i bozar); sabit yalnız harici analiz/testler için duruyor.
SLIPPAGE_BPS: float = 2.0  # ~1 tick slippage estimate per fill (modellenmedi)

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


def _make_bybit_instrument(
    symbol: str = "BTCUSDT",
    base: str = "BTC",
    quote: str | None = None,
    category: str = "linear",
) -> CurrencyPair:
    """Bybit crypto pair for a product category (spot/linear/inverse).

    Each category maps to a distinct Venue (BYBIT_SPOT/BYBIT_LINEAR/BYBIT_INVERSE)
    so their InstrumentId + BarType — and thus catalog keys — stay disjoint.
    Inverse (coin-margined) contracts quote in USD and use the ...USD symbol
    (BTCUSDT → BTCUSD); spot/linear quote in USDT. When ``quote`` is None it is
    derived from the category. Precision is pinned per-symbol to NAU's
    universe.yaml (BTCUSDT→sp3, SOLUSDT→sp2, XRPUSDT→sp1 ...) so backtests here
    use the same instrument spec as the NAU catalog.
    """
    market_symbol = _bybit_market_symbol(symbol, category)
    if quote is None:
        quote = _bybit_quote(category)
    sym = Symbol(market_symbol)
    iid = InstrumentId(symbol=sym, venue=_bybit_venue(category))
    # Precision is keyed by the canonical (USDT) symbol; inverse BTCUSD shares
    # BTC precision, so fall back to the market symbol then the default.
    price_prec, size_prec, tick = _BYBIT_SPECS.get(
        symbol.upper(),
        _BYBIT_SPECS.get(market_symbol.upper(), _BYBIT_DEFAULT_SPEC),
    )
    size_step = "1" if size_prec == 0 else "0." + "0" * (size_prec - 1) + "1"
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
        # M5: gerçek Bybit komisyon oranları — MakerTakerFeeModel bunları okur.
        maker_fee=Decimal(str(BYBIT_MAKER_FEE_BPS / 10_000)),
        taker_fee=Decimal(str(BYBIT_TAKER_FEE_BPS / 10_000)),
        ts_event=0,
        ts_init=0,
    )


def _make_bybit_bar_type(instrument_id: InstrumentId, interval: str) -> BarType:
    """`interval` matches Bybit kline codes: "1","5","15","60","240","D"."""
    step = {
        "1": "1-MINUTE",
        "5": "5-MINUTE",
        "15": "15-MINUTE",
        "60": "1-HOUR",
        "240": "4-HOUR",
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

    L40 (venue ikiliği, bilinçli): burada venue=POLYGON — bu, uygulamanın
    KENDİ index-CSV proxy kimliğidir ve NAU kataloğunun gerçek-borsa
    kimlikleriyle (SPY.ARCA, QQQ.NASDAQ…) ASLA çapraz eşleşmez. Harici
    (NAU) katalog yolu zaten gerçek venue id'leriyle çalışır
    (load_external_bars + EXTERNAL_PEER_BASKET); iki dünya bilinçli ayrıdır.

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
    step = "1-DAY" if granularity == "1d" else "1-MINUTE"
    return BarType.from_str(f"{instrument_id}-{step}-LAST-EXTERNAL")


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
        # L21: sessiz veri kaybı gözlemlenebilir olsun — üç çağrı yolu da
        # (run_backtest, composed, trend-filter) bu tek noktadan loglanır.
        logging.getLogger(__name__).warning(
            "_prepare_df: OHLC-invaryant bozan %d satır atıldı (%d kaldı)",
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
    bars: list[Bar] = []
    for i in range(len(df)):
        # Shift open-time index → bar close (Nautilus ts_event convention).
        ts = int(ts_ns[i]) + interval_ns
        bars.append(
            Bar(
                bar_type,
                Price(float(o[i]), pp),
                Price(float(h[i]), pp),
                Price(float(lo[i]), pp),
                Price(float(c[i]), pp),
                Quantity(float(v[i]), sp),
                ts,
                ts,
            )
        )
    return bars


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


def _periods_per_year(bar_type=None, instrument=None) -> int:
    """H5/M35/L42: yıllıklandırma tabanını KAYNAK + BAR ARALIĞINDAN türet.

    Getiriler bar-frekansındadır; doğru katsayı sqrt(yıldaki bar sayısı)dır.
    Eski kod koşulsuz 365 kullanıyordu — 1m veride Sharpe ~38× ezik, 1h'de
    ~4.9× ezik ve TF'ler arası kıyas geçersizdi.

    - Kripto (7/24): 365 gün × 86400sn / bar_sn
    - Equity/Index (hafta içi): gün-içi → 252 gün × 6.5 saat / bar_sn;
      günlük+ → 252 (NAU konvansiyonu)
    - Türetilemezse muhafazakâr 365 (günlük varsayımı) döner.
    """
    bar_sec = 0.0
    try:
        # spec str'i sürümden bağımsız en sağlam yüzey: '1-MINUTE-LAST'
        # (aggregation alanı pyo3 int enum — adı str()'den çıkmıyor).
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
    # M387: equity/index — WEEK/MONTH için 252 yanlıştı (haftalık Sharpe ~2.2×,
    # aylık ~4.6× şişik). Doğru yıllık periyot sayısı: gün 252, hafta 52, ay 12.
    if bar_sec >= 27 * 86400:  # ~aylık
        return 12
    if bar_sec >= 6 * 86400:  # ~haftalık
        return 52
    if bar_sec >= 86400:  # günlük+
        return 252  # NAU 252 işlem günü
    return max(1, int(252 * 6.5 * 3600 / bar_sec))  # gün-içi equity (RTH)


def _sharpe_manual(equity_series: list[float], annualization: int = 365) -> float:
    """Bar-çözünürlüklü equity serisinden Sharpe.

    Getiriler ardil-bar yüzde değişimleridir (resample YOK — H5: eski
    docstring'in 'daily resampling' iddiası gerçek dışıydı); yıllıklandırma
    sqrt(annualization) ile yapılır ve ``annualization`` YILDAKİ BAR SAYISI
    olmalıdır (bkz. _periods_per_year). 2'den az nokta / sıfır std → NaN.
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


def _max_dd_from_series(equity_series: list[float]) -> float:
    """Compute max drawdown from any equity series (bar or trade resolution).

    SÖZLEŞME (H11/L36): dönüş NEGATİF KESİRDİR (%12 çekilme → -0.12; düşüş
    yoksa 0.0). Uygulama içi tüm tüketiciler (junk kapısı max_dd>=0, MC eşiği
    dd_p50<-25, wfo abs(dd)) bu konvansiyonu varsayar. NAU POZİTİF YÜZDE
    saklar — NAU sınırında ``nau_max_drawdown`` / ``metrics['max_dd_pct']``
    kullanın; kalıpları kelimesi kelimesine kopyalamayın.
    """
    if len(equity_series) < 2:
        return 0.0
    arr = np.array(equity_series, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    return float(dd.min())


def nau_max_drawdown(max_dd: float | None) -> float | None:
    """H11: uygulama-içi negatif-kesir max_dd → NAU pozitif-yüzde dönüşümü.

    Kayıt birleşimi / çapraz-sistem kıyası için tek resmi köprü.
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
) -> dict:
    """Compute backtest metrics.

    Args:
        engine: The BacktestEngine after run().
        positions_df: Closed positions report.
        mtm_equity: Optional bar-resolution mark-to-market equity snapshots
            captured by ComposedStrategy._mtm_equity. When provided, max_dd
            and Sharpe are computed from MTM rather than realized PnL.
        annualization: H5 — yıldaki bar sayısı (_periods_per_year); None ise
            365 (günlük varsayımı, eski davranış).
        mtm_ts: L19 — mtm_equity ile hizalı bar zaman damgaları (ns);
            verilirse ``equity_curve_mtm`` [(iso_ts, eq)] alanı üretilir.
    """
    if annualization is None:
        annualization = 365
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
            # L33: boş koşuda NaN yerine 0.0 (NAU kuralı; NaN JSON'u bozar).
            "profit_factor": 0.0,
            "avg_return": float("nan"),
            "volatility": float("nan"),
            "max_winner": 0.0,
            "max_loser": 0.0,
            "starting_cash": STARTING_CASH,
            "long_ratio": float("nan"),
            "avg_duration_mins": float("nan"),
            "commission_total": 0.0,
            "slippage_total": 0.0,
            "runner": "BacktestEngine",
            # Phase 1 additions
            "sharpe_nautilus": float("nan"),
            "sharpe_per_trade": float("nan"),
            "annualization": annualization,
            "max_dd_mtm": 0.0,
            "equity_curve_realized": [STARTING_CASH],
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
            pass

    # Commissions
    commission_total = 0.0
    if "commissions" in positions_df.columns:
        try:

            def _parse_commissions(v):
                if isinstance(v, list):
                    s = 0.0
                    for item in v:
                        try:
                            s += float(str(item).split()[0])
                        except Exception:
                            pass
                    return s
                try:
                    return float(str(v).split()[0])
                except Exception:
                    return 0.0

            commission_total = float(
                positions_df["commissions"].apply(_parse_commissions).sum()
            )
        except Exception:
            pass

    # Slippage from order fills
    slippage_total = 0.0
    try:
        fills_df = engine.trader.generate_order_fills_report()
        if not fills_df.empty and "slippage" in fills_df.columns:
            slippage_total = float(
                pd.to_numeric(fills_df["slippage"], errors="coerce").sum()
            )
    except Exception:
        pass

    # Realized equity curve for max drawdown (backward-compat) + MTM if available
    dd_pnls = pnls
    if "ts_closed" in positions_df.columns:
        try:
            _order = np.argsort(positions_df["ts_closed"].to_numpy(dtype="int64"))
            dd_pnls = pnls[_order]
        except Exception:
            dd_pnls = pnls
    realized_equity = list(STARTING_CASH + np.cumsum(dd_pnls))
    realized_equity_full = [STARTING_CASH] + realized_equity

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
        """Anahtar yoksa NaN; gerçek 0.0 değerini korur (falsy-zero tuzağı yok)."""
        v = d.get(key)
        if v is None:
            return float("nan")
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    sharpe_nautilus = _stat(ret, "Sharpe Ratio (252 days)")
    sortino = _stat(ret, "Sortino Ratio (252 days)")
    volatility = _stat(ret, "Returns Volatility (252 days)")
    avg_return = _stat(ret, "Average (Return)")
    profit_factor = _stat(ret, "Profit Factor")
    avg_win_ret = _stat(ret, "Average Win (Return)")
    avg_loss_ret = _stat(ret, "Average Loss (Return)")

    # H5: bar-frekanslı manuel Sharpe — annualization ÇAĞIRANDAN gelir
    # (_periods_per_year: kaynak + bar aralığı). MTM eğrisi (bar-çözünürlüklü)
    # kullanılıyorsa bar-frekanslı annualization DOĞRU.
    _using_mtm = bool(mtm_equity and len(mtm_equity) > 5)
    if _using_mtm:
        sharpe_manual = _sharpe_manual(mtm_equity, annualization=annualization)
        sharpe = sharpe_manual if not math.isnan(sharpe_manual) else sharpe_nautilus
    else:
        # H610: MTM yok (registry stratejileri — MACrossover/RSIMeanReversion).
        # realized_equity_full TRADE-çözünürlüklüdür; buna bar-frekanslı
        # annualization (1m'de sqrt(525600)) uygulamak Sharpe'ı ~725× ŞİŞİRİYORDU.
        # Frekans-doğru Nautilus 252-gün değerini birincil yap; o da yoksa
        # per-trade sqrt(n) tabanı.
        sharpe = sharpe_nautilus

    # M35: NAU kıyası için per-trade Sharpe (NAU_ev: mean/std × sqrt(n_trades)
    # per-TRADE getirilerden). M620: NAU population std (ddof=0) kullanır ve
    # dejenere (n<2 / std=0) durumda NaN yerine 0.0 döndürür — hizalandı.
    # (Getiri tabanı NAU'da koşan-equity; burada sabit STARTING_CASH — uzun
    # bileşik koşularda küçük ölçek farkı, çapraz-sistem kıyasında dikkate al.)
    sharpe_per_trade = 0.0
    if n_trades >= 2:
        _tr = np.asarray(pnls, dtype=float) / STARTING_CASH
        _std = float(np.std(_tr, ddof=0))  # M620: population std (NAU paritesi)
        if _std > 0:
            sharpe_per_trade = float(np.mean(_tr)) / _std * math.sqrt(n_trades)
    if not _using_mtm and (sharpe is None or math.isnan(sharpe)):
        # Nautilus Sharpe de yoksa per-trade tabanına düş (frekans-doğru).
        sharpe = sharpe_per_trade

    # L33: kayıpsız koşuda Nautilus PF=inf döner — NAU sonlu cap'i (99.0);
    # inf/NaN standart-dışı JSON üretir ve sıralamayı bozar.
    if math.isinf(profit_factor):
        profit_factor = 99.0 if profit_factor > 0 else 0.0

    # L19: bar-çözünürlüklü MTM equity eğrisi (ts, eq) — UI'daki realized
    # basamak-eğrisi pozisyon-içi dipleri göstermiyordu; max_dd bu seriden
    # geliyor, artık eğri de gösterilebilir. >5000 nokta uniform seyreltilir.
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
        else (
            avg_win_ret * STARTING_CASH if not np.isnan(avg_win_ret) else float("nan")
        )
    )
    avg_loss_usd = (
        float(pnl_stats["Avg Loser"])
        if "Avg Loser" in pnl_stats
        else (
            avg_loss_ret * STARTING_CASH if not np.isnan(avg_loss_ret) else float("nan")
        )
    )
    max_winner = _stat(pnl_stats, "Max Winner")
    max_loser = _stat(pnl_stats, "Max Loser")
    long_ratio = _stat(gen, "Long Ratio")

    return {
        "starting_cash": STARTING_CASH,
        "pnl": total_pnl,
        "pnl_pct": total_pnl / STARTING_CASH,
        "sharpe": sharpe,
        "sortino": sortino,
        "volatility": volatility,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "max_dd": max_dd,
        # H11: NAU sınır kıyası için pozitif-yüzde alanı (iç konvansiyon
        # negatif kesir olarak KALIR — bkz. _max_dd_from_series docstring'i).
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
        "slippage_total": slippage_total,
        "runner": "BacktestEngine",
        # Phase 1 additions
        "sharpe_nautilus": sharpe_nautilus,  # Nautilus 252-day value kept for audit
        "sharpe_per_trade": sharpe_per_trade,  # M35: NAU per-trade tabanı
        "annualization": annualization,  # H5: gerçek bar/yıl tabanı
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
    "flip": "Ters sinyal (flip)",
    "eob": "Backtest sonu",
}

_DR_TAG_RE = re.compile(r"dr:(\d+)")
_XR_TAG_RE = re.compile(r"xr:(\d+)")


def _reason_text(decision: dict | None) -> str | None:
    """Karar kaydını insan-okur tek satıra çevir.

    Örn: "MA Kesişimi (fast=10, slow=30, up) · fast 42.1 / slow 41.8".
    AND mantığında birden çok blok " + " ile birleşir.
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


def _fills_lookup(fills_df: pd.DataFrame | None) -> tuple[dict, dict]:
    """Fills raporunu tek geçişte {order_id: tags/type} dict'lerine çevir.

    Eski hali her trade için pandas ``.loc`` yapıyordu — 1k trade'de profil
    ~0.5s gösterdi. Çoklu-fill emirlerde İLK satır kazanır (eski
    ``iloc[0]`` semantiğiyle birebir).
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
        pass
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

    ``fills_df`` + ``decisions`` verilirse her trade'e giriş/çıkış sebebi
    eklenir: opening/closing_order_id → fills tag'leri ("dr:<seq>"/"xr:<seq>"/
    "sl"/"tp"/"flip"/"eob") → karar günlüğü join'i. Tag yoksa emir tipinden
    fallback (STOP_MARKET→sl, LIMIT→tp); o da yoksa alanlar None kalır.
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

        L5: eşikler µs bandını da kapsar — eski >1e18/>1e12 ikilisi
        mikrosaniyeyi ms sanıp ~55.000 yıl ileri damgalıyordu (latent).
        2001-sonrası gerçek değerler: sn ~1e9, ms ~1e12, µs ~1e15, ns ~1e18.
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

            # ── Giriş sebebi: opening_order_id → "dr:<seq>" → karar kaydı ──
            entry_reason = None
            entry_detail = None
            m = _DR_TAG_RE.search(tags_by_id.get(str(row.get("opening_order_id")), ""))
            if m:
                entry_detail = by_seq.get(int(m.group(1)))
                entry_reason = _reason_text(entry_detail)

            # ── Çıkış sebebi: closing_order_id tag'leri / tip fallback ──
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
            # Parse edilemeyen pozisyon satırı — sessizce düşürülürse trades
            # listesi n_trades'in ALTINDA kalır (metrik↔grafik tutarsızlığı, iz yok).
            _dropped += 1
    if _dropped:
        logging.getLogger(__name__).warning(
            "_extract_trades: %d pozisyon satırı parse edilemedi (trade listesinden düştü)",
            _dropped,
        )
    return trades


def _equity_curve(
    positions_df: pd.DataFrame | None,
) -> tuple[list[float], list[str]]:
    """Return (equity_values, iso_date_labels) aligned by trade close time."""
    if positions_df is None or positions_df.empty:
        return [STARTING_CASH], [""]
    pnls = _parse_money_column(positions_df["realized_pnl"])
    dates_ns = None
    if "ts_closed" in positions_df.columns:
        # M905: koşum sonunda AÇIK pozisyon kalırsa ts_closed pd.NA taşır ve
        # kolon object dtype olur → to_numpy(dtype='int64') TypeError fırlatıp
        # TÜM koşumu 'error'a çeviriyordu (_metrics'te try/except vardı, burada
        # yoktu). Güvenli dönüşüm: NA'ları koru, sırasız bırak.
        try:
            dates_ns = positions_df["ts_closed"].to_numpy(dtype="int64")
            order = np.argsort(dates_ns)
            pnls = pnls[order]
            dates_ns = dates_ns[order]
        except (TypeError, ValueError):
            dates_ns = None  # açık pozisyon / NA → tarihsiz eğri (metrikler sağ)
    curve = STARTING_CASH + np.cumsum(pnls)
    values = [STARTING_CASH] + [float(x) for x in curve]
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
        engine.add_venue(
            venue=active_venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money(STARTING_CASH, USDT)],
            base_currency=None,
            fee_model=MakerTakerFeeModel(),  # M5: gerçek Bybit komisyonları
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
            annualization=_periods_per_year(active_bar_type, instrument),
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
        f"Spec doğrulandı · {len(spec.blocks)} blok ({n_entry} entry, {n_exit} exit) · {spec.name}"
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
            active_instrument = _make_bybit_instrument()
            active_bar_type = _make_bybit_bar_type(active_instrument.id, "1")
            active_venue = active_instrument.id.venue
            active_instrument_id = active_instrument.id

        # Veri bilgisi
        n_bars = len(bars_df)
        try:
            date_start = bars_df.index[0].date()
            date_end = bars_df.index[-1].date()
            _p(f"Veri hazırlanıyor · {n_bars:,} bar · {date_start} → {date_end}")
        except Exception:
            _p(f"Veri hazırlanıyor · {n_bars:,} bar")

        config = BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(bypass_logging=_BYPASS_LOGGING),
        )
        engine = BacktestEngine(config=config)
        # Determine account type from instrument type, not from use_custom flag.
        # Equity (index proxy) → USD CASH; CurrencyPair (crypto) → USDT CASH/MARGIN.
        is_equity = use_custom and isinstance(active_instrument, Equity)
        # M1128: inverse (coin-margined) kripto USD-kotalıdır (BTCUSD); hesap
        # USDT ile açılırsa RiskEngine tüm girişleri SESSİZCE reddeder (0 trade).
        # Enstrümanın gerçek quote para birimini kullan.
        _quote_ccy = getattr(active_instrument, "quote_currency", None)
        _is_usd_quote = _quote_ccy is not None and str(_quote_ccy) == "USD"
        _crypto_ccy = USD if _is_usd_quote else USDT
        if is_equity:
            _account_type = AccountType.CASH
            _balances = [Money(STARTING_CASH, USD)]
            _base = USD
        elif getattr(spec, "allow_short", False):
            _account_type = AccountType.MARGIN
            _balances = [Money(STARTING_CASH, _crypto_ccy)]
            _base = None
        else:
            _account_type = AccountType.CASH
            _balances = [Money(STARTING_CASH, _crypto_ccy)]
            _base = None

        _p(
            f"Engine kuruluyor · {str(active_venue)} · {_account_type.name} · başlangıç: ${STARTING_CASH:,.0f}"
        )

        engine.add_venue(
            venue=active_venue,
            oms_type=OmsType.NETTING,
            account_type=_account_type,
            starting_balances=_balances,
            base_currency=_base,
            bar_adaptive_high_low_ordering=getattr(spec, "use_bracket", False),
            # M5: komisyon yalnız Bybit kripto çiftlerinde (instrument fee'leri
            # tanımlı); index/external Equity venue'larına fee model eklenmez.
            fee_model=MakerTakerFeeModel()
            if isinstance(active_instrument, CurrencyPair)
            else None,
        )
        engine.add_instrument(active_instrument)

        df = _prepare_df(bars_df)
        _p(
            f"Bar nesneleri oluşturuluyor · {len(df):,} bar (OHLC doğrulama: {n_bars - len(df)} satır atıldı)"
        )
        bars = _bars_from_df(active_bar_type, active_instrument, df)
        if not bars:
            raise RuntimeError("bar builder returned no bars")
        engine.add_data(bars)

        # Multi-timeframe trend filter: load and add secondary bars if requested
        secondary_bar_type_obj = None
        if getattr(spec, "trend_filter", False) and getattr(
            spec, "trend_interval", None
        ):
            try:
                _trend_interval = spec.trend_interval
                _p(f"Trend filter verisi yükleniyor · interval={_trend_interval}…")
                _trend_start = bars_df.index[0].to_pydatetime()
                _trend_end = bars_df.index[-1].to_pydatetime()
                if _trend_start.tzinfo is None:
                    _trend_start = _trend_start.replace(tzinfo=UTC)
                if _trend_end.tzinfo is None:
                    _trend_end = _trend_end.replace(tzinfo=UTC)
                # External-catalog Equity (venue != POLYGON — index proxy'si
                # değil): trend barları da aynı harici katalogdan gelir.
                # Katalogda trend dilimi yoksa ValueError → tek-TF fallback.
                _is_ext_equity = (
                    isinstance(active_instrument, Equity)
                    and active_instrument.id.venue != POLYGON
                )
                if _is_ext_equity:
                    _BYBIT_TO_DSL = {
                        "1": "1-MINUTE",
                        "5": "5-MINUTE",
                        "15": "15-MINUTE",
                        "60": "1-HOUR",
                        "240": "4-HOUR",
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

                # Trend TF ana TF ile aynıysa ikinci feed eklenmez: strateji
                # her barı "trend barı" sayar ve hiç işlem açmaz (0 trade).
                if _sec_bar_type.spec == active_bar_type.spec:
                    _p(
                        f"Trend TF ({_trend_interval}) ana TF ile aynı — "
                        "trend filtresi atlanıyor, tek TF ile devam"
                    )
                    trend_df = None
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
                    # M1234: kategori ana koşumun venue'sinden türetilir —
                    # sabit 'linear' spot koşumuna linear-perp fiyatı karıştırıyor
                    # ve inverse'de BTCUSD linear'da bulunmayıp filtreyi sessizce
                    # devre dışı bırakıyordu.
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
                if trend_df is not None and not trend_df.empty:
                    secondary_bar_type_obj = _sec_bar_type
                    trend_bars = _bars_from_df(
                        secondary_bar_type_obj, active_instrument, _prepare_df(trend_df)
                    )
                    if trend_bars:
                        engine.add_data(trend_bars)
                        _p(
                            f"Trend bars eklendi · {len(trend_bars):,} bar · {_trend_interval}"
                        )
            except Exception as _te:
                _p(
                    f"Trend filter verisi yüklenemedi: {_te} — tek TF ile devam ediliyor"
                )
                secondary_bar_type_obj = None

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
            f"Simülasyon başladı · {len(bars):,} bar işlenecek · order_type={spec.order_type}"
        )
        engine.run()
        _p("Simülasyon tamamlandı · sonuçlar toplanıyor…")

        positions_df = engine.trader.generate_positions_report()
        n_trades = (
            len(positions_df)
            if positions_df is not None and not positions_df.empty
            else 0
        )
        _p(f"Metrikler hesaplanıyor · {n_trades} pozisyon bulundu")

        # Collect MTM equity snapshots from strategy (Phase 1)
        mtm_equity = getattr(_composed_strategy, "_mtm_equity", None)
        metrics = _metrics(
            engine,
            positions_df,
            mtm_equity=mtm_equity,
            annualization=_periods_per_year(active_bar_type, active_instrument),
            mtm_ts=getattr(_composed_strategy, "_mtm_ts", None),
        )
        equity, equity_dates = _equity_curve(positions_df)
        # Giriş/çıkış sebepleri: karar günlüğü (_mtm_equity ile aynı yaşam
        # döngüsü) + fills raporu tag join'i
        try:
            reason_fills_df = engine.trader.generate_order_fills_report()
        except Exception:
            reason_fills_df = None
        trades = _extract_trades(
            positions_df,
            marker_shift_s=_bar_interval_ns(active_bar_type) // 1_000_000_000,
            fills_df=reason_fills_df,
            decisions=getattr(_composed_strategy, "_decision_log", None),
        )

        _p(
            f"Tamamlandı · PnL={metrics.get('pnl', 0):+.2f} USDT · win_rate={metrics.get('win_rate', 0) * 100:.1f}%"
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
        _p(f"Hata oluştu: {type(e).__name__}: {e}")
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


# ---------------------------------------------------------------------------
# High-level BacktestNode runner via ParquetDataCatalog
# ---------------------------------------------------------------------------
#
# Uses the catalog written by ``data.write_to_nautilus_catalog()`` and
# BacktestNode — the high-level counterpart of the low-level BacktestEngine
# path above. Strategies are registered via ImportableStrategyConfig so they
# work as if loaded from a YAML config file.
#
# Key differences vs run_backtest():
#   - Data comes from the Nautilus catalog, not a pandas DataFrame.
#   - Timestamps are int64 nanoseconds (correct Arrow schema).
#   - Stats include Sharpe (the v2rc1 BacktestEngine bug doesn't affect this path).
#   - Starting balance is in USDT, not USD; instrument must be a CurrencyPair
#     with USDT quote.
#
# See wiki: [[backtest_node]], [[parquet_data_catalog]], [[backtesting_guide]].


def run_backtest_node(
    strategy_name: str,
    params: dict,
    instrument_id: str,
    bar_type: str | None = None,  # None → katalogdan en kısa interval seç
    venue_name: str | None = None,  # None → instrument_id'nin venue'sinden türet
    starting_balance_usdt: float = STARTING_CASH,
    iteration_id: int = 0,
    rationale: str = "",
) -> IterationResult:
    """Run strategy via BacktestNode + ParquetDataCatalog.

    The catalog must already contain bars for ``instrument_id`` — call
    ``data.write_to_nautilus_catalog()`` first if not.  Returns the same
    IterationResult shape as ``run_backtest()`` for drop-in comparison.
    """
    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.config import (
        BacktestDataConfig,
        BacktestRunConfig,
        BacktestVenueConfig,
        ImportableStrategyConfig,
    )
    from nautilus_trader.model import InstrumentId

    from data import NAUTILUS_CATALOG_DIR

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

    node = None
    try:
        iid = InstrumentId.from_str(instrument_id)
        strat_cls, cfg_cls = STRATEGY_REGISTRY[strategy_name]

        strat_params = dict(params)
        strat_params.setdefault("trade_size", Decimal("0.01"))
        if not isinstance(strat_params.get("trade_size"), Decimal):
            strat_params["trade_size"] = Decimal(str(strat_params["trade_size"]))

        # Bar type: caller'dan gel, yoksa katalogdan en kısa interval'ı seç
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        catalog = ParquetDataCatalog(str(NAUTILUS_CATALOG_DIR))
        bar_type_ids = sorted(
            {str(b.bar_type) for b in catalog.bars(instrument_ids=[instrument_id])}
        )
        matching = [b for b in bar_type_ids if b.startswith(instrument_id)]
        if not matching:
            raise RuntimeError(
                f"No bars for {instrument_id} in catalog. "
                "Call data.write_to_nautilus_catalog() first."
            )
        if bar_type:
            bar_type_str = bar_type
        else:
            # Öncelik: 1m > 5m > 15m > 1h > 4h > 1d
            _pref = ["1-MINUTE", "5-MINUTE", "15-MINUTE", "1-HOUR", "4-HOUR", "1-DAY"]
            bar_type_str = next(
                (b for p in _pref for b in matching if p in b), matching[0]
            )

        strat_params["instrument_id"] = instrument_id
        strat_params["bar_type"] = bar_type_str

        cfg_cls(**strat_params)  # validate params (raises on bad values)
        # msgspec Struct configs have no __dict__; serialise the explicit params
        # we set (all JSON-safe: instrument_id/bar_type are strings here, Decimal→str).
        cfg_dict = {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in strat_params.items()
        }
        ic = ImportableStrategyConfig(
            strategy_path=f"strategies:{strat_cls.__name__}",
            config_path=f"strategies:{cfg_cls.__name__}",
            config=cfg_dict,
        )

        # M6: enstrümana göre para birimi — POLYGON equity/index USD-kotalı;
        # koşulsuz USDT bakiye, equity emirlerini karşılıksız bırakıyordu
        # (run_composed_backtest_node'daki dallanmanın aynısı).
        is_index = instrument_id.endswith(".POLYGON")
        _node_ccy = "USD" if is_index else "USDT"
        run_cfg = BacktestRunConfig(
            engine=BacktestEngineConfig(
                strategies=[ic],
                logging=LoggingConfig(bypass_logging=_BYPASS_LOGGING),
            ),
            venues=[
                BacktestVenueConfig(
                    name=venue_name or instrument_id.rsplit(".", 1)[-1],
                    # Node path serialises the run config to JSON; these fields
                    # must be enum *names* (str), not the pyo3 enum objects.
                    oms_type="NETTING",
                    account_type="CASH",
                    book_type="L1_MBP",
                    starting_balances=[f"{starting_balance_usdt:.2f} {_node_ccy}"],
                )
            ],
            data=[
                BacktestDataConfig(
                    catalog_path=str(NAUTILUS_CATALOG_DIR),
                    data_cls="nautilus_trader.model.data:Bar",
                    instrument_id=iid,
                    bar_types=[bar_type_str],
                )
            ],
        )

        node = BacktestNode(configs=[run_cfg])
        results = node.run()
        if not results:
            raise RuntimeError("BacktestNode returned no results.")
        r = results[0]

        # M6: önce venue para birimi (USD equity'de USDT anahtarı boş olurdu).
        pnls = r.stats_pnls.get(_node_ccy, r.stats_pnls.get("USDT", {}))
        returns = r.stats_returns
        general = r.summary

        # n_trades: total_positions veya general stats'tan al
        n_trades = getattr(r, "total_positions", None)
        if n_trades is None:
            for k, v in general.items():
                if "position" in k.lower() or "trade" in k.lower():
                    try:
                        n_trades = int(v)
                        break
                    except Exception:
                        pass
        n_trades = n_trades or 0

        import math as _math

        def _sf(d, key):
            v = d.get(key)
            if v is None:
                return float("nan")
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("nan")

        pnl = _sf(pnls, "PnL (total)")
        if _math.isnan(pnl):
            pnl = 0.0
        win_rate = _sf(pnls, "Win Rate")
        n_wins = (
            round(n_trades * win_rate) if n_trades and not _math.isnan(win_rate) else 0
        )
        n_losses = (n_trades - n_wins) if n_trades else 0
        pnl_pct_raw = _sf(pnls, "PnL% (total)")
        pnl_pct = (
            (pnl_pct_raw / 100.0)
            if not _math.isnan(pnl_pct_raw)
            else (pnl / starting_balance_usdt)
        )

        metrics: dict = {
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "win_rate": win_rate,
            "n_wins": n_wins,
            "n_losses": n_losses,
            "avg_win": _sf(pnls, "Avg Winner"),
            "avg_loss": _sf(pnls, "Avg Loser"),
            "max_winner": _sf(pnls, "Max Winner"),
            "max_loser": _sf(pnls, "Max Loser"),
            "sharpe": _sf(returns, "Sharpe Ratio (252 days)"),
            # Cross-runner comparable metric: Nautilus 252-day Sharpe on BOTH the
            # Engine and Node paths. Never compare the primary "sharpe" across
            # runners (Engine=manual 365-day, Node=Nautilus 252-day).
            "sharpe_nautilus": _sf(returns, "Sharpe Ratio (252 days)"),
            "annualization": 252,
            "sortino": _sf(returns, "Sortino Ratio (252 days)"),
            "profit_factor": _sf(returns, "Profit Factor"),
            "max_dd": float("nan"),  # BacktestNode stats'te Max Drawdown yok
            "volatility": _sf(returns, "Returns Volatility (252 days)"),
            "long_ratio": _sf(general, "Long Ratio"),
            "avg_duration_mins": float("nan"),
            "commission_total": 0.0,
            "slippage_total": 0.0,
            "n_trades": n_trades,
            "starting_cash": starting_balance_usdt,
            "runner": "BacktestNode",
            "instrument_id": instrument_id,
            "bar_type": bar_type_str,
        }

        return IterationResult(
            id=iteration_id,
            strategy=strategy_name,
            params=params,
            metrics=metrics,
            equity_curve=[starting_balance_usdt, starting_balance_usdt + pnl],
            equity_dates=["", ""],
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
        if node is not None:
            try:
                node.dispose()
            except Exception:
                pass


def run_composed_backtest_node(
    spec,
    instrument_id: str,
    bar_type: str,
    venue_name: str | None = None,  # None → instrument_id'nin venue'sinden türet
    starting_balance_usdt: float = STARTING_CASH,
    start_ns: int | None = None,  # nanosecond timestamp filtresi
    end_ns: int | None = None,
    iteration_id: int = 0,
    rationale: str = "",
    progress_fn=None,
) -> IterationResult:
    """BacktestNode + ParquetDataCatalog ile ComposedStrategy çalıştır.

    Nau'nun standart pipeline'ı: katalogdan veri oku, Rust engine'de çalıştır.
    BacktestEngine yoluna göre 5-10x daha hızlı.

    Kısıtlama: Per-trade detayı (giriş/çıkış zamanları) mevcut değil —
    BacktestNode bu veriyi dışa açmıyor. Sadece özet metrikler döner.
    """
    import json as _json

    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.config import (
        BacktestDataConfig,
        BacktestRunConfig,
        BacktestVenueConfig,
        ImportableStrategyConfig,
    )
    from nautilus_trader.model import InstrumentId

    from data import NAUTILUS_CATALOG_DIR

    def _p(msg: str) -> None:
        if progress_fn:
            try:
                progress_fn(msg)
            except Exception:
                pass

    node = None
    try:
        iid = InstrumentId.from_str(instrument_id)
        _p(f"BacktestNode başlatılıyor · {instrument_id} · {bar_type}")

        # ComposedStrategyConfig tüm değerleri str olarak taşır (JSON-safe)
        cfg_dict = {
            "instrument_id": instrument_id,
            "bar_type": bar_type,
            "spec_json": _json.dumps(spec.to_dict()),
            "trade_size": str(spec.trade_size),
        }
        ic = ImportableStrategyConfig(
            strategy_path="composer:ComposedStrategy",
            config_path="composer:ComposedStrategyConfig",
            config=cfg_dict,
        )

        # Instrument ID'den venue tipini çıkar: .POLYGON → USD, .BYBIT → USDT
        is_index = instrument_id.endswith(".POLYGON")
        # Node path serialises to JSON — use enum *names* (str), not enum objects.
        account_type = "CASH"
        if spec.allow_short and not is_index:
            account_type = "MARGIN"
        currency = "USD" if is_index else "USDT"
        starting_bal = f"{starting_balance_usdt:.2f} {currency}"

        run_cfg = BacktestRunConfig(
            engine=BacktestEngineConfig(
                strategies=[ic],
                logging=LoggingConfig(bypass_logging=_BYPASS_LOGGING),
            ),
            venues=[
                BacktestVenueConfig(
                    name=venue_name or instrument_id.rsplit(".", 1)[-1],
                    oms_type="NETTING",
                    account_type=account_type,
                    book_type="L1_MBP",
                    starting_balances=[starting_bal],
                )
            ],
            data=[
                BacktestDataConfig(
                    catalog_path=str(NAUTILUS_CATALOG_DIR),
                    data_cls="nautilus_trader.model.data:Bar",
                    instrument_id=iid,
                    bar_types=[bar_type],
                    start_time=start_ns,
                    end_time=end_ns,
                )
            ],
        )

        node = BacktestNode(configs=[run_cfg])
        _p("Simülasyon çalışıyor…")
        results = node.run()
        if not results:
            raise RuntimeError("BacktestNode returned no results.")
        r = results[0]
        _p("Simülasyon tamamlandı · metrikler toplanıyor")

        pnls = r.stats_pnls.get(
            currency, r.stats_pnls.get("USDT", r.stats_pnls.get("USD", {}))
        )
        returns = r.stats_returns
        general = r.summary

        n_trades = getattr(r, "total_positions", None)
        if n_trades is None:
            for k, v in general.items():
                if "position" in k.lower() or "trade" in k.lower():
                    try:
                        n_trades = int(v)
                        break
                    except Exception:
                        pass
        n_trades = n_trades or 0

        import math as _math

        def _sf(d, key):
            v = d.get(key)
            if v is None:
                return float("nan")
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("nan")

        pnl = _sf(pnls, "PnL (total)")
        if _math.isnan(pnl):
            pnl = 0.0
        win_rate = _sf(pnls, "Win Rate")
        n_wins = (
            round(n_trades * win_rate) if n_trades and not _math.isnan(win_rate) else 0
        )
        n_losses = (n_trades - n_wins) if n_trades else 0
        # PnL% (total) v2rc1'de zaten yüzde biriminde → fmt_pct tekrar ×100 yapmasın diye /100 ile fraksiyona çevir
        pnl_pct_raw = _sf(pnls, "PnL% (total)")
        pnl_pct = (
            (pnl_pct_raw / 100.0)
            if not _math.isnan(pnl_pct_raw)
            else (pnl / starting_balance_usdt)
        )
        metrics: dict = {
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "win_rate": win_rate,
            "n_wins": n_wins,
            "n_losses": n_losses,
            "avg_win": _sf(pnls, "Avg Winner"),
            "avg_loss": _sf(pnls, "Avg Loser"),
            "max_winner": _sf(pnls, "Max Winner"),
            "max_loser": _sf(pnls, "Max Loser"),
            "sharpe": _sf(returns, "Sharpe Ratio (252 days)"),
            # Cross-runner comparable metric: Nautilus 252-day Sharpe on BOTH the
            # Engine and Node paths. Never compare the primary "sharpe" across
            # runners (Engine=manual 365-day, Node=Nautilus 252-day).
            "sharpe_nautilus": _sf(returns, "Sharpe Ratio (252 days)"),
            "annualization": 252,
            "sortino": _sf(returns, "Sortino Ratio (252 days)"),
            "profit_factor": _sf(returns, "Profit Factor"),
            "max_dd": float("nan"),  # BacktestNode stats'te Max Drawdown yok
            "volatility": _sf(returns, "Returns Volatility (252 days)"),
            "long_ratio": _sf(general, "Long Ratio"),
            "avg_duration_mins": float("nan"),
            "commission_total": 0.0,
            "slippage_total": 0.0,
            "n_trades": n_trades,
            "starting_cash": starting_balance_usdt,
            "runner": "BacktestNode",
        }
        # L33 parity: kayıpsız koşuda Nautilus PF=inf döner — NAU sonlu cap'i (99.0).
        if _math.isinf(metrics["profit_factor"]):
            metrics["profit_factor"] = 99.0 if metrics["profit_factor"] > 0 else 0.0
        _p(f"Tamamlandı · PnL={pnl:+.2f} {currency} · trades={n_trades}")

        return IterationResult(
            id=iteration_id,
            strategy=f"composed:{spec.name}",
            params={"spec_id": spec.id, "n_blocks": len(spec.blocks)},
            metrics=metrics,
            equity_curve=[starting_balance_usdt, starting_balance_usdt + pnl],
            equity_dates=["", ""],
            trades=[],
            rationale=rationale,
            error=None,
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
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
        if node is not None:
            try:
                node.dispose()
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
