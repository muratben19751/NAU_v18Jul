"""Eski AUTO oturum günlüklerini yerinde sıkıştır (kayıt kaybı olmadan).

`_thin_curves` (web/routes/agent_backtest.py) eklenmeden ÖNCE yazılan
`robustness_result` olayları equity eğrilerini ham hâlde taşıyor: olay başına
~3,5 MB (88 WFO penceresi × train/test/naive eğrileri + 50 Monte Carlo örneği
+ 8.605 noktalık OOS eğrisi). Ölçüm 2026-08-10: 111 oturumun 10'u 11,1 GB,
kalan 101'i 1,0 GB. Yeni koşular zaten ince yazıyor (0,2–0,4 MB), yani bu
tarihsel bir birikim — kaynak sızıntı değil.

Bu yüzden çözüm SİLMEK değil: aynı indirgeme kuralı geriye dönük uygulanır.
İndirgeme bir görselleştirme kaybıdır, adli kayıt kaybı değil — metrikler,
kararlar, spec'ler, adım günlüğü olduğu gibi kalır; yalnız çizim için tutulan
uzun seriler seyreltilir.

Kullanım (repo kökü):
    python compact_sessions.py                # kuru çalıştırma — hiçbir şey yazmaz
    python compact_sessions.py --apply        # uygula
    python compact_sessions.py --apply --min-mb 50

Wiki References
---------------
Bkz: [[kesilme_ve_degrade_gorunurlugu]], [[nau_token_tuketim_izleme]]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Yalnız bu iki isim gerekiyor; router ağacını çekmek bir bakım script'i için
# kabul edilebilir (alternatifi indirgeme kuralını ikinci kez yazmaktı — tam
# olarak bu ikizleşme yüzünden `robustness_result` yolu ilk seferde atlanmıştı).
from web.routes.agent_backtest import SESSION_LOG_DIR, _thin_curves


def compact_file(path: Path, *, apply: bool) -> tuple[int, int]:
    """Bir günlüğü sıkıştır. (önce, sonra) bayt döner; kuru çalıştırmada yazmaz."""
    before = path.stat().st_size
    tmp = path.with_suffix(".jsonl.compact-tmp")
    after = 0
    with (
        path.open(encoding="utf-8", errors="replace") as src,
        tmp.open("w", encoding="utf-8", newline="\n") as dst,
    ):
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Bozuk satır AYNEN taşınır: bir denetim kaydını "okuyamadım"
                # diye düşürmek, sıkıştırmanın vaat etmediği bir kayıptır.
                out = line + "\n"
            else:
                out = json.dumps(_thin_curves(rec), ensure_ascii=False, default=str)
                out += "\n"
            dst.write(out)
            after += len(out.encode("utf-8"))
    if apply:
        os.replace(tmp, path)
    else:
        tmp.unlink(missing_ok=True)
    return before, after


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--apply", action="store_true", help="gerçekten yaz (varsayılan: kuru)"
    )
    ap.add_argument(
        "--min-mb",
        type=float,
        default=10.0,
        help="bu boyutun altındaki günlüklere dokunma (varsayılan 10 MB)",
    )
    ap.add_argument("--dir", default="", help="oturum günlüğü dizini (varsayılan: app)")
    args = ap.parse_args()

    root = Path(args.dir) if args.dir else SESSION_LOG_DIR
    floor = args.min_mb * 1e6
    targets = sorted(
        (p for p in root.glob("*.jsonl") if p.stat().st_size >= floor),
        key=lambda p: -p.stat().st_size,
    )
    if not targets:
        print(f"{root}: {args.min_mb} MB üstünde günlük yok.")
        return

    mode = "UYGULANIYOR" if args.apply else "KURU ÇALIŞTIRMA (hiçbir şey yazılmaz)"
    print(f"{mode} · {len(targets)} günlük · {root}")
    tot_b = tot_a = 0
    for p in targets:
        b, a = compact_file(p, apply=args.apply)
        tot_b += b
        tot_a += a
        pct = 100 * (1 - a / b) if b else 0.0
        print(f"  {p.name}  {b / 1e6:9.1f} MB → {a / 1e6:7.1f} MB  (−%{pct:.1f})")
    pct = 100 * (1 - tot_a / tot_b) if tot_b else 0.0
    print(f"TOPLAM {tot_b / 1e9:.2f} GB → {tot_a / 1e9:.2f} GB  (−%{pct:.1f})")
    if not args.apply:
        print("Uygulamak için: python compact_sessions.py --apply")


if __name__ == "__main__":
    main()
