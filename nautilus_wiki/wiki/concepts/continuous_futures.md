---
title: Continuous Futures
type: concept
summary: Ardışık vadeli kontratları roll noktalarında tek ayarlanmış fiyat serisine dikişleyen türetilmiş seri; zincir tanımı ve composite bar hedefleri.
status: draft
key_concepts:
  - data_engine
  - instruments
  - bar_aggregation_and_type_syntax
  - precision_modes
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/continuous_futures.md
last_updated: 2026-07-13
---

# Continuous Futures

Sürekli vadeli (continuous future), ardışık vadeli kontratları tek bir ayarlanmış fiyat serisine dikişleyen türetilmiş bir seridir: her gerçek kontrat expire olur, seri ise geçiş (transition) noktasında bir sonraki kontrata roll edip tarihsel fiyatları yeni kontratın çerçevesine kaydırarak sıçramasız kalır. Nautilus bunu hedef bir `BarType` + çağıranın `params` içinde verdiği açık roll geçiş listesi olarak modeller; [[data_engine]] segmentleri sırayla yürür, segment başına kümülatif fiyat ayarını hesaplar ve ayarlanmış kaynak veriyi normal bar aggregation yolundan geçirir (v1.230).

## Adjustment modları

`ContinuousFutureAdjustmentType` yön (backward/forward) ile işlemi (spread/ratio) birleştirir:

| Mod | İşlem | Anchor segment |
|---|---|---|
| `BACKWARD_SPREAD` | Toplamsal | En yeni kontrat |
| `FORWARD_SPREAD` | Toplamsal | İlk kontrat |
| `BACKWARD_RATIO` | Çarpımsal | En yeni kontrat |
| `FORWARD_RATIO` | Çarpımsal | İlk kontrat |

Spread modları toplamsal offset biriktirir; ratio modları çarpımsal faktör biriktirir ve kesinlikle pozitif fiyat gerektirir.

## Girdi biçimi

Sürekli vadeli istek/aboneliği, `params` içinde `continuous_future_transitions` taşıyan herhangi bir `RequestBars` veya `SubscribeBars` komutudur:

```python
params = {
    "continuous_future_transitions": [
        {
            "transition_time_ns": 1773671460000000000,  # ESH26 -> ESM26 roll anı
            "pre_instrument_id": "ESH26.XCME",
            "post_instrument_id": "ESM26.XCME",
            "pre_price": "6001.00",
            "post_price": "5995.50",
        },
    ],
    "continuous_future_adjustment_mode": ContinuousFutureAdjustmentType.BACKWARD_SPREAD,
}
```

`bar_type` hedef sürekli bar tipidir (ör. `ES.XCME-1-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL`); kök kimlik (`ES.XCME`) gerçek bir kontrat değil sentetik bir root'tur. Hedef, [[bar_aggregation_and_type_syntax]] anlamında **INTERNAL** aggregate edilmiş olmalıdır — EXTERNAL barlar hedef olamaz ama segment kaynağı olabilir. Opsiyonel `last_post_instrument_id` / `first_pre_instrument_id` sınırları, geniş bir geçiş tablosunu belirli bir kontrata anchor'lamaya yarar.

## Segment ve roll mantığı

Segment, tek bir gerçek kontratın sahiplendiği bitişik zaman dilimidir; geçişler segmentleri ayırır. Request yolunda her iterasyon bir segmentin verisi için bir iç istek atar; yanıt gelince cursor ilerler. Subscription yolunda küçük bir state machine tek bir bekleyen time alert ile çalışır: transition ateşlendiğinde mevcut segmentin kaynağından unsubscribe edilir, sonraki segment aktive edilir (yeni kaynak + yeni offset) ve bir sonraki geçiş için timer yeniden kurulur.

## Doğrulama

Hem Rust request yolu hem Cython request/subscription yolları, aggregator ayrılmadan önce geçiş tablosunu doğrular: geçiş zamanları kesin artan olmalı; zincir sürekli olmalı (satır `i`'nin `post_instrument_id`'si satır `i+1`'in `pre_instrument_id`'sine eşit); her instrument id geçerli olup venue'su hedef venue ile eşleşmeli; `pre_price`/`post_price` sonlu (ratio modlarında ayrıca pozitif) olmalı. Hata durumunda istek state ayrılmadan reddedilir.

## Hedef instrument sentezi

Sentetik root'un kendi piyasa verisi yoktur ama aggregator ve cache tüketicileri cache'te bir `Instrument` bekler. Hedef id cache'te yoksa motor ilk segmentin [[instruments|instrument]]'ını klonlar; yalnızca `id` ve `raw_symbol` değiştirilir, `activation_ns` ve `expiration_ns` `0`'a sıfırlanır (sürekli seri "expire olmaz"), currency/precision/multiplier gibi diğer tüm alanlar korunur. Çağıran isterse özel bir sürekli instrument'ı önceden kaydedebilir; o zaman sentez no-op olur.

## Mid-bar roll sınırı

`BarBuilder` ayarlamayı **girişte**, her `update()` çağrısında uygular; yürüyen OHLC state'i her zaman ayarlanmış çerçevededir. Roll, oluşmakta olan bir hedef barın içine denk gelirse builder mevcut OHLC state'ini korur ve yeni ayarı yalnızca sonraki güncellemelere uygular — segment başına ham veri buffer'lamak yerine bilinçli tercih edilen politika budur. `reset()` bar state'ini temizler ama adjustment konfigürasyonunu korur (roll, bar reset'inden çok daha seyrek).

## Sınırlamalar

- Motor roll keşfi yapmaz: kontrat seçimi, geçiş zamanları ve roll fiyatları tamamen çağıranın sorumluluğudur (v1.x).
- Ratio ayarı hot path'te `float` üzerinden geçer; [[precision_modes]] anlamında yüksek hassasiyetli instrument'larda sonuç `Decimal` eşdeğerinden 1 ULP sapabilir. Spread modu doğrudan `PriceRaw` (int64/int128) üzerinde çalıştığı için kesindir.

## Bilinen boşluklar

- Kümülatif ayar formüllerinin segment-indeksli tam matematiği ve bounded-chain anchor kurallarının ayrıntısı burada özetlendi; tam tanım upstream'dedir.
- Çok seviyeli chain aggregator kablolaması (Rust request-scoped zincir vs Cython pipeline topic'leri) bu sayfada yalnızca ima edildi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[instruments]]
<!-- BACKLINKS:END -->
