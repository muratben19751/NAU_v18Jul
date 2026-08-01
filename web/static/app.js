// Nautilus Lab — Chart.js helpers + small UX bits

window.NautilusLab = (function () {
  let equityChart = null;

  function fmtMoney(n) {
    if (n === null || n === undefined) return "—";
    const sign = n < 0 ? "-" : "";
    return sign + "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function renderEquity(canvasId, points) {
    const el = document.getElementById(canvasId);
    if (!el || !window.Chart) return;
    const labels = points.map((_, i) => i);
    const data = points;

    if (equityChart) {
      equityChart.data.labels = labels;
      equityChart.data.datasets[0].data = data;
      equityChart.update("none");
      return;
    }

    equityChart = new Chart(el, {
      type: "line",
      data: {
        labels,
        datasets: [{
          data,
          borderColor: "#e5e5e5",
          backgroundColor: "rgba(255,255,255,0.08)",
          borderWidth: 2,
          fill: true,
          pointRadius: 0,
          tension: 0.15,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: {
          callbacks: { label: (ctx) => fmtMoney(ctx.parsed.y) },
          backgroundColor: "#0c0c0c",
          borderColor: "#242424",
          borderWidth: 1,
          titleFont: { family: "JetBrains Mono", size: 11 },
          bodyFont: { family: "JetBrains Mono", size: 12 },
        }},
        scales: {
          x: { display: false, grid: { display: false } },
          y: {
            grid: { color: "#242424" },
            ticks: {
              color: "#8a8a8a",
              font: { family: "JetBrains Mono", size: 10 },
              callback: (v) => "$" + (v/1000).toFixed(0) + "k",
            },
          },
        },
      },
    });
  }

  function resetEquity() {
    if (equityChart) { equityChart.destroy(); equityChart = null; }
  }

  return { renderEquity, resetEquity, fmtMoney };
})();

// Refresh equity from JSON endpoint whenever HTMX swaps a marker element
document.body.addEventListener("htmx:afterSwap", (evt) => {
  const marker = document.getElementById("equity-marker");
  if (marker && marker.dataset.reload === "1") {
    marker.dataset.reload = "0";
    fetch("/fragments/equity.json")
      .then(r => r.json())
      .then(j => { if (j.points && j.points.length) NautilusLab.renderEquity("equity-canvas", j.points); });
  }
});

// ── MAX date-range filler ────────────────────────────────────────────────────
// Shared by every backtest surface's date pair: fetches the cached coverage of
// the selected series (GET /data/range or a prebuilt URL) and fills the Start/
// End inputs. `params` is an object of query params, or {url: "..."} to hit a
// surface-specific endpoint (Builder). Targets accept an element or selector.
window.nlMaxRange = function (btn, params, startTarget, endTarget) {
  function el(t) { return typeof t === "string" ? document.querySelector(t) : t; }
  var url = params && params.url
    ? params.url
    : "/data/range?" + new URLSearchParams(params).toString();
  var original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  fetch(url)
    .then(function (r) { if (!r.ok) throw new Error("no-data"); return r.json(); })
    .then(function (d) {
      var s = el(startTarget), e = el(endTarget);
      if (s) { s.value = d.start; s.dispatchEvent(new Event("change", { bubbles: true })); }
      if (e) { e.value = d.end; e.dispatchEvent(new Event("change", { bubbles: true })); }
      btn.textContent = original;
    })
    .catch(function () {
      btn.textContent = "veri yok";
      setTimeout(function () { btn.textContent = original; }, 1800);
    })
    .finally(function () { btn.disabled = false; });
};
