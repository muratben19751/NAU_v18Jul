"""Postmortem, `sharpe: None` olan bir koşuda çökmesin.

`auto_review.render_markdown` üç yerde `.get("sharpe", 0)` yazıyordu. O varsayılan
yalnız anahtar YOKKEN devreye girer; anahtar var ve değeri None ise None döner —
`f"{None:.2f}"` de `None > eşik` de TypeError'dır.

`sharpe`'ın None olması bu uygulamada NORMAL: per-trade Sharpe standart sapma
ister, tek işlemli bir sonuçta sapma yoktur. Yani postmortem tam da ANORMAL
koşularda (tek işlem, dejenere sonuç) çöküyordu — raporun en çok gerektiği anda.

Canlı kanıt: koşu 1fa9870e'nin postmortem'i pm2 error log'unda
`TypeError: unsupported format string passed to NoneType.__format__` ile düştü.

Wiki References
---------------
See: [[auto_360_canli_review_iyilestirmeleri]].
"""

from __future__ import annotations

import auto_review


def _events(sharpe):
    """Tek backtest sonucu + tek mühürlü holdout — sharpe'ı çağıran belirler."""
    return [
        {"event": "session_start", "run_id": "r1", "ts": "2026-08-15T00:00:00+00:00"},
        {
            "event": "backtest_result",
            "run_id": "r1",
            "ts": "2026-08-15T00:01:00+00:00",
            "spec_name": "Tek İşlemli Aday",
            "interval": "1-DAY",
            "n_trades": 1,
            "score": 0.5,
            "metrics": {"sharpe": sharpe, "pnl_pct": 0.1, "n_trades": 1},
        },
        {
            "event": "session_end",
            "run_id": "r1",
            "ts": "2026-08-15T00:02:00+00:00",
            "outcome": "ok",
            "winner_holdout": {"sharpe": sharpe, "n_trades": 1, "measured": False},
        },
    ]


class TestNoneSharpe:
    def test_render_does_not_crash(self):
        md = auto_review.render_markdown(auto_review.analyze(_events(None), []), "r1")

        assert md
        assert "Tek İşlemli Aday" in md

    def test_none_is_reported_as_zero_not_invented(self):
        md = auto_review.render_markdown(auto_review.analyze(_events(None), []), "r1")

        # 0,00 yazmak "ölçülemedi"nin dürüst karşılığı; uydurma bir sayı YOK.
        assert "0.00" in md

    def test_a_real_sharpe_still_renders(self):
        md = auto_review.render_markdown(auto_review.analyze(_events(1.25), []), "r1")

        assert "1.25" in md
