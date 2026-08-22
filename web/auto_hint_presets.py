"""AUTO brief'in GUIDANCE alanı için hazır prompt'lar (seçilir, düzenlenir).

Dört ardışık AUTO koşusundan (2026-08-21/22, QQQ/QQQC, `qwen2.5-coder:14b`)
kalibre edildi: alfa kapısı Calmar ≥ al-tut (QQQC 2003→ için 0,22) ve al-tut'tan
fazla getiri istiyor; mühürlü holdout son ~3,4 yılda ≥5 giriş bekliyor; en iyi
aday 604 işlemlik saatlik bir osilatördü; "hacim odaklı" hint'i dört koşuda da
al-tut'a ezildi; 14b model çağrıların ~%70'inde JSON/kod üretemedi.

Metinler `agent.py` tarafından "User hint (incorporate this into the strategy
concept)" satırıyla LLM'e AYNEN geçer — bu yüzden hedefi (kapıyı) değil
KAVRAMI söylerler; hedef zaten `_OBJECTIVE_BLOCK` ile gidiyor.

Yalnız Studio brief'inde (SAUTO) sunulur; `lab.html`/`agent_backtest.html`
hint alanları serbest metin olarak kalır.

Wiki References
---------------
Bkz: [[webapp_module_map]] (bu modülün satırı), [[aile_ici_ayirt_edicilik_2026_08_21]]
(prompt'ların kalibre edildiği dört AUTO koşusunun bağlamı). `templating.py` globali
`auto_hint_presets` ile şablona, `fragments/auto_hint_presets.html` seçicisiyle ekrana
akar; seçim `#mc-hint` textarea'sını DOLDURUR ama kilitlemez.
"""

from __future__ import annotations

MAX_TEXT_CHARS = 600

PRESETS: tuple[dict[str, str], ...] = (
    {
        "key": "builtin-sade",
        "label": "Sade: yalnız hazır bloklar, sık işlem (modeli düşürmez)",
        "text": (
            "Yalnız katalogdaki hazır (builtin) blokları kullan, özel kod yazma. "
            "En fazla 2 blok: bir giriş, bir çıkış. Parametreleri az ve yuvarlak "
            "tut. Saatlik barlarda sık işlem açan (yılda 50+), ortalamaya dönüş "
            "tipi bir strateji kur."
        ),
    },
    {
        "key": "rejim-200g",
        "label": "Rejim filtresi: 200 günlük ortalama, al-tut'u Calmar'da yen",
        "text": (
            "Amaç al-tut'tan daha düşük drawdown ile benzer getiri: fiyat 200 "
            "günlük ortalamanın üstündeyken neredeyse hep pozisyonda kal, altına "
            "inince çık ve yukarı kesişte geri gir. Tek bir rejim filtresi + geniş "
            "iz süren stop. Sık giriş-çıkış yapma."
        ),
    },
    {
        "key": "oynaklik-rejimi",
        "label": "Oynaklık rejimi anahtarı: düşük volde içeride, yüksekte dışarıda",
        "text": (
            "Gerçekleşen oynaklık düşükken tam pozisyonda ol; 20 günlük oynaklık "
            "kendi 1 yıllık medyanının üstüne çıkınca pozisyonu kapat. Hedef: "
            "büyük düşüş dönemlerini dışarıda geçirip yükselişlerin çoğunu "
            "yakalamak. Hacim kullanma."
        ),
    },
    {
        "key": "saatlik-asiri-satim",
        "label": "Saatlik aşırı-satım dönüşü: osilatör giriş, zamanlı çıkış",
        "text": (
            "1 saatlik barlarda osilatör tabanlı aşırı-satım girişi (Williams %R "
            "veya RSI < 25), çıkış zamana bağlı (20-40 bar) ya da ortalamaya geri "
            "dönüşte. Uzun vadeli trend yukarıdayken işlem aç. Yüzlerce işlem "
            "üretmeli."
        ),
    },
    {
        "key": "hacim-onay",
        "label": "Hacim yalnız onay: 50 günlük zirve kırılımı + ATR iz süren stop",
        "text": (
            "Hacim tek başına sinyal olmasın, yalnız onay olsun: fiyat 50 günlük "
            "zirveyi hacim ortalamasının 1,5 katıyla kırınca gir, 2×ATR iz süren "
            "stop ile çık. Hacim düşüşünü çıkış sinyali olarak kullanma."
        ),
    },
)


def validate_presets(presets: tuple[dict[str, str], ...] = PRESETS) -> None:
    """Şablona gitmeden önce: anahtar/etiket tekil, metin boş değil ve kısa.

    Uzun metin `<select>`'te değil textarea'da gösterilir, ama 600 karakterin
    üstü LLM'e "konsept" değil "spesifikasyon" olarak gider — o zaman hint,
    objective bloğuyla yarışmaya başlar.
    """
    keys = [p["key"] for p in presets]
    labels = [p["label"] for p in presets]
    if len(set(keys)) != len(keys):
        raise ValueError(f"tekrarlayan preset anahtarı: {keys}")
    if len(set(labels)) != len(labels):
        raise ValueError(f"tekrarlayan preset etiketi: {labels}")
    for p in presets:
        if not p["text"].strip():
            raise ValueError(f"boş preset metni: {p['key']}")
        if len(p["text"]) > MAX_TEXT_CHARS:
            raise ValueError(
                f"preset metni çok uzun ({len(p['text'])} > {MAX_TEXT_CHARS}): {p['key']}"
            )
        if not p["key"].replace("-", "").isalnum():
            raise ValueError(f"preset anahtarı yalnız harf/rakam/tire: {p['key']!r}")


validate_presets()
