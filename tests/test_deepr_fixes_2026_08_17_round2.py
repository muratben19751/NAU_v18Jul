"""DeepR 2026-08-17 — ikinci tur (5–8. bulgular).

İlk turun dosyası ``test_deepr_fixes_2026_08_17.py``. Bu turda kalanlar:

1. Aşırı geniş tarih aralığı (``0001-01-01``–``9999-12-31``) hem RAM/CPU yiyor
   hem ``date.max + 1`` taşmasıyla 500 üretiyordu.

Wiki References
---------------
Bkz: [[strategy_studio]], [[review_raporu_uretildigi_anda_bayatlar]]
"""

from __future__ import annotations

from datetime import date

import pytest

# ---------------------------------------------------------------------------
# 1. Tarih aralığı genişliği
# ---------------------------------------------------------------------------


def test_absurd_date_range_is_refused_at_the_http_boundary():
    """Biçim ve sıra tek tek geçerli — kabul eden tam da buydu."""
    from web.shared import invalid_date_range

    err = invalid_date_range("0001-01-01", "9999-12-31")
    assert err and "too wide" in err


def test_realistic_ranges_still_pass():
    """Tavan hiçbir gerçek isteği kesmemeli: en uzun index geçmişi ~66 yıl."""
    from web.shared import invalid_date_range

    assert invalid_date_range("2024-01-01", "2024-12-31") is None
    assert invalid_date_range("1930-01-01", "2026-01-01") is None


def test_inverted_range_still_reports_inversion_not_width():
    """Ters aralığa önce 'ters' demek, 'çok geniş' demekten yardımcı."""
    from web.shared import invalid_date_range

    err = invalid_date_range("2025-01-01", "2024-01-01")
    assert err and "before the start" in err


def test_loader_refuses_the_range_itself():
    """Yükleyici HTTP sınırına GÜVENMEMELİ: script/test/yeni uç de çağırabilir.

    Eski kod burada `while d <= end: d += timedelta(days=1)` ile 3,6 milyon
    `date` nesnesi kurup `date.max`'ta `OverflowError` atıyordu.
    """
    from data import load_index_bars

    with pytest.raises(ValueError, match="day limit"):
        load_index_bars("SPX", date(1, 1, 1), date(9999, 12, 31))
