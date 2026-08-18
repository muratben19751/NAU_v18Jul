---
title: Token tüketim izleme — defter kalıcı ve tek kaynak
type: synthesis
summary: LLM tüketimi eskiden AUTO döngüsüne bağlı, tek modelli ve restart'ta kaybolan bellek-içi sayaçlarla izleniyordu; kalıcı JSONL defteri her çağrıyı API'nin GERÇEKTEN yanıtladığı modele yazar, `/tokens/badge` ile arayüz de aynı tek kaynaktan okur.
sources: []
related:
  - wiki/synthesis/llm_maliyet_kaldiraclari.md
  - wiki/synthesis/kesilme_ve_degrade_gorunurlugu.md
last_updated: 2026-08-18
---

# Token tüketim izleme — defter kalıcı ve tek kaynak

Uygulamadaki her LLM çağrısı `agent._create_message`'tan (ve oradan geçirilen
dört anlatı/özet yardımcısından) akıyor; her başarılı çağrı
`~/.cache/nautilus_web_app/token_usage.jsonl` dosyasına **tek bir satır**
ekliyor: zaman damgası, model, amaç ve dört token sayacı.

Bunun yerini aldığı şey, neden yetmediğini de anlatıyor: bellek-içi
`_AGENT_PROGRESS` sayaçları **AUTO döngüsüne bağlıydı** (döngü dışındaki
çağrılar hiç sayılmıyordu), **tek modelliydi** ve **restart'ta kayboluyordu**.
Yani "bugün ne harcadım" sorusunun cevabı sunucu her yeniden başladığında
sıfırlanıyordu.

## İki tasarım kararı ve gerekçeleri

**Yazılan model, İSTENEN değil YANITLAYAN modeldir.** Kredi tükenince
devreye giren Fable→Opus geri düşmesi, uygulamanın varsayılanına değil gerçekten
koşan modele yazılıyor. Aksi hâlde defter, maliyeti üreten modeli hiç
göstermeden "beklenen" modeli raporlardı.

**Defter yazımı bir LLM çağrısını ASLA bozamaz.** `record` best-effort:
tüm hatalar yutulur. Bir disk aksaklığının strateji üretimini düşürmesi,
ölçmeye çalıştığı şeyden pahalıya mal olurdu. Aynı gerekçe okuma tarafında da
var — bozuk/yarım satırlar (eşzamanlı append ile yırtılmış bir satır) atlanır,
ölümcül sayılmaz.

## Maliyet NOSYONELDİR

`/tokens/badge` kenar çubuğundaki ENGINE kartının altına tüketimi ve dolar
karşılığını yazar, ama bu bir fatura değil: çağrılar Claude CLI aboneliği
üzerinden gidiyor. Dolar, kullanım için bir ÖLÇEK — kalemler
`token_ledger._PRICES_PER_MTOK` liste fiyatlarından türetiliyor (Opus $5/$25,
Sonnet $3/$15, Haiku $1/$5 per MTok; cache okuması ×0,1).

"Oturum" = bu sunucu süreci başladığından beri, ve bu bile ayrı bir sayaçla
değil `web.shared.SERVER_STARTED_AT` ile kalıcı defteri süzerek hesaplanıyor —
**ikinci bir sayaç, ıraksayacak bir sayaçtır.**

## Ölçme maliyetinin kendisi ölçüldü

Rozet 60 saniyede bir tetikleniyor ve iki kez pahalıya patladı: 2026-08-08'de
tam dosya yeniden OKUMA, 2026-08-11'de tam liste yeniden KATLAMA. Bugün
`_folded` artımlı akümülatörü ikisini de kaldırdı (bkz.
[[nau_performans_denetimi]]).

Aynı defterin ikinci bir kullanımı maliyet denetimi:
[[llm_maliyet_kaldiraclari]] kalem kırılımını, model kıyasını ve çağrı sınıfı
başına cache oranını buradan çıkarıyor.

Kod tarafı: `token_ledger.py`, `web/routes/tokens.py`, `compact_sessions.py`
(aynı önbellek kökündeki oturum günlüklerinin geriye dönük indirgenmesi).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[llm_maliyet_kaldiraclari]]
<!-- BACKLINKS:END -->
