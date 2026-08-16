"""One AUTO run, rendered as a reviewable document — deterministic, no LLM.

The session log already records twenty-odd event types, but reviewing a run
meant reading a 5 MB JSONL by hand or running a terminal script whose output
scrolls away. This module turns one run into a single markdown file covering
every dimension of it, so a review is reading rather than archaeology.

Two rules make the document trustworthy:

* **Absence is stated, never rendered as zero.** "robustluk taraması hiç
  koşmadı" and "0 aday geçti" describe completely different runs; a table that
  prints ``0`` for both destroys the distinction the reviewer needs most.
* **No judgement is invented.** Every line is a number read off the log or a
  threshold comparison whose threshold is documented (see ``TH``). Interpreting
  them is the reviewer's job — that is what makes this cheap enough to run on
  every single run (measurement 2026-08-14: one multi-agent review round cost
  314 agents / ~9.5M tokens; this costs zero).

Entry points::

    python -m auto_review                 # en son koşu
    python -m auto_review 65a87244        # belirli koşu
    python -m auto_review --stdout        # dosya yerine ekrana

``write_review()`` is also called automatically when a run logs ``session_end``
(see ``web/routes/agent_backtest.py``), so every finished run leaves its own
review file next to its log.

Wiki References
---------------
See: [[auto_mission_control]], [[webapp_module_map]]
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from app_constants import DATA_DIR

SESSIONS = DATA_DIR / "agent_sessions"

# ── Eşikler ─────────────────────────────────────────────────────────────────
# Uydurulmadı: `scripts/auto_postmortem.py --calibrate` ile 118 oturumluk
# geçmişten okunan dağılıma göre konuldu. Değiştirmeden önce yeniden kalibre et
# — eşik, gözlemin yerine geçmemeli.
TH = {
    # 118 oturumda medyan 0,54 / p75 0,60 / p90 0,64. Buradaki asıl haber eşik
    # değil ölçümün kendisi: fikir tekrarı bu sistemde ANOMALİ DEĞİL, NORM.
    "idea_overlap": 0.65,
    # Kazanansız koşu da anomali değil (medyan kazanan/koşu = 0); alarm ancak
    # para p75'i ($2,86) aşınca anlamlı: "harcandı, çıktı yok".
    "no_winner_cost_usd": 2.86,
    "cost_per_winner_usd": 3.0,
    # KALİBRE EDİLMEDİ: llm_usage yalnız yeni koşularda var, dağılım
    # çıkarılamadı. Bu bir tahmindir; koşu biriktikçe --calibrate ile bakılmalı.
    "cache_miss_ratio": 0.5,
    "min_trades": 30,  # altı istatistiksel gürültü
    "sharpe_suspicious": 2.5,  # robustluktan geçmemiş yüksek Sharpe
}

SEV_MARK = {"kritik": "🔴", "uyari": "⚠", "bilgi": "·"}

# `step` olayları hacmin ~%90'ı ve bir olay değil bir anlatı; sayılır, taşınmaz.
_STEP_MARKERS = ('"event": "step"', '"event":"step"')


# ── Yükleme ve küçük yardımcılar ────────────────────────────────────────────


def load_events(path: Path) -> tuple[list[dict], int]:
    """(step dışı olaylar, step sayısı). Bozuk satır atlanır, koşuyu düşürmez."""
    out: list[dict] = []
    steps = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if any(marker in line for marker in _STEP_MARKERS):
                steps += 1
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out, steps


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
    h, m = divmod(m, 60)
    if h:
        return f"{h}sa {m}dk"
    return f"{m}dk {s}sn" if m else f"{s}sn"


def _short(value, n: int = 60) -> str:
    text = str(value if value is not None else "—").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ── Çözümleme ───────────────────────────────────────────────────────────────


def analyze(events: list[dict], step_count: int = 0) -> dict:
    """Olay akışını, rapor bölümlerinin okuyacağı tek sözlüğe indir."""
    by = defaultdict(list)
    for e in events:
        by[e.get("event")].append(e)

    stamps = [t for t in (_ts(e) for e in events) if t]
    wall = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0

    # ── Ekonomi ─────────────────────────────────────────────────────────────
    usage = by["llm_usage"]
    tok = Counter()
    cost = 0.0
    by_purpose = defaultdict(Counter)
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
    for p in by["strategy_proposed"]:
        per_round[p.get("round", 1)].append((p.get("spec") or {}).get("name") or "")
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
    for b in by["backtest_result"]:
        for blk in b.get("spec_blocks") or []:
            block_types[blk.get("type", "?")] += 1

    # ── Zaman ───────────────────────────────────────────────────────────────
    lane_time = Counter()
    open_spans: dict[str, tuple[float, str]] = {}
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

    # ── LLM davranışı ───────────────────────────────────────────────────────
    llm_status = Counter(u.get("status") or "?" for u in usage)
    llm_errors = [u for u in usage if u.get("status") in ("error", "cancelled")]
    transcripts = [u for u in usage if u.get("transcript")]
    cap_hits = [u for u in usage if u.get("output_cap_exceeded")]
    durations = sorted(float(u.get("duration_s") or 0.0) for u in usage)

    # ── Veri ────────────────────────────────────────────────────────────────
    windows = {}
    for b in by["backtest_result"]:
        info = b.get("bars_info") or {}
        key = (
            str(info.get("symbol") or info.get("ticker") or "?"),
            str(b.get("interval") or info.get("interval") or "?"),
        )
        windows.setdefault(
            key,
            {
                "n_bars": info.get("n_bars"),
                "start": info.get("start"),
                "end": info.get("end"),
                "runs": 0,
            },
        )
        windows[key]["runs"] += 1

    # ── Canlı nabız ─────────────────────────────────────────────────────────
    beats = by["run_heartbeat"]
    peak_rss = max((int(b.get("rss_bytes") or 0) for b in beats), default=0)
    worst_stall = max(
        beats, key=lambda b: float(b.get("stalled_s") or 0.0), default=None
    )
    burn = None
    if len(beats) > 1:
        first, last = beats[0], beats[-1]
        d_t = float(last.get("elapsed_s") or 0) - float(first.get("elapsed_s") or 0)
        d_b = int(last.get("budget_used") or 0) - int(first.get("budget_used") or 0)
        if d_t > 0:
            burn = d_b / (d_t / 3600.0)  # bütçe-token / saat

    return {
        "start": (by["session_start"] or [{}])[-1],
        "end": (by["session_end"] or [{}])[-1],
        "env": (by["run_env"] or [{}])[-1],
        "config": (by["run_config"] or [{}])[-1],
        "beats": beats,
        "peak_rss": peak_rss,
        "worst_stall": worst_stall,
        "burn_per_hour": burn,
        "wall": wall,
        "steps": step_count,
        "n_events": len(events),
        "proposals": by["strategy_proposed"],
        "backtests": by["backtest_result"],
        "robust": by["robustness_result"],
        "winners": by["winner"],
        "blocks": by["custom_block_generated"],
        "discarded_blocks": by["custom_block_discarded"],
        "holdout": by["holdout_result"],
        "promotion_rejected": by["promotion_rejected"],
        "dedup": by["candidate_deduplicated"],
        "exposure": by["exposure_normalized"],
        "truncated_data": by["data_segment_truncated"],
        "degraded": by["degraded"],
        "budget_rejects": by["llm_budget_rejected"],
        "phases": by["phase_change"],
        "tok": tok,
        "cost": cost,
        "by_purpose": by_purpose,
        "cache_miss": cache_miss,
        "n_llm": len(usage),
        "llm_status": llm_status,
        "llm_errors": llm_errors,
        "transcripts": transcripts,
        "cap_hits": cap_hits,
        "llm_durations": durations,
        "overlaps": overlaps,
        "block_types": block_types,
        "lane_time": lane_time,
        "windows": windows,
    }


# ── Markdown ────────────────────────────────────────────────────────────────


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
        *["| " + " | ".join(str(c) for c in r) + " |" for r in rows],
    ]


# `.get("alan", 0)` varsayılanı yalnız anahtar YOKKEN devreye girer. Anahtar var
# ve değeri None ise None döner — ve `f"{None:.2f}"` ile `None > eşik` ikisi de
# TypeError'dır. `sharpe`'ın None olması bu uygulamada NORMAL: per-trade Sharpe
# standart sapma ister, tek işlemli bir sonuçta sapma yoktur (bkz. HOLDOUT_MIN_TRADES
# yorumu). Yani postmortem tam da ANORMAL koşularda çöküyordu — raporun en çok
# gerektiği anda. Doğru deyim `(... or 0)`; dosya zaten pnl_pct'de onu kullanıyordu.
def render_markdown(a: dict, run_id: str) -> str:
    """13 boyutlu review belgesi. Her bölüm ya veri ya da 'kayıt yok' der."""
    s, e = a["start"], a["end"]
    bt, rb, wn = a["backtests"], a["robust"], a["winners"]
    alarms: list[tuple[str, str]] = []
    L: list[str] = []
    add = L.append

    outcome = e.get("outcome") or ("—(bitiş kaydı yok)" if not e else "—")
    add(f"# AUTO review · `{run_id}`")
    add("")
    add(
        "> Deterministik, LLM'siz. Her satır oturum logundan okunmuş bir sayı ya "
        "da eşik karşılaştırmasıdır; yorum review edene aittir."
    )
    add("")

    # ── 1. KİMLİK ───────────────────────────────────────────────────────────
    add("## 1 · Kimlik")
    add("")
    if not s:
        add("`session_start` kaydı YOK — bu log bir AUTO koşusuna ait olmayabilir.")
    else:
        mode = "continuous" if s.get("continuous_mode") else "tek atış"
        add(
            "\n".join(
                _table(
                    ["alan", "değer"],
                    [
                        [
                            "enstrüman",
                            _short(s.get("symbol") or s.get("instrument_id")),
                        ],
                        ["aralık", "/".join(s.get("intervals") or ["?"])],
                        ["model", _short(s.get("model") or "—")],
                        ["effort", s.get("effort") or "—(ayarsız)"],
                        ["mod", mode],
                        ["kaynak", _short(s.get("source"))],
                        ["hint", _short(s.get("hint"), 90)],
                        ["tohum", _short(s.get("search_seed"))],
                        ["süre", _fmt_dur(a["wall"])],
                        [
                            "tur",
                            f"{e.get('completed_rounds', '?')} / "
                            f"{s.get('n_iterations', '?')} iterasyon",
                        ],
                        ["bitiş", f"**{outcome}**"],
                        ["sebep", _short(e.get("reason"), 90)],
                        [
                            "olay",
                            f"{a['n_events']} olay + {a['steps']} adım mesajı",
                        ],
                    ],
                )
            )
        )
    add("")

    # ── 2. PROVENANCE ───────────────────────────────────────────────────────
    add("## 2 · Bu koşuyu hangi kod üretti")
    add("")
    env = a["env"]
    if not env:
        add(
            "`run_env` kaydı YOK — bu koşu provenance kaydından önce yapılmış. "
            "Sayılar hangi commit'e ait, söylenemez."
        )
    else:
        git = env.get("git") or {}
        dirty = git.get("dirty")
        dirty_text = (
            "bilinmiyor"
            if dirty is None
            else (f"**EVET** ({git.get('dirty_files', 0)} dosya)" if dirty else "hayır")
        )
        rows = [
            ["commit", f"`{git.get('short', '—')}` ({git.get('branch', '—')})"],
            ["kirli ağaç", dirty_text],
            ["python", env.get("python", "—")],
            ["platform", _short(env.get("platform"), 40)],
            ["host / pid", f"{env.get('host', '—')} / {env.get('pid', '—')}"],
        ]
        for name, ver in (env.get("packages") or {}).items():
            rows.append([f"`{name}`", ver or "kurulu değil"])
        add("\n".join(_table(["alan", "değer"], rows)))
        overrides = env.get("env_overrides") or {}
        add("")
        if overrides:
            add("Yürürlükteki ortam ezmeleri (sır değerleri gizli):")
            add("")
            add(
                "\n".join(
                    _table(
                        ["değişken", "değer"],
                        [
                            [f"`{k}`", f"`{_short(v, 60)}`"]
                            for k, v in overrides.items()
                        ],
                    )
                )
            )
        else:
            add("Ortam ezmesi yok — her ayar belgelenmiş varsayılanında.")
        if dirty:
            alarms.append(
                (
                    "uyari",
                    f"koşu KİRLİ ağaçtan çıktı ({git.get('dirty_files', 0)} dosya) "
                    f"— `{git.get('short', '?')}` tek başına bu koşuyu üretmez",
                )
            )
    add("")
    cfg = {
        k: v
        for k, v in (a["config"] or {}).items()
        if k not in ("ts", "event", "run_id")
    }
    if cfg:
        add("Yürürlükteki karar sabitleri (hepsi env ile ezilebilir):")
        add("")
        add(
            "\n".join(
                _table(
                    ["sabit", "değer"],
                    [[f"`{k}`", f"`{_short(v, 70)}`"] for k, v in cfg.items()],
                )
            )
        )
        add("")
    else:
        add(
            "_Karar sabitleri kaydedilmemiş: bu koşuda hangi eşiklerin "
            "uygulandığı ancak BUGÜNKÜ kod okunarak tahmin edilebilir._"
        )
        add("")

    # ── 3. HUNİ ─────────────────────────────────────────────────────────────
    add("## 3 · Huni: fikirden kazanana")
    add("")
    passed = sum(1 for r in rb if r.get("passed"))
    add(
        "\n".join(
            _table(
                ["aşama", "adet", "not"],
                [
                    ["öneri", len(a["proposals"]), "LLM'in ürettiği aday"],
                    ["backtest", len(bt), "gerçekten koşan"],
                    ["robustluk", len(rb), f"{passed} geçti ({_pct(passed, len(rb))})"],
                    ["holdout", len(a["holdout"]), "mühürlü OOS"],
                    ["kazanan", len(wn), "promosyona giden"],
                ],
            )
        )
    )
    add("")
    if bt and not rb:
        alarms.append(
            (
                "kritik",
                f"{len(bt)} backtest koştu ama robustluk taraması HİÇ başlamadı "
                f"— koşu eleme kapısına varmadan bitti, harcama bir kabul/ret "
                f"sinyaline dönüşmedi",
            )
        )
    if bt:
        traded = sum(1 for b in bt if (b.get("n_trades") or 0) >= TH["min_trades"])
        add(
            f"İşlem sayısı ≥{TH['min_trades']} olan backtest: **{traded}/{len(bt)}** "
            f"({_pct(traded, len(bt))}) — altındakiler istatistiksel gürültü."
        )
        add("")
    if not wn and a["cost"] > TH["no_winner_cost_usd"]:
        alarms.append(
            (
                "kritik",
                f"kazanan YOK ama ${a['cost']:.2f} harcandı "
                f"(geçmişte koşu maliyeti medyanı $1,30)",
            )
        )
    elif wn:
        cpw = a["cost"] / len(wn)
        add(f"Kazanan başına maliyet: **${cpw:.2f}**")
        add("")
        if cpw > TH["cost_per_winner_usd"]:
            alarms.append(
                (
                    "uyari",
                    f"kazanan başına ${cpw:.2f} > ${TH['cost_per_winner_usd']:.2f}",
                )
            )

    # ── 4. KAPI KARARLARI ───────────────────────────────────────────────────
    add("## 4 · Kapı kararları (ne, neden elendi)")
    add("")
    decided = False
    if rb:
        decided = True
        add("**Robustluk**")
        add("")
        add(
            "\n".join(
                _table(
                    ["strateji", "geçti", "aşırı uyum", "WF", "çoklu sembol"],
                    [
                        [
                            _short(r.get("spec_name"), 30),
                            "✓" if r.get("passed") else "✗",
                            _short(r.get("overfitting_label"), 18),
                            str(r.get("wf_pass", "—")),
                            _short(r.get("ms_label"), 18),
                        ]
                        for r in rb[:12]
                    ],
                )
            )
        )
        add("")
    if a["promotion_rejected"]:
        decided = True
        add("**Promosyon reddi**")
        add("")
        add(
            "\n".join(
                _table(
                    ["tur", "spec", "sebep"],
                    [
                        [
                            str(p.get("round", "—")),
                            _short(p.get("spec_id"), 12),
                            _short(p.get("reason"), 70),
                        ]
                        for p in a["promotion_rejected"][:12]
                    ],
                )
            )
        )
        add("")
    if a["dedup"]:
        decided = True
        add(
            f"**Tekrar elemesi**: {len(a['dedup'])} aday parmak izi çakıştığı için "
            f"koşmadan elendi."
        )
        add("")
    if a["holdout"]:
        decided = True
        h = a["holdout"][-1]
        add(
            f"**Mühürlü holdout**: sharpe {h.get('sharpe') or 0:.2f} · "
            f"{h.get('n_trades', 0)} işlem · {h.get('days', 0)} gün."
        )
        add("")
    if not decided:
        add(
            "Bu koşuda hiçbir eleme kapısı karar üretmedi — ne robustluk, ne "
            "promosyon reddi, ne holdout. Harcanan kaynak bir kabul/ret "
            "sinyaline dönüşmemiş demektir."
        )
        add("")

    # ── 5. AŞIRI UYUM ───────────────────────────────────────────────────────
    add("## 5 · Aşırı uyum sinyali")
    add("")
    if not bt:
        add("Hiç backtest koşmadı — değerlendirilecek sonuç yok.")
    else:
        best = sorted(bt, key=lambda b: -(b.get("score") or 0))[:5]
        add(
            "\n".join(
                _table(
                    ["strateji", "skor", "sharpe", "getiri", "işlem", "aralık"],
                    [
                        [
                            _short(b.get("spec_name"), 30),
                            f"{b.get('score') or 0:.3f}",
                            f"{(b.get('metrics') or {}).get('sharpe') or 0:.2f}",
                            f"{100 * ((b.get('metrics') or {}).get('pnl_pct') or 0):.1f}%",
                            str(b.get("n_trades", 0)),
                            _short(b.get("interval"), 12),
                        ]
                        for b in best
                    ],
                )
            )
        )
        add("")
        risky = [
            b
            for b in bt
            if ((b.get("metrics") or {}).get("sharpe") or 0)
            > TH["sharpe_suspicious"]
            and (b.get("n_trades") or 0) < TH["min_trades"]
        ]
        if risky:
            alarms.append(
                (
                    "uyari",
                    f"{len(risky)} backtest yüksek Sharpe (>{TH['sharpe_suspicious']}) "
                    f"ama <{TH['min_trades']} işlem — eğri uydurma şüphesi",
                )
            )
    add("")

    # ── 6. FİKİR ÇEŞİTLİLİĞİ ────────────────────────────────────────────────
    add("## 6 · Fikir çeşitliliği")
    add("")
    add("_Geçmiş: medyan %54, p90 %64 — tekrar bu sistemde anomali değil, norm._")
    add("")
    if a["overlaps"]:
        add(
            "\n".join(
                _table(
                    ["tur", "öneri adları arası örtüşme"],
                    [
                        [
                            str(rnd),
                            f"{a['overlaps'][rnd]:.0%}"
                            + (
                                " ← tekrar"
                                if a["overlaps"][rnd] > TH["idea_overlap"]
                                else ""
                            ),
                        ]
                        for rnd in sorted(a["overlaps"])
                    ],
                )
            )
        )
        add("")
        for rnd, ov in sorted(a["overlaps"].items()):
            if ov > TH["idea_overlap"]:
                alarms.append(
                    (
                        "uyari",
                        f"tur {rnd}: fikir çöküşü (örtüşme {ov:.0%}) — model kendi "
                        f"geçmişini tekrar ediyor",
                    )
                )
    else:
        add("Örtüşme ölçülemedi: bir turda karşılaştırılacak iki öneri olmadı.")
        add("")
    if a["block_types"]:
        top = ", ".join(f"`{k}`×{v}" for k, v in a["block_types"].most_common(6))
        add(f"Farklı blok tipi: **{len(a['block_types'])}** — {top}")
        add("")
    add(
        f"Custom block üretimi: **{len(a['blocks'])}**"
        + (f" · atılan: {len(a['discarded_blocks'])}" if a["discarded_blocks"] else "")
        + (f" · dedup ile elenen aday: {len(a['dedup'])}" if a["dedup"] else "")
    )
    add("")

    # ── 7. EKONOMİ ──────────────────────────────────────────────────────────
    add("## 7 · Ekonomi")
    add("")
    t = a["tok"]
    add(
        f"**${a['cost']:.2f}** · {sum(t.values()):,} ham token "
        f"(in {t['input_tokens']:,} / out {t['output_tokens']:,} / "
        f"cache-r {t['cache_read_input_tokens']:,} / "
        f"cache-w {t['cache_creation_input_tokens']:,})"
    )
    add("")
    if a["n_llm"]:
        miss = a["cache_miss"] / a["n_llm"]
        add(
            f"LLM çağrısı **{a['n_llm']}** · cache ıskası **{miss:.0%}** "
            f"({a['cache_miss']} çağrı önbelleği sıfırdan yazdı)"
        )
        add("")
        if miss > TH["cache_miss_ratio"]:
            alarms.append(
                (
                    "uyari",
                    f"cache ıskası {miss:.0%} — prompt öneki parçalanıyor, "
                    f"her ıska tam fiyat",
                )
            )
        add(
            "\n".join(
                _table(
                    ["amaç", "çağrı", "maliyet", "çıktı token"],
                    [
                        [
                            f"`{p}`",
                            c["calls"],
                            f"${c['cost']:.2f}",
                            f"{c['output_tokens']:,}",
                        ]
                        for p, c in sorted(
                            a["by_purpose"].items(), key=lambda kv: -kv[1]["cost"]
                        )
                    ],
                )
            )
        )
        add("")
    else:
        add(
            "`llm_usage` kaydı yok — bu koşu ya hiç model çağırmadı ya da telemetri kapalıydı."
        )
        add("")

    # ── 8. LLM DAVRANIŞI ────────────────────────────────────────────────────
    add("## 8 · LLM davranışı")
    add("")
    if not a["n_llm"]:
        add("Çağrı kaydı yok.")
    else:
        d = a["llm_durations"]
        med = d[len(d) // 2] if d else 0.0
        add(
            "Durum dağılımı: "
            + ", ".join(f"`{k}`×{v}" for k, v in a["llm_status"].most_common())
            + f" · süre medyan {med:.1f}s, en uzun {d[-1] if d else 0:.1f}s"
        )
        add("")
        if a["cap_hits"]:
            add(
                f"Çıktı tavanına dayanan çağrı: **{len(a['cap_hits'])}** — kesilen "
                f"yanıt yeniden denenir, yani her biri iki kez faturalanmıştır."
            )
            add("")
        if a["llm_errors"]:
            add(
                "\n".join(
                    _table(
                        ["amaç", "durum", "hata"],
                        [
                            [
                                f"`{u.get('purpose', '?')}`",
                                u.get("status", "?"),
                                _short(u.get("error"), 70),
                            ]
                            for u in a["llm_errors"][:10]
                        ],
                    )
                )
            )
            add("")
        if a["transcripts"]:
            clipped = sum(
                1
                for u in a["transcripts"]
                if (u.get("transcript") or {}).get("clipped")
            )
            add(
                f"**Transkript**: {len(a['transcripts'])}/{a['n_llm']} çağrının "
                f"prompt+yanıt metni `{run_id}_artifacts/` altında saklandı"
                + (
                    f" ({clipped} tanesi karakter tavanında kırpıldı)."
                    if clipped
                    else "."
                )
            )
            add("")
            add(
                "Modelin neden bu fikirleri ürettiği YALNIZ burada görülebilir; "
                "sayaçlar sebebi değil sonucu ölçer."
            )
        else:
            add(
                "**Transkript yok** — bu koşuda prompt/yanıt metni kaydedilmemiş "
                "(`NAU_LLM_TRANSCRIPT=0` ya da koşu bu özellikten eski). Model "
                "davranışı hakkındaki her yorum, sayaçlardan yapılan bir tahmindir."
            )
        add("")

    # ── 9. VERİ BÜTÜNLÜĞÜ ───────────────────────────────────────────────────
    add("## 9 · Veri bütünlüğü")
    add("")
    if a["windows"]:
        add(
            "\n".join(
                _table(
                    ["sembol", "aralık", "bar", "başlangıç", "bitiş", "koşu"],
                    [
                        [
                            sym,
                            iv,
                            f"{w['n_bars']:,}" if w.get("n_bars") else "—",
                            _short(w.get("start"), 22),
                            _short(w.get("end"), 22),
                            w["runs"],
                        ]
                        for (sym, iv), w in sorted(a["windows"].items())
                    ],
                )
            )
        )
        add("")
    else:
        add("Backtest çalışmadığı için ölçülen bir veri penceresi yok.")
        add("")
    if a["truncated_data"]:
        for d in a["truncated_data"]:
            add(
                f"⚠ Katalog boşluğu: doğrulanmış sürekli kuyruk "
                f"`{d.get('effective_start', '?')}` tarihinden kullanıldı."
            )
        alarms.append(
            (
                "uyari",
                "veri penceresi kısaltıldı — sonuçlar istenen aralığın tamamı değil",
            )
        )
        add("")
    if a["exposure"]:
        add(
            f"Pozisyon büyüklüğü normalize edilen aday: **{len(a['exposure'])}** "
            f"(karşılaştırmayı aynı riske çekmek için)."
        )
        add("")

    # ── 10. ZAMAN ───────────────────────────────────────────────────────────
    add("## 10 · Zaman nereye gitti")
    add("")
    tot = sum(a["lane_time"].values())
    if tot:
        add(
            "\n".join(
                _table(
                    ["şerit", "süre", "pay"],
                    [
                        [lane, _fmt_dur(sec), f"{100 * sec / tot:.0f}%"]
                        for lane, sec in a["lane_time"].most_common()
                    ],
                )
            )
        )
    else:
        add("Kapanmış bir zaman çizelgesi aralığı yok (koşu erken bitmiş olabilir).")
    add("")

    # ── 11. CANLI NABIZ ─────────────────────────────────────────────────────
    add("## 11 · Canlı nabız")
    add("")
    beats = a["beats"]
    if not beats:
        add(
            "Nabız kaydı yok — bu koşu sabit aralıklı nabızdan eski. Olay "
            "üretilmeyen aralıklarda ne olduğu ÖLÇÜLEMEZ, tahmin edilir."
        )
    else:
        last = beats[-1]
        rows = [
            ["nabız", f"{len(beats)} kayıt"],
            ["son nabız", f"{_fmt_dur(float(last.get('elapsed_s') or 0))} içinde"],
            [
                "tepe bellek",
                f"{a['peak_rss'] / 1_048_576:.0f} MB"
                if a["peak_rss"]
                else "ölçülemedi",
            ],
        ]
        if a["burn_per_hour"]:
            rows.append(["bütçe yakımı", f"{a['burn_per_hour']:,.0f} token/saat"])
            cap = int(last.get("budget_cap") or 0)
            used = int(last.get("budget_used") or 0)
            if cap and a["burn_per_hour"] > 0 and used < cap:
                rows.append(
                    [
                        "tavana kalan",
                        _fmt_dur((cap - used) / a["burn_per_hour"] * 3600),
                    ]
                )
        worst = a["worst_stall"]
        if worst and float(worst.get("stalled_s") or 0) > 0:
            span = (worst.get("open_spans") or [{}])[0]
            rows.append(
                [
                    "en uzun bekleme",
                    f"{_fmt_dur(float(worst['stalled_s']))} · "
                    f"{_short(span.get('label') or span.get('lane'), 40)}",
                ]
            )
        add("\n".join(_table(["ölçü", "değer"], rows)))
        add("")
        if not e:
            # Bitiş kaydı yoksa son nabız, koşunun bilinen SON hâlidir — bir
            # kesintinin nerede yakaladığını en fazla bir aralık hatasıyla söyler.
            add(
                f"**Bitiş kaydı yok; bilinen son hâl:** faz `{_short(last.get('phase'), 50)}` · "
                f"son adım _{_short(last.get('last_step'), 70)}_ · "
                f"{len(last.get('open_spans') or [])} iş açık."
            )
            add("")
    add("")

    # ── 12. SESSİZ BOZULMA ──────────────────────────────────────────────────
    add("## 12 · Sessiz bozulma ve fallback")
    add("")
    fb = e.get("fallback_count", 0) or 0
    add(
        f"fallback **{fb}** · degraded **{len(a['degraded'])}** · "
        f"bütçe reddi **{len(a['budget_rejects'])}** · "
        f"atılan blok **{len(a['discarded_blocks'])}**"
    )
    add("")
    for d in a["degraded"][:8]:
        add(f"- `{d.get('what', '?')}` → {_short(d.get('reason'), 100)}")
        alarms.append(
            ("uyari", f"degraded — {d.get('what')}: {_short(d.get('reason'))}")
        )
    for r in (e.get("fallback_reasons") or [])[:8]:
        add(f"- fallback: {_short(r, 100)}")
    if fb:
        alarms.append(("uyari", f"{fb} fallback: birincil model/yol tutmadı"))
    if a["degraded"] or fb or a["budget_rejects"]:
        add("")
    # Yığın izi: aynı istisna adı bir turda altı kez düşebiliyor ve altısı ayrı
    # satırdan gelebiliyor. Ad semptomu, iz satırı söyler.
    traces = [d for d in (a["degraded"] + [e]) if d.get("traceback")]
    if traces:
        add("<details><summary>Yığın izleri</summary>")
        add("")
        for t in traces[:5]:
            add(f"`{_short(t.get('what') or t.get('outcome'), 40)}`")
            add("")
            add("```")
            add(str(t["traceback"])[-1500:].strip())
            add("```")
            add("")
        add("</details>")
        add("")

    # ── 13. YENİDEN ÜRETİLEBİLİRLİK ─────────────────────────────────────────
    add("## 13 · Yeniden üretilebilirlik")
    add("")
    blockers = []
    git = (a["env"] or {}).get("git") or {}
    if not a["env"]:
        blockers.append("provenance kaydı yok — hangi kod koştu bilinmiyor")
    elif git.get("dirty"):
        blockers.append(f"ağaç kirliydi ({git.get('dirty_files', 0)} dosya)")
    if not s.get("search_seed"):
        blockers.append("arama tohumu kaydedilmemiş")
    if not a["transcripts"] and a["n_llm"]:
        blockers.append("LLM transkripti yok — aynı prompt tekrar kurulamaz")
    if not a["config"]:
        blockers.append(
            "karar sabitleri kaydedilmemiş — hangi eşikler uygulandı belirsiz"
        )
    if a["truncated_data"]:
        blockers.append("veri penceresi koşu sırasında kısaltıldı")
    if blockers:
        add("Bu koşu **birebir** tekrar edilemez:")
        add("")
        for b in blockers:
            add(f"- {b}")
    else:
        add(
            "Kod sürümü, tohum, veri penceresi ve prompt metinleri kayıtlı — bu "
            "koşu tekrar kurulabilir. (Model çıktısı yine de deterministik değildir.)"
        )
    add("")

    # ── 14. ALARMLAR ────────────────────────────────────────────────────────
    add("## 14 · Alarmlar")
    add("")
    if alarms:
        order = {"kritik": 0, "uyari": 1, "bilgi": 2}
        # Aynı alarm altı kez tekrarlanırsa listedeki DİĞER alarmları bastırır.
        # Tekrar sayısı bilgidir, tekrarın kendisi değil.
        counted = Counter(alarms)
        for (sev, msg), n in sorted(
            counted.items(), key=lambda kv: (order.get(kv[0][0], 3), -kv[1])
        ):
            times = f" _(×{n})_" if n > 1 else ""
            add(f"- {SEV_MARK.get(sev, '·')} **{sev}** — {msg}{times}")
    else:
        add("Eşik aşan bir şey yok.")
    add("")
    add("---")
    add(
        f"_Ham kayıt: `{run_id}.jsonl` · artifact'lar: `{run_id}_artifacts/` · "
        f"eşikler: `auto_review.TH` (kalibrasyon: "
        f"`python scripts/auto_postmortem.py --calibrate`)._"
    )
    add("")
    return "\n".join(L)


# ── Dosyaya yazma ───────────────────────────────────────────────────────────


def review_path(run_id: str, sessions_dir: Path | None = None) -> Path:
    return (sessions_dir or SESSIONS) / f"{run_id}_review.md"


def build_review(run_id: str, sessions_dir: Path | None = None) -> str:
    """Markdown metni üret. Log yoksa `FileNotFoundError`."""
    path = (sessions_dir or SESSIONS) / f"{run_id}.jsonl"
    events, steps = load_events(path)
    return render_markdown(analyze(events, steps), run_id)


def write_review(
    run_id: str, out: Path | None = None, sessions_dir: Path | None = None
) -> Path:
    """Review'ı diske yaz ve yolunu döndür. Yazma atomik: yarım dosya kalmaz.

    ``sessions_dir`` çağrı anında geçilir çünkü sunucu tarafında oturum kökü
    testlerde yer değiştiriyor; import anında sabitlenmiş bir kök, testin
    yazdığı loga değil gerçek veri dizinine bakardı.
    """
    target = Path(out) if out else review_path(run_id, sessions_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(build_review(run_id, sessions_dir), encoding="utf-8")
    tmp.replace(target)
    return target


def _latest_run_id() -> str | None:
    files = sorted(SESSIONS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1].stem if files else None


def main(argv: list[str]) -> int:
    if not SESSIONS.exists():
        print(f"oturum dizini yok: {SESSIONS}")
        return 1
    picked = [x for x in argv if not x.startswith("--")]
    run_id = picked[0] if picked else _latest_run_id()
    if not run_id:
        print("hiç oturum logu yok")
        return 1
    if not (SESSIONS / f"{run_id}.jsonl").exists():
        print(f"koşu bulunamadı: {SESSIONS / f'{run_id}.jsonl'}")
        return 1
    if "--stdout" in argv:
        print(build_review(run_id))
        return 0
    out = None
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            print("--out bir yol bekliyor")
            return 1
        out = Path(argv[idx + 1])
    print(write_review(run_id, out))
    return 0


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
