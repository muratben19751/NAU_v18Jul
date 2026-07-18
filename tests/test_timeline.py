"""Zaman çizelgesi (Gantt) katmanı — span helpers, render modeli, replay."""

from __future__ import annotations

from datetime import UTC, datetime

import web.routes.agent_backtest as ab
from web.routes.sessions import _build_timeline_spans
from web.viewmodels import associate_steps, timeline_view

T0 = 1_700_000_000.0


def _hms(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%H:%M:%S")


def _span(key, lane, t0, t1, *, status="ok", sub=False, rnd=1, **meta):
    return {
        "key": key,
        "lane": lane,
        "label": key,
        "t0": t0,
        "t1": t1,
        "status": status,
        "sub": sub,
        "round": rnd,
        "meta": dict(meta),
    }


# ---------------------------------------------------------------------------
# _tl_begin / _tl_end / _tl_close_open (canlı state üzerinde)
# ---------------------------------------------------------------------------


class TestTlHelpers:
    def _seed(self, run_id):
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = {"timeline": [], "steps": []}

    def _cleanup(self, run_id):
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)

    def test_begin_end_pairing(self):
        rid = "tltest01"
        self._seed(rid)
        try:
            ab._tl_begin(rid, "backtest", "bt-1", "BT 1", round_num=2, iter=1)
            tl = ab._AGENT_PROGRESS[rid]["timeline"]
            assert len(tl) == 1
            assert tl[0]["status"] == "running" and tl[0]["t1"] is None
            assert tl[0]["round"] == 2 and tl[0]["meta"]["iter"] == 1
            ab._tl_end(rid, "bt-1", status="ok", pnl=5.0)
            assert tl[0]["t1"] is not None and tl[0]["status"] == "ok"
            assert tl[0]["meta"]["pnl"] == 5.0
        finally:
            self._cleanup(rid)

    def test_orphan_end_is_noop(self):
        rid = "tltest02"
        self._seed(rid)
        try:
            ab._tl_end(rid, "yok-boyle-span", status="ok")  # raise etmemeli
            assert ab._AGENT_PROGRESS[rid]["timeline"] == []
        finally:
            self._cleanup(rid)

    def test_close_open_closes_all_running(self):
        rid = "tltest03"
        self._seed(rid)
        try:
            ab._tl_begin(rid, "data", "d1", "D1")
            ab._tl_begin(rid, "llm", "l1", "L1")
            ab._tl_end(rid, "d1", status="ok")
            ab._tl_close_open(rid, status="warn")
            tl = ab._AGENT_PROGRESS[rid]["timeline"]
            assert all(sp["t1"] is not None for sp in tl)
            assert tl[0]["status"] == "ok"  # kapalı olan dokunulmadı
            assert tl[1]["status"] == "warn"
        finally:
            self._cleanup(rid)

    def test_cap_evicts_closed_first(self):
        rid = "tltest04"
        self._seed(rid)
        try:
            ab._tl_begin(rid, "data", "acik-kalan", "R")  # önce açılan, açık kalacak
            for i in range(ab._TL_MAX_SPANS + 5):
                ab._tl_begin(rid, "backtest", f"c{i}", f"C{i}")
                ab._tl_end(rid, f"c{i}", status="ok")
            tl = ab._AGENT_PROGRESS[rid]["timeline"]
            assert len(tl) <= ab._TL_MAX_SPANS
            assert any(sp["key"] == "acik-kalan" for sp in tl), (
                "açık span cap eviction'da korunmalı"
            )
        finally:
            self._cleanup(rid)


# ---------------------------------------------------------------------------
# timeline_view
# ---------------------------------------------------------------------------


class TestTimelineView:
    def test_percentage_math(self):
        spans = [
            _span("a", "data", T0, T0 + 50),
            _span("b", "backtest", T0 + 50, T0 + 100),
        ]
        tl = timeline_view(spans, now=None)
        bars = {
            b["key"]: b for lane in tl["lanes"] for row in lane["rows"] for b in row
        }
        assert bars["a"]["x_pct"] == 0.0
        assert abs(bars["a"]["w_pct"] - 50.0) < 0.5
        assert abs(bars["b"]["x_pct"] - 50.0) < 0.5
        assert tl["now_pct"] is None  # done → imleç yok

    def test_min_width_clamp(self):
        spans = [
            _span("uzun", "data", T0, T0 + 1000),
            _span("an", "backtest", T0 + 500, T0 + 500.01),  # ~anlık
        ]
        tl = timeline_view(spans)
        bar = next(
            b
            for lane in tl["lanes"]
            for row in lane["rows"]
            for b in row
            if b["key"] == "an"
        )
        assert bar["w_pct"] >= 0.35, "anlık işlemler tıklanabilir min-genişlik almalı"

    def test_running_span_extends_to_now(self):
        spans = [_span("r", "llm", T0, None, status="running")]
        tl = timeline_view(spans, now=T0 + 60)
        bar = tl["lanes"][0]["rows"][0][0]
        assert bar["status"] == "running"
        assert bar["w_pct"] > 90  # tek span, now'a kadar uzar
        assert tl["now_pct"] is not None

    def test_nice_ticks_short_and_long(self):
        short = timeline_view([_span("a", "data", T0, T0 + 45)])
        long = timeline_view([_span("a", "data", T0, T0 + 95 * 60)])
        assert 2 <= len(short["ticks"]) <= 8
        assert 2 <= len(long["ticks"]) <= 8

    def test_round_filtering_and_empty(self):
        spans = [
            _span("r1", "data", T0, T0 + 10, rnd=1),
            _span("r2", "data", T0 + 100, T0 + 110, rnd=2),
        ]
        tl1 = timeline_view(spans, round_num=1)
        keys = [b["key"] for lane in tl1["lanes"] for row in lane["rows"] for b in row]
        assert keys == ["r1"]
        assert timeline_view(spans, round_num=99) is None
        assert timeline_view([]) is None

    def test_robustness_sub_row(self):
        spans = [
            _span("rob", "robustness", T0, T0 + 100),
            _span("rob-ms", "robustness", T0 + 5, T0 + 40, sub=True),
        ]
        tl = timeline_view(spans)
        lane = tl["lanes"][0]
        assert len(lane["rows"]) == 2


# ---------------------------------------------------------------------------
# associate_steps
# ---------------------------------------------------------------------------


class TestAssociateSteps:
    def test_window_mapping_and_innermost(self):
        spans = [
            _span("outer", "robustness", T0, T0 + 100),
            _span("inner", "robustness", T0 + 10, T0 + 50, sub=True),
        ]
        steps = [
            {"ts": _hms(T0 + 20), "msg": "iç"},
            {"ts": _hms(T0 + 80), "msg": "dış"},
        ]
        out = associate_steps(spans, steps)
        assert [s["msg"] for s in out.get("inner", [])] == ["iç"]
        assert [s["msg"] for s in out.get("outer", [])] == ["dış"]

    def test_midnight_wrap(self):
        # Span 23:59'da başlar, adım 00:01'de (ertesi gün) düşer.
        base = datetime(2026, 7, 13, 23, 59, 0, tzinfo=UTC).timestamp()
        spans = [_span("gece", "data", base, base + 300)]
        steps = [{"ts": "00:01:00", "msg": "sarmali"}]
        out = associate_steps(spans, steps)
        assert out.get("gece"), "gece-yarısı sarması eşlenmeli"

    def test_bad_ts_ignored(self):
        spans = [_span("a", "data", T0, T0 + 10)]
        out = associate_steps(spans, [{"ts": "bozuk", "msg": "x"}])
        assert out == {}


# ---------------------------------------------------------------------------
# _build_timeline_spans (JSONL replay)
# ---------------------------------------------------------------------------


class TestBuildTimelineSpans:
    def test_begin_end_pairing_from_events(self):
        events = [
            {
                "event": "timeline",
                "op": "begin",
                "key": "a",
                "lane": "data",
                "label": "A",
                "t0": T0,
                "sub": False,
                "round": 1,
                "meta": {},
            },
            {
                "event": "timeline",
                "op": "end",
                "key": "a",
                "t1": T0 + 30,
                "status": "ok",
                "meta": {"n_bars": 5},
            },
        ]
        spans = _build_timeline_spans(events)
        assert len(spans) == 1
        assert spans[0]["t1"] == T0 + 30 and spans[0]["status"] == "ok"
        assert spans[0]["meta"]["n_bars"] == 5

    def test_orphan_running_closed_warn(self):
        events = [
            {
                "event": "timeline",
                "op": "begin",
                "key": "a",
                "lane": "llm",
                "label": "A",
                "t0": T0,
                "sub": False,
                "round": 1,
                "meta": {},
            },
            {
                "event": "timeline",
                "op": "begin",
                "key": "b",
                "lane": "backtest",
                "label": "B",
                "t0": T0 + 10,
                "sub": False,
                "round": 1,
                "meta": {},
            },
            {
                "event": "timeline",
                "op": "end",
                "key": "b",
                "t1": T0 + 40,
                "status": "ok",
                "meta": {},
            },
            {
                "event": "session_end",
                "ts": datetime.fromtimestamp(T0 + 50, UTC).isoformat(),
            },
        ]
        spans = _build_timeline_spans(events)
        a = next(sp for sp in spans if sp["key"] == "a")
        assert a["t1"] is not None and a["status"] == "warn"

    def test_phase_change_fallback_for_legacy_sessions(self):
        iso = lambda t: datetime.fromtimestamp(t, UTC).isoformat()  # noqa: E731
        events = [
            {
                "event": "phase_change",
                "ts": iso(T0),
                "phase_idx": 0,
                "phase_label": "Veri yükleniyor",
                "status": "running",
            },
            {
                "event": "phase_change",
                "ts": iso(T0 + 60),
                "phase_idx": 0,
                "phase_label": "Veri yükleniyor",
                "status": "done",
            },
            {
                "event": "phase_change",
                "ts": iso(T0 + 60),
                "phase_idx": 2,
                "phase_label": "Backtest döngüsü",
                "status": "running",
            },
        ]
        spans = _build_timeline_spans(events)
        assert len(spans) == 2
        veri = next(sp for sp in spans if sp["lane"] == "data")
        assert veri["t1"] == T0 + 60 and veri["status"] == "ok"
        bt = next(sp for sp in spans if sp["lane"] == "backtest")
        assert bt["status"] == "warn"  # açık kaldı → warn kapanış


# ---------------------------------------------------------------------------
# _make_rob_progress (4-marker parse köprüsü)
# ---------------------------------------------------------------------------


class TestMakeRobProgress:
    def test_markers_open_and_close_subspans(self):
        rid = "tltest05"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[rid] = {"timeline": [], "steps": []}
        try:
            pf = ab._make_rob_progress(rid, 1, 1)
            pf("🌐 Multi-Symbol — test")
            pf("  [ETHUSDT] Veri yükleniyor…")
            pf("  → 2/2 sembolde pozitif")
            pf("📊 IS/OOS Split — test")
            pf("🎲 Monte Carlo — test")
            pf("  ⚠ Monte Carlo atlandı — trade yok")
            tl = ab._AGENT_PROGRESS[rid]["timeline"]
            keys = {sp["key"]: sp for sp in tl}
            assert "rob-r1-c1-ms" in keys and keys["rob-r1-c1-ms"]["status"] == "ok"
            # 📊 açıldı; 🎲 gelince (yeni marker) ok kapandı
            assert keys["rob-r1-c1-isoos"]["status"] == "ok"
            assert keys["rob-r1-c1-mc"]["status"] == "warn"
            assert all(sp["sub"] for sp in tl)
            # step'ler de akmış olmalı
            assert len(ab._AGENT_PROGRESS[rid]["steps"]) == 6
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(rid, None)
