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
