---
title: Value Types
type: concept
summary: Price/Quantity/Money fixed-point değer tipleri — precision kuralları, aritmetik kısıtlar ve deterministik platformlar-arası hesap.
status: draft
key_concepts:
  - precision_modes
  - instruments
  - orders
  - parquet_data_catalog
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/value_types.md
last_updated: 2026-07-13
---

# Value Types

Nautilus, temel trading kavramları için özel değer tipleri sunar: `Price`, `Quantity`, `Money`. Üçü de içeride fixed-point aritmetik kullanır — platformlar arasında deterministik, float sürprizlerinden arınmış hesap. [[orders|Emirlerin]] fiyat ve miktar alanları, pozisyon büyüklükleri ve hesap bakiyeleri hep bu tiplerle taşınır.

| Tip | Amaç | İşaretli | Currency |
|---|---|---|---|
| `Quantity` | İşlem/emir miktarları, pozisyonlar | Hayır | - |
| `Price` | Piyasa fiyatları, quote seviyeleri | Evet | - |
| `Money` | Parasal tutarlar, P&L, bakiyeler | Evet | Evet |

## Immutability

Tüm değer tipleri **immutable**'dır; aritmetik işlemler orijinali değiştirmek yerine yeni instance üretir. Bu, thread güvenliği, öngörülebilirlik ve hashlenebilirlik (dict anahtarı / set üyesi olma) sağlar. Biriktirme deseni bu yüzden yeniden atamadır: `total = total + amount`.

## Fixed-point temsil ve precision

Değerler float değil, global bir sabit ölçeğe (high-precision modda 10^16) ölçeklenmiş tamsayı olarak saklanır; ham genişlik [[precision_modes]] derleme seçimine bağlıdır (64-bit / 128-bit). `precision` alanı kurulumda sabitlenir ve **değeri değil gösterimi** kontrol eder: string biçimlendirme ve serileştirmede kaç ondalık basamak görüneceğini belirler.

```python
from nautilus_trader.model.objects import Price

p1 = Price(1.23, precision=2)   # "1.23"
p2 = Price(1.230, precision=3)  # "1.230"
assert p1 == p2                 # eşitlik alttaki sayısal değere bakar

result = Price(100.5, precision=1) + Price(0.125, precision=3)
assert result.precision == 3    # sonuç operandların maksimum precision'ını alır
```

Bir instrument'ın hangi fiyat/miktar değerlerinin geçerli olduğunu ise [[instruments]] üzerindeki `price_precision` / `size_precision` alanları kısıtlar. Piyasa verisi Parquet/Arrow'a yazılırken precision dosya metadata'sında saklanır ve tek dosyadaki tüm değerler aynı precision'ı paylaşmalıdır; venue tick size değiştirirse (v1.x) öncesi/sonrası dosyalar tek [[parquet_data_catalog]] dosyasında birleştirilmemelidir.

## Aritmetik kuralları

- **Aynı tip, toplama/çıkarma → aynı tip**: `Price + Price → Price` (fiyat + fiyat hâlâ fiyattır).
- **Aynı tip, çarpma/bölme (`*`, `/`, `//`, `%`) → `Decimal`**: sonuç boyutsal olarak farklı bir anlam taşır (fiyat x fiyat "fiyat karesi", miktar/miktar boyutsuz oran); `Decimal` dönmek birim değişimini açık kılar.
- **Karışık tip → Python numeric tower**: `float` ile işlem `float`, `int` ve `Decimal` ile işlem hassasiyeti korumak için `Decimal` döner (her iki yönde de).
- **Farklı precision'lar**: sonuç, operandların maksimum precision'ını kullanır.
- **Unary operatörler** tipi korur; tek istisna `-Quantity → Decimal` (unsigned tip negatif değeri temsil edemez). `abs`/`+` tip korur, `round` `Decimal` döner.

## Tip kısıtları

- `Quantity` negatif olamaz: negatif kurulum da, sonucu negatife düşürecek çıkarma da `ValueError` fırlatır. Kısıtlar hem kurulumda hem işlem anında doğrulanır.
- `Money` currency taşır; toplama/çıkarma yalnızca aynı currency'ler arasında geçerlidir, uyuşmazlık `ValueError`'dur.

## Dönüşümler

`as_decimal()` precision'ı koruyarak `Decimal`'e, `as_double()` `float`'a çevirir; `str()` biçimlendirilmiş gösterim verir. Ters yönde `Quantity.from_str("100.5")`, `Price.from_str("99.95")`, `Money.from_str("1000.00 USD")` gibi sınıf metotlarıyla parse edilir.

## Bilinen boşluklar

- Raw değere doğrudan erişim API'si (`raw` alanı) ve high-precision/standard modlar arasında serileştirme uyumluluğu bu upstream sayfada yer almıyor; [[precision_modes]] tarafında da doğrulanmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[instruments]]
- [[orders]]
- [[precision_modes]]
<!-- BACKLINKS:END -->
