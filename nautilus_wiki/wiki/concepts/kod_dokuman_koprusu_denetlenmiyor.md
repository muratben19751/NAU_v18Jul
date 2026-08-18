---
title: Kod-doküman köprüsünün kod yakası (artık denetleniyor)
type: concept
summary: Lint uzun süre yalnız wiki/ altındaki .md'leri taradı, modül başlıklarındaki Wiki References bağlarını görmedi — broken_links (0) derken kod yakasında 373 bağın 30'u kırıktı. 2026-08-18'de tarama lint'e eklendi ve çıkış koduna dahil edildi.
sources: []
last_updated: 2026-08-18
---

# Kod-doküman köprüsünün kod yakası denetlenmiyor

`nautilus_wiki` iki yönlü bir köprü kuruyor: sayfalar koda atıfta bulunuyor,
modüller de başlıklarındaki `Wiki References` bloğuyla sayfalara. Lint bu
köprünün **yalnız bir yakasını** tarıyor — `wiki/` altındaki `.md` dosyalarını.
Modül docstring'lerindeki `[[...]]` bağları hiç okunmuyor.

Sonuç, denetimin sessizce yeşil yanması: `broken_links (0)` cümlesinin öznesi
sistem değil, ARACIN kapsamı. Aynı hata bir kez daha ölçülmüştü (2026-08-16):
`auto/robustness.py` var olmayan bir sayfaya bağ veriyordu, lint yine
`broken_links (0)` diyordu; kod tarafı elle tarandığında 40 bağın 5'i kırıktı.

## Ölçüm (2026-08-18, wiki-sync turu)

Lint altı kategoride de sıfır verirken kod yakası elle tarandı:

| | |
|---|---|
| `Wiki References` taşıyan modül | 157 |
| toplam bağ | 373 |
| çözülmeyen | **45** → düzeltme sonrası 41 |
| bunların gerçek kırığı | **32** → düzeltme sonrası **28** |

45'in 13'ü yanlış pozitif ve ikisi ayrı sınıf:

* **Docstring örneği** (10): `wiki_helper.py`, `web/routes/wiki.py` ve
  testleri, wikilink sözdizimini ANLATIRKEN `[[slug]]`, `[[missing_page]]`,
  `[[nope_not_a_page]]`, `[[wikilinks]]` yazıyor. Bunlar bağ değil, örnek.
* **Sayfa değil dosya adı** (3): `[[test_auto_layer_is_web_free]]` gibi
  kullanımlar bir test modülünü gösteriyor. Wikilink sözdizimi burada yanlış
  araç — bağ çözülmüyor çünkü hedef zaten bir sayfa değil.

## Gerçek kırıkların TEK bir sebebi var: çapraz-vault sızıntısı

Kalan 32'nin tamamı, bu depoda OLMAYAN ama kullanıcının kişisel Obsidian
vault'unda BULUNAN sayfalara işaret ediyor:

| hedef | kaç yerde |
|---|---:|
| `deepr_skill` | 11 |
| `ticker_kimlik_degil_o_gunun_etiketi` | 4 |
| `nau_deepr_mimari_katman_ayrimi` | 4 |
| `surec_yoneticisi_ortami_dondurur` | 3 |
| `review_raporu_uretildigi_anda_bayatlar` | 3 |
| diğer 7 hedef | 1'er |

Mekanizma basit ve tekrarlanabilir: aynı ajan iki bilgi tabanına da yazıyor,
kişisel vault'ta gerçek olan bir sayfa adını proje modülüne kopyalıyor. İki
vault'un ad uzayı ayrı olduğu için bağ orada çözülüyor, burada çözülmüyor —
ve hiçbir araç uyarmıyor.

Bu tur açılan üç test dosyası (`test_review_360_fixes.py`,
`test_gate_rejection_margin.py`, `test_effective_symbols.py`) tam olarak bu
hatayı taşıyordu ve düzeltildi; kalan 28 başka oturumların dosyalarında duruyor
ve toplu düzenleme wiki-sync'in kapsamı dışında — ölçüm burada, karar
kullanıcının.

## Kapatıldı: tarama lint'in içinde (2026-08-18)

`wiki_tools lint` artık köprünün iki yakasını da görüyor —
`_code_bridge_links()` + yeni `code_broken_links` kategorisi. Üç tasarım kararı
ve gerekçeleri:

* **Docstring `ast` ile okunuyor, bayt kesimiyle değil.** İlk elle taramam
  dosyanın ilk 6 KB'ını alıyordu; keyfi bir kesim uzun başlıklarda bağ kaybeder,
  kısa dosyalarda da docstring dışındaki metni bağ sayar. `ast.get_docstring`
  köprü bloğunun yaşadığı yeri tam verir.
* **Gövde sayfa tarafıyla AYNI süzgeçten geçiyor** (`_bare_targets`), yani kod
  çiti ve satır-içi backtick elenmiş oluyor. Bunun bedava kazancı: elle taramada
  ayıklamak zorunda kaldığım 10 "docstring örneği" yanlış pozitifi kendiliğinden
  düştü — `wiki_helper.py` ve `web/routes/wiki.py` wikilink SÖZDİZİMİNİ
  anlatıyor, artık listede yoklar. Süzgeci paylaşmak, kuralı iki kez yazmamak
  demek.
* **Çıkış kodu kod yakasını da sayıyor** (`return 2`). Raporda gösterip yeşil
  yanmak, bu denetimin kapatmak için var olduğu deseni birebir yeniden
  üretirdi: sorunu YAZAN ama yine de "temiz" diyen araç.

Ölçüm aracın kendi ağzından: **30 kırık** (elle saydığım 28'in üstüne, dosya
adını sayfa sanan 2 kullanım daha — niyet ne olursa olsun bağ çözülmüyor, o
yüzden aracın sayısı doğru olan).

Karşılığında `lint` bugün **2 ile çıkıyor**. Bu bir gerileme değil, ölçümün
görünür hâle gelmesi: sayı zaten oradaydı, yalnız kimse bakmıyordu.

İlgili: [[webapp_module_map]] · [[nau_soz_verip_yapmayan_yollar_2026_08_17]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_holdout_dogrulama_turu_2026_08_18]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
