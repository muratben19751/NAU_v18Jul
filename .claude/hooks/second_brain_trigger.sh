#!/usr/bin/env bash
# UserPromptSubmit hook — ikinci beyin yazımının TEK tetikleyicisi.
#
# İki yol var:
#   1) Açık tetik: kullanıcı "beyne yaz" (veya "beynime yaz/kaydet") yazdı.
#   2) Bekleyen tur: bir önceki tur bitti (second_brain_stop.sh işaret bıraktı)
#      ve geçen turun kalıcı öğrenilenleri henüz işlenmedi.
#
# İkincisi bilerek turun BAŞINDA çalışır: yazma cevaptan önce yapılınca araç
# çağrıları cevabın üstünde kalır ve asıl yanıt ekranın en altında olur.
input=$(cat)
prompt=$(printf '%s' "$input" | jq -r '.prompt // ""' 2>/dev/null)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)

# Makine-bağımsız vault seçimi: Mac ve Windows yolları farklı — var olanı kullan.
VAULT=""
for v in "$HOME/OneDrive/Desktop/myOBSIDIAN/murat_obsidian" \
         "$HOME/Desktop/obsidian/murat_obsidian"; do
  if [ -d "$v" ]; then VAULT="$v"; break; fi
done
[ -n "$VAULT" ] || exit 0   # vault bu makinede yoksa sessizce geç

pending=""
[ -n "$sid" ] && pending="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.second_brain/$sid.pending"

# Türkçe küçük harfe indir (I/İ dahil), tetik kelimeleri ara
low=$(printf '%s' "$prompt" | tr '[:upper:]' '[:lower:]')

ctx=""
if printf '%s' "$low" | grep -qiE 'beyne yaz|beynime (yaz|kaydet)|ikinci beyne (yaz|kaydet)'; then
  ctx="Kullanıcı 'beyne yaz' tetiğini kullandı. Bu oturumdaki/istenen kalıcı bilgiyi "
  ctx+="$VAULT vault'una Karpathy deseniyle işle: sources/ (gerekiyorsa, immutable) + "
  ctx+="wiki/{entities,concepts,synthesis,tutorials}/ sentez sayfası + [[bare-name]] "
  ctx+="bağlar + log.md'ye append; sonra 'cd $VAULT && python tools/wiki_tools.py "
  ctx+="backlinks && python tools/wiki_tools.py index && python tools/wiki_tools.py lint'."
elif [ -n "$pending" ] && [ -f "$pending" ]; then
  ctx="İkinci beyin (sessiz arka plan işi, ÖNCE bunu yap, SONRA kullanıcının mesajını yanıtla): "
  ctx+="Bir önceki turda KALICI değeri olan bir şey öğrenildiyse (yeni kaynak, kavram, karar, "
  ctx+="proje özeti) onu $VAULT vault'una Karpathy deseniyle yaz — sources/ (immutable, "
  ctx+="gerekiyorsa) + wiki/ sentez sayfası + [[bare-name]] bağlar + log.md'ye append — sonra "
  ctx+="'cd $VAULT && python tools/wiki_tools.py backlinks && python tools/wiki_tools.py index "
  ctx+="&& python tools/wiki_tools.py lint' çalıştır. Önceki tur önemsiz/geçici ise (küçük "
  ctx+="düzeltme, sohbet) HİÇBİR ŞEY yazma ve bundan HİÇ SÖZ ETME — doğrudan kullanıcının "
  ctx+="mesajını yanıtla. Yazdıysan yanıtının EN BAŞINA tek satır '✎ ikinci beyin: N sayfa' "
  ctx+="koy, hemen ardından asıl cevabı ver: hangi sayfaya ne yazdığını AÇIKLAMA, madde/liste "
  ctx+="verme, lint çıktısını dökme. Kural: son söz her zaman kullanıcının sorusunun cevabıdır."
else
  exit 0
fi

[ -n "$pending" ] && rm -f "$pending" 2>/dev/null
jq -n --arg c "$ctx" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$c}}'
exit 0
