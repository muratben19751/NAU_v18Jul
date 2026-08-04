#!/usr/bin/env bash
# Stop hook — ARTIK BLOKLAMAZ.
#
# Eskiden bu hook turun sonunda `decision:"block"` döndürüp Claude'a "ikinci
# beyne yaz" dedirtiyordu. Sonuç: her turda fazladan bir asistan turu doğuyor,
# vault yazımının araç çağrıları + rapor satırı asıl cevabın ALTINA düşüyor ve
# cevabı ekrandan yukarı itiyordu (2026-08-04 kullanıcı geri bildirimi:
# "cevap yukarıda kaldı, ben bunu istemiyorum").
#
# Yeni akış — sıra tersine: burada yalnızca "işlenmemiş tur var" işareti
# bırakılır; yazma bir SONRAKİ kullanıcı sorusunun başında, cevaptan ÖNCE
# yapılır (second_brain_trigger.sh). Böylece araç çağrıları hep cevabın
# üstünde kalır, son söz cevabındır.
#
# Bilinen sınır: oturumun SON turu bir sonraki soru gelmediği için işlenmez —
# o durumda kullanıcı "beyne yaz" tetiğini kullanır.
input=$(cat)
active=$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)
if [ "$active" = "true" ]; then
  exit 0   # döngü önleme (artık bloklamıyoruz ama zararsız kalsın)
fi

sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[ -n "$sid" ] || exit 0

# Makine-bağımsız vault seçimi: Mac ve Windows yolları farklı — var olanı kullan.
VAULT=""
for v in "$HOME/OneDrive/Desktop/myOBSIDIAN/murat_obsidian" \
         "$HOME/Desktop/obsidian/murat_obsidian"; do
  if [ -d "$v" ]; then VAULT="$v"; break; fi
done
[ -n "$VAULT" ] || exit 0   # vault bu makinede yoksa sessizce geç

dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.second_brain"
mkdir -p "$dir" 2>/dev/null || exit 0
printf '%s\n' "$VAULT" >"$dir/$sid.pending" 2>/dev/null
# Kapanmayan oturumların artıkları birikmesin.
find "$dir" -name '*.pending' -mtime +7 -delete 2>/dev/null
exit 0
