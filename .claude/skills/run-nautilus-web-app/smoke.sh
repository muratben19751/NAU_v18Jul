#!/usr/bin/env bash
# smoke.sh — Nautilus Web App'i arka planda başlatır, temel route'ları test eder,
# temiz çıkar. Başarısızlıkta sıfır olmayan exit code döner.
set -euo pipefail

VENV="${VENV:-.venv}"
PORT="${PORT:-8000}"
HOST="127.0.0.1"
BASE="http://${HOST}:${PORT}"
LOG="/tmp/nautilus_server_$$.log"

# --- Başlat ---
echo "▶ Sunucu başlatılıyor (port $PORT)…"
"${VENV}/bin/uvicorn" server:app \
  --host "$HOST" --port "$PORT" --log-level warning \
  >"$LOG" 2>&1 &
SERVER_PID=$!

# Temizleme: script çıkınca sunucuyu öldür
trap 'kill "$SERVER_PID" 2>/dev/null; rm -f "$LOG"' EXIT

# Hazır olmasını bekle (max 15s)
for i in $(seq 1 30); do
  if python3 -c "
import urllib.request, sys
try:
    urllib.request.urlopen('${BASE}/', timeout=1)
    sys.exit(0)
except: sys.exit(1)
" 2>/dev/null; then
    echo "✓ Sunucu hazır (${i}×0.5s)"
    break
  fi
  sleep 0.5
done

# --- Route smoke testleri ---
FAIL=0
check() {
  local path="$1" expect="${2:-200}" timeout="${3:-10}"
  local code
  code=$(python3 -c "
import urllib.request, urllib.error, sys
try:
    r = urllib.request.urlopen('${BASE}${path}', timeout=${timeout})
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print('ERR')
" 2>/dev/null)
  if [ "$code" = "$expect" ]; then
    echo "  ✓ ${path} → ${code}"
  else
    echo "  ✗ ${path} → ${code} (beklenen: ${expect})"
    FAIL=1
  fi
}

echo "▶ Route kontrolleri…"
check "/"
check "/backtest"
check "/strategy"
check "/agent"
check "/wiki"
check "/data" 200 30   # ilk açılışta Parquet katalog yazıyor → yavaş
check "/reports"
check "/lab"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "✅ Tüm smoke testler geçti."
else
  echo "❌ Bazı testler başarısız. Log: $LOG"
  cat "$LOG"
  exit 1
fi
