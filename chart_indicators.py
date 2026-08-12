"""Chart indicator series — from the strategy's ACTUAL blocks, with ACTUAL parameters.

Instead of a decorative fixed EMA9/RSI(14), the chart now shows the indicators
used by the run spec with their own parameters. This way, when a trade is
clicked, "why did it enter here?" can be seen on the chart.

Overlays (on the same panel as price) and separate panel indicators (like RSI,
MACD histogram) are separated.

CAUTION — these series are VISUAL approximations, they do not affect the backtest:
- RSI: Wilder smoothing here; the strategy uses Nautilus RelativeStrengthIndex
  (EMA type, α=2/(period+1)) — shape/value differs somewhat
  (M chart_indicators). rsi_threshold also fires on a 0-100 scale (H6).
- Bollinger: only close here; Nautilus uses typical price (H+L+C)/3 —
  the band level can deviate by up to ~20bps.
- SCOPE: adx_threshold / stoch_rsi_cross / wave_trend_cross / donchian_channel /
  volume_spike and custom blocks are NOT YET represented on the chart (the
  signature only takes closes; ADX/WaveTrend/Donchian need highs/lows,
  volume_spike needs volumes).
These differences only affect the "why did it enter here?" visual; orders come
from the strategy's actual (Nautilus) indicator values.
"""

from __future__ import annotations

from typing import Any

# DeepR 2026-08-09 [ORTA] re-checked against current code: the finding claimed
# indicators.py's O(n·period)→O(n) SMA/EMA/RSI fix (2026-08-08) never reached
# these — false as of this check. _sma's running sum, _ema's incremental
# blend, and _rsi's Wilder recurrence below are each already O(n); none
# re-scans the window per step. The remaining duplication IS real but is
# shape-driven, not an oversight: these return a list the SAME LENGTH as
# `closes`, None-padded before the indicator is computable, so `_series()`
# can zip it 1:1 against the time axis for the chart. indicators.py's
# sma()/ema()/calc_rsi_series() return only the computed values (no
# leading padding) for backtest signal evaluation, which never needs
# positional alignment to a timestamp. Sharing one core would still need a
# per-caller padding/trimming wrapper — not attempted here without a
# concrete second consumer that would need the padded form.


def _sma(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if period <= 0:
        return out
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= period:
            s -= closes[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def _ema(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    # First EMA = SMA of the first 'period' bars
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        loss_d = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss_d) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def _bollinger(closes: list[float], period: int, k: float):
    """Bollinger bantları — KOŞAN kare toplamıyla O(n).

    DeepR 2026-08-11 [DÜŞÜK]: yukarıdaki 2026-08-09 notu `_sma`/`_ema`/`_rsi`'yi
    inceleyip O(n) olduklarını doğrulamış ama `_bollinger` o incelemenin dışında
    kalmıştı — her `i` için `closes[i-period+1:i+1]` dilimi alınıp pencere
    baştan taranıyordu, yani gerçekten O(n·period) idi. `_MAX_WINDOW_CANDLES`
    60.000 mum olduğu için bir bollinger_break spec'i grafikte açıldığında
    ölçülen maliyet: period=20 → 95,6 ms, period=200 → 691,5 ms. Koşan toplam +
    koşan kare toplamıyla ikisi de ~11 ms'e (period'dan BAĞIMSIZ) iner.

    Varyans `E[x²] − m²` ile hesaplanır. Bu, saf-fark formülüne göre büyük
    fiyatlarda (BTC ~60k) sondan birkaç basamak hassasiyet kaybettirir; iki
    nedenle kabul edilebilir: (1) bu seriler zaten GÖRSEL yaklaşımlar (dosya
    başlığındaki uyarı — Nautilus tipik fiyat kullanır, buradaki bant ~20bps
    sapabilir), (2) neredeyse sabit bir pencerede oluşabilecek küçük NEGATİF
    varyans `max(0.0, …)` ile kırpılır, yani `sqrt` asla domain hatası vermez.
    """
    mid = _sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    if period <= 0:
        return mid, upper, lower
    sq = 0.0
    for i, c in enumerate(closes):
        sq += c * c
        if i >= period:
            old = closes[i - period]
            sq -= old * old
        m = mid[i]
        if m is None or i < period - 1:
            continue
        var = max(0.0, sq / period - m * m)
        sd = var**0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return mid, upper, lower


def _series(times: list[int], vals: list[float | None]) -> list[dict]:
    # 8 decimals — for low-priced instruments (SHIB/PEPE ~1e-5..1e-8)
    # round(v, 4) flattened the line to 0.0.
    #
    # strict=True (DeepR 2026-08-09 [BİLGİ]/B905): every caller derives vals
    # from calc(closes, period), always len(closes)-shaped, and times/closes
    # come from the same DataFrame in web/routes/chart.py — same length by
    # construction. A mismatch would only mean a caller bug (misaligned
    # timestamps silently zipped to the wrong values); surface it instead
    # of truncating. chart.py already wraps this call in a broad
    # except-and-degrade, so a raise here cannot break the chart.
    return [
        {"time": t, "value": round(v, 8)}
        for t, v in zip(times, vals, strict=True)
        if v is not None
    ]


# type → chart indicator definition generator. Each one:
#   overlays: lines to draw on the price panel [{name, color, data}]
#   panes: separate panel indicators [{label, series:[{name,color,data}], refs:[{value,color}]}]
def indicators_for_spec(spec, times: list[int], closes: list[float]) -> dict[str, Any]:
    """Compute overlay + pane indicators based on the spec blocks."""
    overlays: list[dict] = []
    panes: list[dict] = []
    seen: set[str] = set()  # don't draw the same indicator twice (fingerprint)

    COLORS = [
        "#f59e0b",
        "#60a5fa",
        "#a78bfa",
        "#f97316",
        "#34d399",
        "#ec4899",
        "#eab308",
    ]
    ci = 0

    def _next_color():
        nonlocal ci
        c = COLORS[ci % len(COLORS)]
        ci += 1
        return c

    for b in spec.blocks:
        btype = b.type
        p = b.params or {}

        if btype in ("ma_cross", "ema_cross", "macd_cross"):
            # ma_cross default is 10/30; ema_cross & macd_cross same 12/26 as
            # composer meta (if params are empty, the chart shouldn't draw the wrong period)
            _dfast, _dslow = (10, 30) if btype == "ma_cross" else (12, 26)
            fast = int(p.get("fast", _dfast))
            slow = int(p.get("slow", _dslow))
            calc = _sma if btype == "ma_cross" else _ema
            for period, tag in [(fast, "fast"), (slow, "slow")]:
                fp = f"{'SMA' if btype == 'ma_cross' else 'EMA'}{period}"
                if fp in seen:
                    continue
                seen.add(fp)
                overlays.append(
                    {
                        "name": fp,
                        "color": _next_color(),
                        "data": _series(times, calc(closes, period)),
                    }
                )

        elif btype == "rsi_threshold":
            period = int(p.get("period", 14))
            thr = float(p.get("threshold", 30.0))
            fp = f"RSI{period}"
            if fp not in seen:
                seen.add(fp)
                panes.append(
                    {
                        "label": f"RSI({period})",
                        "series": [
                            {
                                "name": fp,
                                "color": "#a78bfa",
                                "data": _series(times, _rsi(closes, period)),
                            }
                        ],
                        "refs": [
                            {"value": thr, "color": "rgba(239,68,68,0.5)"},
                            {"value": 50, "color": "rgba(156,163,175,0.2)"},
                        ],
                    }
                )
            else:
                # Second rsi_threshold block with the same period (e.g. entry<30 +
                # exit>70): the RSI line already exists — just add the threshold reference.
                for _pane in panes:
                    if any(s["name"] == fp for s in _pane["series"]):
                        _pane["refs"].append(
                            {"value": thr, "color": "rgba(239,68,68,0.5)"}
                        )
                        break

        elif btype == "bollinger_break":
            period = int(p.get("period", 20))
            k = float(p.get("k", 2.0))
            fp = f"BB{period}_{k}"
            if fp not in seen:
                seen.add(fp)
                mid, up, lo = _bollinger(closes, period, k)
                c = _next_color()
                overlays.append(
                    {
                        "name": f"BB{period} mid",
                        "color": c,
                        "data": _series(times, mid),
                    }
                )
                overlays.append(
                    {
                        "name": "BB upper",
                        "color": "rgba(239,68,68,0.5)",
                        "data": _series(times, up),
                    }
                )
                overlays.append(
                    {
                        "name": "BB lower",
                        "color": "rgba(34,197,94,0.5)",
                        "data": _series(times, lo),
                    }
                )

        elif btype == "price_breakout":
            n = int(p.get("lookback", 20))
            fp = f"BRK{n}"
            if fp not in seen and n > 0:
                seen.add(fp)
                # Rolling high/low channel
                hi: list[float | None] = [None] * len(closes)
                lo: list[float | None] = [None] * len(closes)
                for i in range(len(closes)):
                    if i < n:
                        continue
                    w = closes[i - n : i]
                    hi[i] = max(w)
                    lo[i] = min(w)
                overlays.append(
                    {
                        "name": f"{n}-bar high",
                        "color": "rgba(239,68,68,0.5)",
                        "data": _series(times, hi),
                    }
                )
                overlays.append(
                    {
                        "name": f"{n}-bar low",
                        "color": "rgba(34,197,94,0.5)",
                        "data": _series(times, lo),
                    }
                )

        # atr_stop / momentum: an overlay line is not meaningful (trailing/lookback);
        # skipped for now — can be added if desired.

    return {"overlays": overlays, "panes": panes}
