---
title: OrderEmulator
type: entity
summary: Venue'nun desteklemediği emir tiplerini yerelde taklit eder; emulation_trigger izler, tetiklenince venue'ya yalnız MARKET/LIMIT gönderir (v1.x).
status: draft
key_concepts:
  - orders
  - order_flow_pipeline
  - risk_engine
  - crash_only_design
sources:
  - sources/06_concepts_docs_v1230.md
  - sources/02_architecture_docs.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/orders/emulated.md
related:
  - wiki/concepts/order_flow_pipeline.md
  - wiki/entities/execution_engine.md
last_updated: 2026-07-13
---

# OrderEmulator

Venue'nun native desteklemediği [[orders|emir tiplerini]] yerelde taklit eden bileşen; gerçek yürütme için venue'ya yalnızca `MARKET` ve `LIMIT` emirleri gider (v1.x). Emulator, `emulation_trigger` parametresiyle belirtilen piyasa fiyatı türünü sürekli izler ve tetiklenme koşulu sağlanınca temel emri otomatik gönderir.

## Emulation Trigger

`emulation_trigger` şu `TriggerType` değerlerini alır: `NO_TRIGGER` (emülasyon tamamen kapalı — emir doğrudan venue'ya gider), `DEFAULT` (= `BID_ASK`, çoğu emülasyon için standart seçim), `BID_ASK` (en iyi bid/ask kotasyonları), `LAST_PRICE` (son işlem fiyatı), `DOUBLE_LAST` / `DOUBLE_BID_ASK` (iki ardışık güncellemeyle teyit), `LAST_OR_BID_ASK`, `MID_POINT`, `MARK_PRICE` (türev piyasaları), `INDEX_PRICE`.

## Hangi Tipler Emüle Edilir

| Emir tipi | Emüle edilebilir | Release edildiğinde |
| --- | --- | --- |
| `MARKET`, `MARKET_TO_LIMIT` | Hayır | — |
| `LIMIT`, `STOP_MARKET`, `MARKET_IF_TOUCHED`, `TRAILING_STOP_MARKET` | Evet | `MARKET` |
| `STOP_LIMIT`, `LIMIT_IF_TOUCHED`, `TRAILING_STOP_LIMIT` | Evet | `LIMIT` |

## Yaşam Döngüsü

1. Strategy emri `submit_order` ile gönderir.
2. [[risk_engine]] pre-trade kontrollerini yapar (deny mümkün).
3. OrderEmulator emri tutar ve emüle eder (`EMULATED` durumu).
4. Trigger gerçekleşince emir `OrderInitialized` event'iyle `MARKET`/`LIMIT`'e dönüştürülür, `emulation_trigger` alanı `NONE` yapılır ve release edilir (`RELEASED` durumu).
5. Release edilen emir ikinci kez [[risk_engine]] kontrolünden geçer; onaylanırsa [[execution_engine]]'e ve `ExecutionClient` üzerinden venue'ya iletilir.

Risk kontrolleri hem submission hem release aşamasında çalışır (v1.x). Not: [[order_flow_pipeline]] diyagramı Risk Engine'i emulator'dan sonra tek aşama olarak gösterir; upstream emulated dokümanına göre kontrol iki noktadadır.

## Tutulan Emirlerin İşlenmesi

- Orijinal `SubmitOrder` komutu cache'lenir; emir yerel `MatchingCore` bileşeninde işlenir.
- Emulator ihtiyaç duyduğu piyasa verisi aboneliklerini kendisi açar.
- Emir, release veya iptal edilene dek modify/update edilebilir.
- Client order ID tüm yaşam döngüsü boyunca aynen korunur; `order.is_emulated` anlık emülasyon durumunu verir (release sonrası `False`).
- Çalışan instance başına emüle edilebilecek emir sayısında bir sınır yoktur.

## Cache Sorguları

Yerel referans tutmak yerine [[cache]] üzerinden sorgulanması önerilir:

```python
emulated_orders = self.cache.orders_emulated()
is_emulated = self.cache.is_order_emulated(client_order_id)
count = self.cache.orders_emulated_count()
```

## Kalıcılık

Sistem aktif emüle emirler varken çöker ya da kapanırsa, emirler yapılandırılmış cache veritabanından OrderEmulator içine yeniden yüklenir — [[crash_only_design]] ile uyumlu davranış (v1.x).

## Bilinen boşluklar

- Contingency emir tiplerinin (OTO/OCO/OUO) emülasyon altındaki davranışı upstream emulated sayfasında belgelenmemiş.
- [[message_bus]] üzerindeki emulator topic/event isimleri ve konfigürasyon anahtarları.
- v2.x'e özgü davranış değişiklikleri henüz doğrulanmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[events]]
- [[execution_engine]]
- [[order_flow_pipeline]]
- [[orders]]
- [[synthetics]]
<!-- BACKLINKS:END -->
