"""AUTO koşusunun tek sayfalık postmortem'i — LLM YOK, tek atış, salt okunur.

AUTO zaten her şeyi yazıyor: `strategy_proposed`, `backtest_result`,
`robustness_result`, `winner`, `custom_block_generated`, `llm_usage`,
`timeline`, `degraded`, `session_end`. Bu script o olayları okuyup altı eksende
tablolar; hiçbir ajan çağırmaz, saniyeler sürer.

Neden LLM yok: elde yapılandırılmış sayı varken onu bir modele tarif ettirip
yorum istemek, tablonun kendisinden pahalıya mal olur (2026-08-14 ölçümü: bir
çoklu-ajan review turu 314 ajan / ~9,5M token). Bu script rakamı bulur; yargı
gerektiren yorum insana (ya da tek bir sohbet turuna) kalır.

Kullanım:
    python scripts/auto_postmortem.py                # en son koşu
    python scripts/auto_postmortem.py 65a87244       # belirli koşu
    python scripts/auto_postmortem.py --live         # süren koşu (o ana kadarki tablo)
    python scripts/auto_postmortem.py --calibrate    # tüm geçmişten eşik dağılımı

DÖNGÜ YOK: script tek atıştır, kendini tekrar çağırmaz, hiçbir şey beklemez.

Wiki References: [[auto_mission_control]], [[webapp_module_map]]
"""

from __future__ import annotations

import io
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_constants import DATA_DIR  # noqa: E402

SESSIONS = DATA_DIR / "agent_sessions"

# ── Eşikler ─────────────────────────────────────────────────────────────────
# Uydurulmadı: `--calibrate` ile 118 oturumluk geçmişten okunan dağılıma göre
# konuldu. Değiştirirken önce `--calibrate` koştur — eşik, gözlemin yerine
# geçmemeli.
TH = {
    # 118 oturumda medyan 0,54 / p75 0,60 / p90 0,64. İlk denemede 0,55
    # koymuştum: koşuların YARISINDA alarm çalardı, yani alarm anlamını
    # yitirirdi. p90'ın üstüne çekildi. Buradaki asıl haber eşik değil ölçümün
    # kendisi: fikir tekrarı bu sistemde ANOMALİ DEĞİL, NORM (bkz. rapordaki
    # "sistemik" satırı) — tek koşuya bakarak görülemeyecek bir bulgu.
    "idea_overlap": 0.65,
    # Kazanansız koşu da anomali değil: medyan kazanan/koşu = 0. Bu yüzden
    # "kazanan yok" tek başına alarm değil; ancak maliyet p75'i ($2,86) aşarsa
    # alarm — yani "para harcandı, çıktı yok".
    "no_winner_cost_usd": 2.86,
    "cost_per_winner_usd": 3.0,
    # KALİBRE EDİLMEDİ: geçmiş loglarda llm_usage olayı yalnız yeni koşularda
    # var (456 olay / 118 oturum), dolayısıyla dağılım çıkarılamadı. Bu eşik
    # şimdilik bir tahmindir; birkaç koşu biriktikten sonra --calibrate ile
    # yeniden bakılmalı.
    "cache_miss_ratio": 0.5,
    "min_trades": 30,  # bunun altındaki backtest istatistiksel gürültü
    "sharpe_suspicious": 2.5,  # robustluktan geçmemiş yüksek Sharpe
}

SEV = {"kritik": "🔴", "uyari": "⚠", "bilgi": "·"}


def _load(path: Path) -> list[dict]:
    """Oturum logunu oku. `step` olayları hacmin %90'ı ve bize gereksiz."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"event": "step"' in line or '"event":"step"' in line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _ts(rec: dict) -> float | None:
    raw = rec.get("ts")
    if not isinstance(raw, str) or "T" not in raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _words(text: str) -> set[str]:
    keep = "".join(c.lower() if c.isalnum() else " " for c in str(text or ""))
    return {w for w in keep.split() if len(w) > 3}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def _fmt_dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}dk {s}sn" if m else f"{s}sn"


def analyze(events: list[dict]) -> dict:
    by = defaultdict(list)
    for e in events:
        by[e.get("event")].append(e)

    start = (by["session_start"] or [{}])[-1]
    end = (by["session_end"] or [{}])[-1]
    proposals = by["strategy_proposed"]
    backtests = by["backtest_result"]
    robust = by["robustness_result"]
    winners = by["winner"]
    blocks = by["custom_block_generated"]
    usage = by["llm_usage"]

    stamps = [t for t in (_ts(e) for e in events) if t]
    wall = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0

    # ── Ekonomi ─────────────────────────────────────────────────────────────
    tok = Counter()
    cost = 0.0
    by_purpose = defaultdict(lambda: Counter())
    cache_miss = 0
    for u in usage:
        d = u.get("usage") or {}
        p = u.get("purpose") or "?"
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            tok[k] += int(d.get(k) or 0)
            by_purpose[p][k] += int(d.get(k) or 0)
        by_purpose[p]["calls"] += 1
        by_purpose[p]["cost"] += float(d.get("cost_usd") or 0.0)
        cost += float(d.get("cost_usd") or 0.0)
        if not int(d.get("cache_read_input_tokens") or 0):
            cache_miss += 1
    # token_snapshot daha güvenilir: worker'ın kendi sayacı (llm_usage bazı
    # çağrılarda purpose'suz gelebiliyor).
    snap = (by["token_snapshot"] or [{}])[-1]
    if snap.get("cost_usd"):
        cost = float(snap["cost_usd"])

    # ── Çeşitlilik ──────────────────────────────────────────────────────────
    per_round = defaultdict(list)
    for p in proposals:
        name = (p.get("spec") or {}).get("name") or ""
        per_round[p.get("round", 1)].append(name)
    overlaps = {}
    for rnd, names in per_round.items():
        pairs = [
            _overlap(_words(a), _words(b))
            for i, a in enumerate(names)
            for b in names[i + 1 :]
        ]
        if pairs:
            overlaps[rnd] = sum(pairs) / len(pairs)

    block_types = Counter()
    for b in backtests:
        for blk in b.get("spec_blocks") or []:
            block_types[blk.get("type", "?")] += 1

    # ── Zaman dağılımı ──────────────────────────────────────────────────────
    lane_time = Counter()
    open_spans = {}
    for t in by["timeline"]:
        key, lane, op = t.get("key"), t.get("lane"), t.get("op")
        stamp = _ts(t)
        if not (key and stamp):
            continue
        if op == "begin":
            open_spans[key] = (stamp, lane)
        elif op == "end" and key in open_spans:
            t0, ln = open_spans.pop(key)
            lane_time[ln or "?"] += stamp - t0

    return {
        "start": start,
        "end": end,
        "wall": wall,
        "proposals": proposals,
        "backtests": backtests,
        "robust": robust,
        "winners": winners,
        "blocks": blocks,
        "tok": tok,
        "cost": cost,
        "by_purpose": by_purpose,
        "cache_miss": cache_miss,
        "n_llm": len(usage),
        "overlaps": overlaps,
        "block_types": block_types,
        "lane_time": lane_time,
        "degraded": by["degraded"],
        "dedup": by["candidate_deduplicated"],
        "budget_rejects": by["llm_budget_rejected"],
        "holdout": by["holdout_result"],
    }


def report(a: dict, run_id: str) -> list[str]:
    L: list[str] = []
    add = L.append
    s, e = a["start"], a["end"]
    bt, rb, wn = a["backtests"], a["robust"], a["winners"]
    alarms: list[tuple[str, str]] = []

    add(f"╔═ AUTO POSTMORTEM · {run_id} " + "═" * 34)
    mode = "continuous" if s.get("continuous_mode") else "tek atış"
    # effort boş string gelebiliyor (form göndermemişse). "?" yazmak yerine
    # boşluğun kendisini söyle — ayarsız kalmış olması da bir bilgi.
    eff = s.get("effort") or "—(ayarsız)"
    add(
        f"║ {s.get('symbol', '?')} · {'/'.join(s.get('intervals') or ['?'])} · "
        f"{s.get('model', '?')} · effort={eff} · {mode} · "
        f"kaynak={s.get('source', '?')}"
    )
    if s.get("hint"):
        add(f"║ hint: {str(s['hint'])[:70]}")
    outcome = e.get("outcome", "—")
    add(
        f"║ süre {_fmt_dur(a['wall'])} · tur {e.get('completed_rounds', '?')}"
        f"/{s.get('n_iterations', '?')} iter · bitiş: {outcome}"
    )
    if e.get("reason"):
        add(f"║ sebep: {str(e['reason'])[:70]}")
    add("╚" + "═" * 60)

    # 1 ─ HUNİ
    add("\n▸ HUNİ (fikirden kazanana)")
    passed = sum(1 for r in rb if r.get("passed"))
    add(
        f"    öneri {len(a['proposals']):>3}  →  backtest {len(bt):>3}  →  "
        f"robustluk {len(rb):>3} ({passed} geçti)  →  kazanan {len(wn):>3}"
    )
    # Huninin NEREDE tıkandığı, kaç kazanan çıktığından daha çok şey söyler:
    # backtest var ama robustluk hiç koşmadıysa koşu kapıya varmadan ölmüştür
    # ve harcanan para hiçbir eleme sinyali üretmemiştir.
    if bt and not rb:
        alarms.append(
            (
                "kritik",
                f"{len(bt)} backtest koştu ama robustluk taraması HİÇ "
                f"başlamadı — koşu eleme kapısına varmadan bitti, "
                f"harcama bir kabul/ret sinyaline dönüşmedi",
            )
        )
    if bt:
        traded = sum(1 for b in bt if (b.get("n_trades") or 0) >= TH["min_trades"])
        add(
            f"    işlem sayısı ≥{TH['min_trades']}: {traded}/{len(bt)} "
            f"({_pct(traded, len(bt))}) — altındakiler istatistiksel gürültü"
        )
    if not wn and a["cost"] > TH["no_winner_cost_usd"]:
        # Kazanansız koşu tek başına anomali değil (medyan kazanan/koşu = 0);
        # alarm ancak para p75'i aşınca anlamlı.
        alarms.append(
            (
                "kritik",
                f"kazanan YOK ama ${a['cost']:.2f} harcandı "
                f"(geçmişte koşu maliyeti medyanı $1,30)",
            )
        )
    elif wn:
        cpw = a["cost"] / len(wn)
        add(f"    kazanan başına maliyet: ${cpw:.2f}")
        if cpw > TH["cost_per_winner_usd"]:
            alarms.append(
                (
                    "uyari",
                    f"kazanan başına ${cpw:.2f} > ${TH['cost_per_winner_usd']:.2f}",
                )
            )

    # 2 ─ ÇEŞİTLİLİK
    add(
        "\n▸ FİKİR ÇEŞİTLİLİĞİ  (geçmiş: medyan %54, p90 %64 — tekrar bu sistemde norm)"
    )
    for rnd in sorted(a["overlaps"]):
        ov = a["overlaps"][rnd]
        mark = " ← tekrar" if ov > TH["idea_overlap"] else ""
        add(f"    tur {rnd}: öneri adları arası örtüşme {ov:.0%}{mark}")
        if ov > TH["idea_overlap"]:
            alarms.append(
                (
                    "uyari",
                    f"tur {rnd}: fikir çöküşü (örtüşme {ov:.0%}) "
                    f"— model kendi geçmişini tekrar ediyor",
                )
            )
    if a["block_types"]:
        top = ", ".join(f"{k}×{v}" for k, v in a["block_types"].most_common(5))
        add(f"    farklı blok tipi: {len(a['block_types'])} ({top})")
    add(
        f"    custom block üretimi: {len(a['blocks'])}"
        + (f" · dedup ile elenen aday: {len(a['dedup'])}" if a["dedup"] else "")
    )

    # 3 ─ AŞIRI UYUM
    add("\n▸ AŞIRI UYUM SİNYALİ")
    if bt:
        best = sorted(bt, key=lambda b: -(b.get("score") or 0))[:3]
        for b in best:
            m = b.get("metrics") or {}
            add(
                f"    {str(b.get('spec_name', '?'))[:34]:<34} "
                f"sharpe {m.get('sharpe', 0):>6.2f}  "
                f"pnl {100 * (m.get('pnl_pct') or 0):>6.1f}%  "
                f"işlem {b.get('n_trades', 0):>4}  {b.get('interval', '')}"
            )
        risky = [
            b
            for b in bt
            if (b.get("metrics") or {}).get("sharpe", 0) > TH["sharpe_suspicious"]
            and (b.get("n_trades") or 0) < TH["min_trades"]
        ]
        if risky:
            alarms.append(
                (
                    "uyari",
                    f"{len(risky)} backtest yüksek Sharpe "
                    f"(>{TH['sharpe_suspicious']}) ama <"
                    f"{TH['min_trades']} işlem — eğri uydurma şüphesi",
                )
            )
    if rb:
        add(f"    robustluk: {passed}/{len(rb)} geçti ({_pct(passed, len(rb))})")
        for r in rb[:3]:
            add(
                f"      {str(r.get('spec_name', '?'))[:30]:<30} "
                f"{r.get('overfitting_label', '?')} · WF {r.get('wf_pass', '?')} "
                f"· {r.get('ms_label', '')}"
            )
    if a["holdout"]:
        h = a["holdout"][-1]
        add(
            f"    holdout: sharpe {h.get('sharpe', 0):.2f} · "
            f"{h.get('n_trades', 0)} işlem · {h.get('days', 0)} gün"
        )

    # 4 ─ EKONOMİ
    add("\n▸ EKONOMİ")
    t = a["tok"]
    raw = sum(t.values())
    add(
        f"    ${a['cost']:.2f} · {raw:,} ham token "
        f"(in {t['input_tokens']:,} / out {t['output_tokens']:,} / "
        f"cache-r {t['cache_read_input_tokens']:,} / "
        f"cache-w {t['cache_creation_input_tokens']:,})"
    )
    if a["n_llm"]:
        miss = a["cache_miss"] / a["n_llm"]
        add(
            f"    LLM çağrısı {a['n_llm']} · cache ıskası {miss:.0%} "
            f"({a['cache_miss']} çağrı sıfırdan yazdı)"
        )
        if miss > TH["cache_miss_ratio"]:
            alarms.append(
                (
                    "uyari",
                    f"cache ıskası {miss:.0%} — prompt öneki "
                    f"parçalanıyor, her ıska tam fiyat",
                )
            )
    for p, c in sorted(a["by_purpose"].items(), key=lambda kv: -kv[1]["cost"]):
        add(
            f"      {p:<16} {c['calls']:>3} çağrı  ${c['cost']:>6.2f}  "
            f"out {c['output_tokens']:>7,}"
        )
    if a["blocks"]:
        add(f"    custom block başına ≈ ${a['cost'] / len(a['blocks']):.2f}")

    # 5 ─ ZAMAN
    add("\n▸ ZAMAN NEREYE GİTTİ")
    tot = sum(a["lane_time"].values()) or 1
    for lane, sec in a["lane_time"].most_common():
        bar = "█" * int(30 * sec / tot)
        add(f"    {lane:<11} {_fmt_dur(sec):>9}  {bar} {100 * sec / tot:.0f}%")

    # 6 ─ SESSİZ DEGRADASYON
    add("\n▸ SESSİZ DEGRADASYON")
    fb = e.get("fallback_count", 0)
    add(
        f"    fallback {fb} · degraded {len(a['degraded'])} · "
        f"bütçe reddi {len(a['budget_rejects'])}"
    )
    for d in a["degraded"][:5]:
        add(f"      ↳ {d.get('what', '?')}: {d.get('reason', '?')}")
        alarms.append(("uyari", f"degraded — {d.get('what')}: {d.get('reason')}"))
    if fb:
        alarms.append(("uyari", f"{fb} fallback: birincil model/yol tutmadı"))
    if outcome == "error":
        alarms.append(("kritik", f"koşu HATAYLA bitti: {str(e.get('reason'))[:60]}"))
    if outcome == "budget" and (s.get("max_hours") or 0) > 0:
        used_h = a["wall"] / 3600
        if used_h < 0.5 * float(s["max_hours"]):
            alarms.append(
                (
                    "kritik",
                    f"süre bütçesinin yalnız {used_h / float(s['max_hours']):.0%}'i "
                    f"kullanılmışken token tavanı kesti — tavan yanlış kalibre",
                )
            )

    add("\n" + "─" * 62)
    if alarms:
        add(f"ALARMLAR ({len(alarms)})")
        for sev, msg in alarms:
            add(f"  {SEV.get(sev, '·')} {msg}")
    else:
        add("✓ Eşik aşan bir şey yok.")
    return L


def calibrate() -> list[str]:
    """Tüm geçmişten dağılım çıkar — eşikler bundan konur, sezgiden değil."""
    costs, winners_per, props, overlaps, outcomes = [], [], [], [], Counter()
    files = sorted(SESSIONS.glob("*.jsonl"))
    for f in files:
        try:
            a = analyze(_load(f))
        except Exception:
            continue
        if not a["start"]:
            continue
        outcomes[a["end"].get("outcome", "—")] += 1
        if a["cost"]:
            costs.append(a["cost"])
        winners_per.append(len(a["winners"]))
        props.append(len(a["proposals"]))
        overlaps.extend(a["overlaps"].values())

    def dist(name: str, xs: list[float], f: str = "{:.2f}") -> str:
        if not xs:
            return f"    {name:<22} veri yok"
        xs = sorted(xs)
        p = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
        return (
            f"    {name:<22} medyan {f.format(statistics.median(xs))}  "
            f"p75 {f.format(p(0.75))}  p90 {f.format(p(0.90))}  "
            f"max {f.format(xs[-1])}  (n={len(xs)})"
        )

    L = [f"▸ KALİBRASYON — {len(files)} oturum\n"]
    L.append(dist("koşu maliyeti ($)", costs))
    L.append(dist("kazanan/koşu", [float(w) for w in winners_per], "{:.0f}"))
    L.append(dist("öneri/koşu", [float(p) for p in props], "{:.0f}"))
    L.append(dist("tur içi fikir örtüşmesi", overlaps))
    L.append(
        "\n    bitiş dağılımı: "
        + ", ".join(f"{k}={v}" for k, v in outcomes.most_common())
    )
    L.append(
        "\n    Not: eşikler p75–p90 arasına konmalı — medyan koşu alarm "
        "üretirse alarm anlamını yitirir."
    )
    return L


def main(argv: list[str]) -> int:
    if not SESSIONS.exists():
        print(f"oturum dizini yok: {SESSIONS}")
        return 1
    if "--calibrate" in argv:
        print("\n".join(calibrate()))
        return 0

    picked = [a for a in argv if not a.startswith("--")]
    if picked:
        path = SESSIONS / f"{picked[0]}.jsonl"
        if not path.exists():
            print(f"koşu bulunamadı: {path}")
            return 1
    else:
        files = sorted(SESSIONS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            print("hiç oturum logu yok")
            return 1
        path = files[-1]

    a = analyze(_load(path))
    if not a["end"] and "--live" not in argv:
        print(
            f"⚠ {path.stem} henüz bitmemiş görünüyor — süren koşu için --live "
            f"kullan (tablo o ana kadarki veriyle çıkar).\n"
        )
    print("\n".join(report(a, path.stem)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
