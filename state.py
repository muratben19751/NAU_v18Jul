"""Bir backtest iterasyonunun sonuç kaydı.

Bu modül eskiden legacy Loop sayfasının oturum durumunu da tutuyordu
(`AppState`/`get_state`); Loop emekliye ayrılınca (kullanıcı kararı 2026-08-17)
o kısım kaldırıldı. `IterationResult` KALDI çünkü hiç legacy değil: her
backtest'in dönüş tipi (`backtest.py`, `sandbox.py` ve dört test dosyası onu
kullanıyor). Dosyanın adı bu yüzden tarihsel — taşımak, ondan fazlasını
değiştirmeyen bir commit'te gereksiz gürültü olurdu.

Kalıcılık yok; kayıt çağıranın elinde yaşar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IterationResult:
    id: int
    strategy: str
    params: dict
    metrics: dict
    equity_curve: list[float]
    rationale: str
    error: str | None
    timestamp: datetime
    equity_dates: list[str] = field(default_factory=list)
    trades: list[dict] = field(
        default_factory=list
    )  # [{entry_time, exit_time, entry_price, exit_price, side, pnl}]
    bars_info: dict = field(
        default_factory=dict
    )  # {symbol, category, interval, n_bars}
    # backtest_log.jsonl timestamp of this iteration, stamped after the log
    # write — the key the tear sheet overlay opens the run with. "" when the
    # log write failed or the iteration predates the field.
    log_ts: str = ""
