"use strict";

/* ─── Indicators ─────────────────────────────────────────────────────────── */

// İstemci-tarafı indikatör hesapları KALDIRILDI — indikatörler artık
// /chart/data'dan (d.indicators) sunucu-kanonik olarak geliyor
// (chart_indicators.py). Bu kopyalar hiçbir yerden çağrılmıyordu ve NAU
// parite kütüphanesiyle drift riski taşıyordu.

/* ─── Chart factory ──────────────────────────────────────────────────────── */

window.initPriceChart = function(containerId, candles, trades, opts) {
  opts = opts || {};
  const indicators = opts.indicators || { overlays: [], panes: [] };
  const container = document.getElementById(containerId);
  if (!container) return;

  // Eski chart'ları + observer'ları HER ZAMAN temizle (empty check'ten ÖNCE) — #15, #6
  if (container.__lwCharts) {
    container.__lwCharts.forEach(c => { try { c.remove(); } catch(e) {} });
    container.__lwCharts = null;
  }
  if (container.__resizeObserver) {
    try { container.__resizeObserver.disconnect(); } catch(e) {}
    container.__resizeObserver = null;
  }
  // Eski trade highlight handler'larını temizle (kaldırılmış chart'a bağlı kalmasın) — #15
  window._priceChartHighlight = null;
  window._priceChartClearHighlight = null;
  window._drawTradeOnChart = null;
  window._priceChartZoom = null;
  container.__chartApi = null;  // #11: bayat api disposed chart'a bağlı kalmasın (erken dönüşlerde de temizlen)

  if (!candles || candles.length === 0) {
    container.innerHTML = '<div class="empty-state" style="padding:40px;">Veri yok</div>';
    return;
  }
  container.innerHTML = "";

  const LW = window.LightweightCharts;
  if (!LW) { console.warn("LightweightCharts not loaded"); return; }

  const closes = candles.map(c => c.close);
  const times  = candles.map(c => c.time);

  // ── Trade markers + giriş/çıkış arası çizgiler (TradingView-tarzı) ───────
  // Çok trade'li grafikte (>120) marker METİNLERİ bastırılır — 600+ etiket
  // grafiği okunmaz kılıyor; oklar + PnL-renkli çizgiler kalır, detay trade
  // tablosundan (tıkla → vurgula) alınır.
  const showText = (trades || []).length <= 120;
  const markers = [];
  (trades || []).forEach(t => {
    const isSell = t.side === "SELL";
    if (t.entry_time) markers.push({
      time: t.entry_time,
      position: isSell ? "aboveBar" : "belowBar",
      shape:    isSell ? "arrowDown" : "arrowUp",
      color:    isSell ? "#f87171"   : "#4ade80",
      text:     showText ? (isSell ? "Sell " : "Buy ") + "$" + (t.entry_price || 0).toFixed(0) : undefined,
      size: 2,
    });
    if (t.exit_time) {
      const pnlStr = t.pnl != null ? ((t.pnl >= 0 ? "+" : "") + t.pnl.toFixed(0) + "$") : "";
      // Çıkış sebebi eki: sl/tp/signal/flip/eob (varsa)
      const kindStr = { sl: " [SL]", tp: " [TP]", signal: " [EXIT]", flip: " [FLIP]", eob: " [EOB]" }[t.exit_kind] || "";
      markers.push({
        time: t.exit_time,
        position: isSell ? "belowBar" : "aboveBar",
        shape:    isSell ? "arrowUp" : "arrowDown",
        color:    t.pnl >= 0 ? "#4ade80" : "#f87171",
        text:     showText ? (isSell ? "Cover " : "Sell ") + "$" + (t.exit_price || 0).toFixed(0) + (pnlStr ? " " + pnlStr : "") + kindStr : undefined,
        size: 1.5,
      });
    }
  });
  markers.sort((a, b) => a.time - b.time);

  // ── Layout: 1 ana panel + spec'in gerektirdiği kadar alt panel ──────────
  const overlays = indicators.overlays || [];
  const panes    = indicators.panes || [];
  const nSub     = panes.length;
  const totalH   = opts.height || 580;
  const mainH    = nSub ? Math.round(totalH * (0.62 - 0.04 * Math.min(nSub, 3))) : totalH;
  const subH     = nSub ? Math.round((totalH - mainH) / nSub) : 0;

  const darkBase = {
    layout:    { background: { color: "#0c0c0c" }, textColor: "#8a8a8a" },
    grid:      { vertLines: { color: "#242424" }, horzLines: { color: "#242424" } },
    rightPriceScale: { borderColor: "#242424" },
    timeScale: { borderColor: "#242424", timeVisible: true, secondsVisible: false, visible: false },
    crosshair: { mode: 1 },
    handleScroll: { mouseWheel: true },
    handleScale:  { mouseWheel: true },
  };

  const makeDiv = (h, borderTop) => {
    const d = document.createElement("div");
    d.style.cssText = `height:${h}px;width:100%;position:relative;${borderTop ? "border-top:1px solid #242424;" : ""}`;
    container.appendChild(d);
    return d;
  };
  const addLabel = (div, text) => {
    const el = document.createElement("div");
    el.style.cssText = "position:absolute;top:3px;left:6px;font-size:11px;color:#c8c8c8;font-family:var(--mono);pointer-events:none;font-weight:500;z-index:2;";
    el.textContent = text;
    div.appendChild(el);
  };

  // ── Ana panel (mum) ──────────────────────────────────────────────────────
  const mainDiv = makeDiv(mainH, false);
  const mainChart = LW.createChart(mainDiv, {
    ...darkBase,
    timeScale: { ...darkBase.timeScale, visible: nSub === 0 },
    width: container.clientWidth,
    height: mainH,
  });

  const candleSeries = mainChart.addCandlestickSeries({
    upColor: "#4ade80", downColor: "#f87171",
    wickUpColor: "#4ade80", wickDownColor: "#f87171",
    borderVisible: false,
  });
  candleSeries.setData(candles);
  if (markers.length) candleSeries.setMarkers(markers);

  // Giriş-çıkış arası yatay çizgiler
  (trades || []).forEach(t => {
    if (!t.entry_time || !t.exit_time || !t.entry_price || !t.exit_price) return;
    const color = t.pnl >= 0 ? "rgba(74,222,128,0.55)" : "rgba(248,113,113,0.55)";
    const lineSeries = mainChart.addLineSeries({
      color, lineWidth: 1, lineStyle: 0,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    lineSeries.setData([
      { time: t.entry_time, value: t.entry_price },
      { time: t.exit_time,  value: t.exit_price  },
    ]);
  });

  // ── Overlay indikatörleri (fiyat panelinde) — stratejinin gerçek çizgileri ─
  const overlayNames = [];
  overlays.forEach(ov => {
    if (!ov.data || !ov.data.length) return;
    const s = mainChart.addLineSeries({
      color: ov.color || "#f59e0b", lineWidth: 1.5,
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
      title: ov.name,
    });
    s.setData(ov.data);
    overlayNames.push(ov.name);
  });
  if (overlayNames.length) addLabel(mainDiv, overlayNames.join(" · "));

  // ── Alt paneller (RSI vb.) — stratejinin gerçek osilatörleri ─────────────
  const addRef = (chart, val, col) => {
    if (!times.length) return;
    const s = chart.addLineSeries({ color: col, lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    s.setData([{ time: times[0], value: val }, { time: times[times.length-1], value: val }]);
  };

  const subCharts = [];
  panes.forEach((pane, pi) => {
    const isLast = pi === panes.length - 1;
    const div = makeDiv(subH, true);
    const chart = LW.createChart(div, {
      ...darkBase,
      timeScale: { ...darkBase.timeScale, visible: isLast },
      width: container.clientWidth,
      height: subH,
    });
    (pane.series || []).forEach(ser => {
      const s = chart.addLineSeries({ color: ser.color || "#a78bfa", lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false });
      s.setData(ser.data || []);
    });
    (pane.refs || []).forEach(ref => addRef(chart, ref.value, ref.color));
    addLabel(div, pane.label || "");
    subCharts.push(chart);
  });

  // ── Zaman eksenlerini senkronize et ──────────────────────────────────────
  const allCharts = [mainChart, ...subCharts];
  let syncing = false;
  allCharts.forEach((src, si) => {
    src.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (syncing || !range) return;
      syncing = true;
      allCharts.forEach((dst, di) => { if (di !== si) dst.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    });
  });

  // Fit and resize
  allCharts.forEach(c => c.timeScale().fitContent());
  const ro = new ResizeObserver(() => {
    allCharts.forEach(c => c.applyOptions({ width: container.clientWidth }));
  });
  ro.observe(container);
  container.__lwCharts = allCharts;
  container.__resizeObserver = ro;  // sonraki render'da disconnect edilecek (#6)

  // ── Trade hover highlight ─────────────────────────────────────────────────
  let _highlightSeries = null;
  const _chartTimeMin = candles.length ? candles[0].time  : 0;
  const _chartTimeMax = candles.length ? candles[candles.length-1].time : 0;

  function _reloadForTrade(entryTime, exitTime, entryPrice, exitPrice, pnl, tf) {
    const el = container;  // closure — /reports'ta birden çok grafik olabilir
    if (!el) return;
    const sym = el.dataset.priceSymbol || "BTCUSDT";
    const cat = el.dataset.priceCategory || "linear";
    // tf seçimi
    let interval = tf;
    if (!interval || interval === "auto") {
      const durMins = Math.round((exitTime - entryTime) / 60);
      interval = durMins <= 30 ? "1" : durMins <= 180 ? "5" : durMins <= 720 ? "15" : durMins <= 2880 ? "60" : "240";
    }
    const margin = Math.round((exitTime - entryTime) * 4);
    const sid = el.dataset.priceSpec || "";
    let newSrc = `/chart/data?symbol=${sym}__AMP__category=${cat}__AMP__interval=${interval}__AMP__start_ts=${entryTime - margin}__AMP__end_ts=${exitTime + margin}`;
    if (sid) newSrc += `__AMP__spec_id=${sid}`;
    el.setAttribute("data-price-chart", newSrc);
    el.setAttribute("data-price-interval", interval);
    el._chartDone = false;
    // TF buton güncelle (kendi panelindekiler)
    const _panel = container.closest(".panel") || document;
    _panel.querySelectorAll(".chart-tf-btn").forEach(b => {
      b.style.background = b.dataset.tf === interval ? "rgba(255,255,255,0.12)" : "";
      b.style.color      = b.dataset.tf === interval ? "#f2f2f2" : "";
    });
    // Chart yükle, sonra highlight çiz (async reload sonrası _afterLoad tetiklenir)
    el._afterLoad = function() {
      if (entryPrice || exitPrice) {
        if (window._drawTradeOnChart) window._drawTradeOnChart(entryTime, exitTime, entryPrice, exitPrice, pnl);
      } else {
        // #10: zoom yolu (fiyat 0,0) — highlight çizme, yalnız görünür aralığı ayarla
        const _m = Math.round((exitTime - entryTime) * 3);
        if (container.__lwCharts) container.__lwCharts.forEach(c => { try { c.timeScale().setVisibleRange({ from: entryTime - _m, to: exitTime + _m }); } catch (e) {} });
      }
    };
    _loadPriceChart(el);
  }

  // Giriş-çıkış çizgisini ve highlight'ı çiz
  window._drawTradeOnChart = function(entryTime, exitTime, entryPrice, exitPrice, pnl) {
    if (_highlightSeries) {
      try { mainChart.removeSeries(_highlightSeries); } catch(e) {}
      _highlightSeries = null;
    }
    const color = pnl >= 0 ? "#4ade80" : "#f87171";
    const pnlLabel = (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + " $";

    _highlightSeries = mainChart.addLineSeries({
      color, lineWidth: 2.5, lineStyle: 0,
      priceLineVisible: false,
      lastValueVisible: true,
      lastValueLabel: pnlLabel,
      crosshairMarkerVisible: false,
      title: pnlLabel,
    });
    _highlightSeries.setData([
      { time: entryTime, value: entryPrice },
      { time: exitTime,  value: exitPrice  },
    ]);
    // Çizginin orta noktasına PnL marker ekle
    const midTime = Math.round((entryTime + exitTime) / 2);
    const midPrice = (entryPrice + exitPrice) / 2;
    _highlightSeries.setMarkers([{
      time: midTime,
      position: pnl >= 0 ? "aboveBar" : "belowBar",
      shape: "text",
      color: color,
      text: pnlLabel,
      size: 0,
    }]);
    const margin = Math.round((exitTime - entryTime) * 2);
    mainChart.timeScale().setVisibleRange({ from: entryTime - margin, to: exitTime + margin });
    subCharts.forEach(c => { try { c.timeScale().setVisibleRange({ from: entryTime - margin, to: exitTime + margin }); } catch(e) {} });
  };

  window._priceChartHighlight = function(entryTime, exitTime, entryPrice, exitPrice, pnl, tf) {
    // Trade chart aralığında değilse reload
    if (entryTime < _chartTimeMin || exitTime > _chartTimeMax || (tf && tf !== "auto" && tf !== container.dataset?.priceInterval)) {
      _reloadForTrade(entryTime, exitTime, entryPrice, exitPrice, pnl, tf || "auto");
      return;
    }
    window._drawTradeOnChart(entryTime, exitTime, entryPrice, exitPrice, pnl);
  };

  window._priceChartClearHighlight = function() {
    if (_highlightSeries) {
      try { mainChart.removeSeries(_highlightSeries); } catch(e) {}
      _highlightSeries = null;
    }
    allCharts.forEach(c => { try { c.timeScale().fitContent(); } catch(e) {} });
  };

  window._priceChartZoom = function(entryTime, exitTime, tf) {
    if (entryTime < _chartTimeMin || exitTime > _chartTimeMax || (tf && tf !== "auto")) {
      _reloadForTrade(entryTime, exitTime, 0, 0, 0, tf || "auto");
      return;
    }
    const margin = Math.round((exitTime - entryTime) * 3);
    allCharts.forEach(c => { try { c.timeScale().setVisibleRange({ from: entryTime - margin, to: exitTime + margin }); } catch(e) {} });
  };

  // M10: panel-scoped API — window global'leri SON init edilen grafiği
  // işaret ediyordu; iki rapor detayı açıkken ilk tablodaki trade tıklaması
  // İKİNCİ grafiği oynatıyordu (yanlış highlight/zoom + yanlış pencereye
  // reload). Tüketiciler grafiği kendi fragment kökünden __chartApi ile
  // çözer; window alias'ları tek-grafik sayfalar için geriye-uyum kalır.
  container.__chartApi = {
    draw: window._drawTradeOnChart,
    highlight: window._priceChartHighlight,
    clear: window._priceChartClearHighlight,
    zoom: window._priceChartZoom,
  };

  // _afterLoad callback (reload sonrası highlight)
  const _el = container;
  if (_el && _el._afterLoad) {
    const cb = _el._afterLoad;
    _el._afterLoad = null;
    try { cb(); } catch(e) {}
  };
};

/* ─── Price Chart Loader ─────────────────────────────────────────────────── */
function _loadPriceChart(el) {
  if (el._chartDone) return;
  const src = (el.getAttribute("data-price-chart") || "")
    .replace(/__AMP__/g, "&").replace(/&amp;/g, "&");
  if (!src) return;
  el._chartDone = true;
  const _seq = (el._loadSeq = (el._loadSeq || 0) + 1);  // #12: yarış jetonu
  let trades = [];
  try {
    const raw = (el.getAttribute("data-price-trades") || "[]")
      .replace(/&#34;/g, '"').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
    trades = JSON.parse(raw);
  } catch(e) {}
  fetch(src)
    .then(r => r.json())
    .then(d => {
      if (_seq !== el._loadSeq) return;  // #12: geç dönen eski istek — yoksay
      if (!el.isConnected) { el._chartDone = false; el._afterLoad = null; return; }  // #13: satır kapandıysa reopen'da yeniden yüklensin
      if (d.error && (!d.candles || !d.candles.length)) {
        // Örn. pencere/TF fizibilite reddi — kullanıcıya nedenini söyle
        el._chartDone = false;
        el._afterLoad = null;  // #11: hatada bayat highlight callback'ini temizle
        el.innerHTML = '<div class="empty-state" style="padding:40px;">⚠ ' + d.error + '</div>';
        return;
      }
      window.initPriceChart(el.id, d.candles, trades.length ? trades : (d.trades || []), { indicators: d.indicators });
    })
    .catch(e => { el._chartDone = false; el._afterLoad = null; console.warn("chart load failed:", src, e); });
}

function _initAllCharts() {
  document.querySelectorAll("[data-price-chart]").forEach(_loadPriceChart);
}

// MutationObserver — DOM'a [data-price-chart] eklenince anında çalışır
const _chartObserver = new MutationObserver(function(mutations) {
  mutations.forEach(function(m) {
    m.addedNodes.forEach(function(node) {
      if (node.nodeType !== 1) return;
      if (node.hasAttribute && node.hasAttribute("data-price-chart")) {
        _loadPriceChart(node);
      }
      if (node.querySelectorAll) {
        node.querySelectorAll("[data-price-chart]").forEach(_loadPriceChart);
      }
    });
  });
});

function _startObserver() {
  _chartObserver.observe(document.body, { childList: true, subtree: true });
  _initAllCharts();
}

if (document.body) {
  _startObserver();
} else {
  document.addEventListener("DOMContentLoaded", _startObserver);
}
