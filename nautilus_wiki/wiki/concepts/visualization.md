---
title: Visualization
type: concept
summary: Plotly tabanlı genişletilebilir backtest görselleştirmesi — grafikleri ve istatistikleri tek etkileşimli HTML tearsheet'inde birleştirir (v1.230).
status: draft
key_concepts:
  - reports
  - backtesting_guide
  - backtest_node
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/visualization.md
last_updated: 2026-07-13
---

# Visualization

NautilusTrader, backtest sonuç analizini Plotly üzerine kurulu, genişletilebilir bir
görselleştirme sistemiyle sunar: birden çok grafik ve istatistiği tek bir etkileşimli HTML
**tearsheet**'inde birleştirir (v1.230.0). Çıktılar kendi başına yeterli (self-contained)
HTML dosyalarıdır — herhangi bir modern tarayıcıda açılır, paylaşılabilir, arşivlenebilir.

## Mimari

Sistem üç parçadan oluşur:

1. **Chart Registry** — grafik tanımları çekirdekten ayrık; özel grafiklerle genişletilebilir.
2. **Theme System** — yerleşik ve özel temalarla tutarlı stil.
3. **Configuration** — neyin, nasıl render edileceğinin bildirimsel (declarative) tanımı.

Kurulum `visualization` extra'sı ile yapılır; Pandas, Plotly ve statik görüntü export'u için
Kaleido kurulur: `uv pip install "nautilus_trader[visualization]"`.

## Tearsheet üretimi

En kısa yol, backtest bittikten sonra `create_tearsheet` çağrısıdır:

```python
from nautilus_trader.analysis import create_tearsheet

engine.run()
create_tearsheet(engine=engine, output_path="backtest_results.html")
```

`TearsheetConfig` ile grafik seçimi (`charts`), tema, başlık, yükseklik (varsayılan 1500 px)
ve `GridLayout` ile ızgara düzeni kontrol edilir. Çok para birimli backtest'lerde
`currency=USD` gibi bir filtre verilmelidir; return-tabanlı grafikler ancak hesaplar tek para
birimini paylaşıyorsa hesap raporlarından yeniden kurulabilir. `benchmark_returns` (datetime
index'li pandas Series) verilirse equity eğrisine karşılaştırma benchmark'ı bindirilir.

## Yerleşik grafikler

Dokuz yerleşik grafik vardır (v1.230.0): `run_info` ve `stats_table` tabloları, `equity`
(kümülatif getiri + opsiyonel benchmark), `drawdown`, `monthly_returns` heatmap'i,
`yearly_returns`, `distribution` histogramı, `rolling_sharpe` (60 günlük) ve
`bars_with_fills` (OHLC mumları üzerine emir fill marker'ları). Aylık/yıllık getiri
grafikleri varsayılan olarak bileşik (compounded) getiri hesaplar; `compounding=False`
sabit sermaye (constant-capital) konvansiyonuna geçer. Bu istatistikler, [[reports]]
tarafında anlatılan tablo-temelli analiz çıktılarının görsel karşılığıdır.

## Temalar

Dört yerleşik tema: `plotly_white` (varsayılan), `plotly_dark`, `nautilus`, `nautilus_dark`.
`register_theme()` ile renk paleti/font/arka plan tanımlanarak kurumsal özel tema kaydedilir;
tablo renkleri (`table_*`) verilmezse `background`/`grid` renklerinden türetilir.

## Özel grafikler ve offline analiz

- `register_chart()` kendi figürünü döndüren bağımsız grafik fonksiyonu kaydeder.
- `register_tearsheet_chart()` paylaşılan subplot ızgarasına (`fig`, `row`, `col`) çizen bir
  renderer kaydeder; sonra `TearsheetCustomChart(chart="...")` ile config'e eklenir.
- Elde `BacktestEngine` yoksa `create_tearsheet_from_stats()` önceden hesaplanmış
  `stats_pnls` / `stats_returns` / `stats_general` sözlükleri ve `returns` serisiyle çalışır —
  çoklu koşu karşılaştırması ve harici pipeline entegrasyonu için.

Düşük seviyeli akışta `create_tearsheet` doğrudan `BacktestEngine` alır; yüksek seviyeli
[[backtest_node]] akışında node'un çalıştırdığı engine'e erişilerek aynı üretim uygulanır.
Backtest kurulumunun bütünü için [[backtesting_guide]] sayfasına bakın.

## Bilinen boşluklar

- Tearsheet HTML'i tüm veriyi inline gömer; uzun backtest'lerde dosya birkaç MB'a ulaşır —
  upstream'in performans önerileri (grafik azaltma, tek grafik fonksiyonları) özetlenmedi.
- `show_logo` parametresi ileride logo render'ı için rezerve; henüz işlevsiz (v1.230.0).
- `create_bars_with_fills` standalone kullanımının ayrıntıları (marker semantiği) kısaltıldı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
- [[reports]]
<!-- BACKLINKS:END -->
