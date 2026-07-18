---
title: Synthetic Instruments
type: entity
summary: Bileşen instrument'lardan formülle türetilen yerel fiyatlama yapısı ({symbol}.SYNTH) — quote/trade üretimi, güncelleme akışı ve emir tetikleme kullanımı.
status: draft
key_concepts:
  - instruments
  - data_engine
  - order_emulator
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/synthetics.md
last_updated: 2026-07-13
---

# Synthetic Instruments

Sentetik enstrüman, tek ya da birden çok venue'daki bileşen [[instruments|instrument]]'lardan aritmetik bir formülle yerel olarak türetilen fiyatlama yapısıdır. Kimliği `{symbol}.SYNTH` biçimini alır (örn. `BTC-ETH:BINANCE.SYNTH`) ve platform dışında karşılığı yoktur: doğrudan trade edilemez, analitik araç olarak yaşar (v1.x).

## Kullanım alanları

- Actor/Strategy'lerin türetilmiş quote/trade feed'lerine subscribe olması.
- Türetilmiş fiyattan emulated order tetiklemek: emirde `emulation_trigger=TriggerType.DEFAULT` ve `trigger_instrument_id=<synthetic_id>` verildiğinde [[order_emulator]] emri sentetik fiyata göre serbest bırakır.
- Sentetik quote/trade'lerden standart aggregation mekanizmasıyla bar üretmek.

## Formül dili

Formüller bileşen `InstrumentId`'lerine doğrudan referans verir (`BTCUSDT.BINANCE`, `AUD/USD.SIM`, `ETH-USDT-SWAP.OKX`; eski `-` → `_` yazımı geriye dönük kabul edilir). Desteklenen yapılar:

- Sayısal ve boolean literal'ler, parantez, unary `-` / `!`.
- Operatörler: `+ - * / % ^`, karşılaştırma `< <= > >= == !=`, kısa devreli `&&` / `||` (öncelik sırası: `^` en yüksek, `&&`/`||` en düşük; `^` sağdan bağlar, `-2 ^ 2 == -(2 ^ 2)`).
- Yerel atama ve `;` ile sıralı değerlendirme: `spread = a - b; spread / 2`. Formül sayısal bir değerle bitmek zorundadır.
- Built-in fonksiyonlar: `abs`, `ceil`, `floor`, `round`, variadic `min`/`max` ve yalnızca seçilen dalı değerlendiren `if(cond, when_true, when_false)`.
- `//` satır ve `/* */` blok yorumları.

Derleme limitleri: en fazla 32 stack derinliği ve 16 yerel değişken — gerçekçi formüller için fazlasıyla geniş (8 bileşenli ağırlıklı toplam tepe 3 stack kullanır).

## Oluşturma ve güncelleme

Bileşen instrument'ların tanımdan önce [[cache]]'te bulunması ön koşuldur.

```python
synthetic = SyntheticInstrument(
    symbol=Symbol("BTC-ETH:BINANCE"),
    price_precision=8,
    components=[btcusdt_binance_id, ethusdt_binance_id],
    formula=f"{btcusdt_binance_id} - {ethusdt_binance_id}",
    ts_event=self.clock.timestamp_ns(),
    ts_init=self.clock.timestamp_ns(),
)
self.add_synthetic(synthetic)
self.subscribe_quote_ticks(synthetic.id)  # BTC-ETH:BINANCE.SYNTH
```

Formül çalışma zamanında değiştirilebilir: `self.cache.synthetic(id)` ile alınan nesnede `change_formula(...)` çağrılır, ardından `self.update_synthetic(synthetic)` ile sisteme geri yazılır.

## Güncelleme akışı

Herhangi bir bileşenin fiyatı güncellendiğinde formül yeniden değerlendirilir ve üretilen sentetik quote/trade [[data_engine]] üzerinden abonelere yayılır — bar aggregation dahil standart veri yolunu izler. Değerlendirme sıfır-allocation `f64` stack'iyle çalışır: tipik formüller 12–28 ns, tek seferlik derleme ~0.7–1.4 µs sürer (v1.230 benchmark'ları).

## Kısıtlar ve hata yönetimi

- Doğrudan trade edilemez; yalnızca yerel fiyatlama ve tetikleme aracıdır.
- Derleme aşaması bilinmeyen sembolleri, tip hatalarını ve kapasite aşımını reddeder; değerlendirme aşaması yanlış girdi sayısını ve non-finite (NaN/Infinity) fiyatları reddeder.
- Formül sonucu sayısal olmalıdır; boolean ya da atamayla biten formüller geçersizdir.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[instruments]]
<!-- BACKLINKS:END -->
