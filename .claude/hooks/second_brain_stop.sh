#!/usr/bin/env bash
# Stop hook: her oturum sonunda Claude'a "bu oturumun kalıcı öğrenilenlerini
# ikinci-beyin vault'una yaz" talimatı enjekte eder. Döngü-güvenli: Claude zaten
# bir Stop-hook tetiğiyle çalışıyorsa (stop_hook_active=true) hiçbir şey yapmaz.
input=$(cat)
active=$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)
if [ "$active" = "true" ]; then
  exit 0   # döngü önleme: ikinci kez tetiklenmede sessizce geç
fi
# Makine-bağımsız vault seçimi: Mac ve Windows yolları farklı — var olanı kullan.
VAULT=""
for v in "$HOME/OneDrive/Desktop/myOBSIDIAN/murat_obsidian" \
         "$HOME/Desktop/obsidian/murat_obsidian"; do
  if [ -d "$v" ]; then VAULT="$v"; break; fi
done
[ -n "$VAULT" ] || exit 0   # vault bu makinede yoksa sessizce geç
msg="İkinci beyin kontrolü: Bu oturumda KALICI değeri olan bir şey öğrenildiyse "
msg+="(yeni kaynak, kavram, karar, proje özeti) onu $VAULT vault'una Karpathy "
msg+="deseniyle yaz — sources/ (immutable, gerekiyorsa) + wiki/ sentez sayfası + "
msg+="[[bare-name]] bağlar + log.md'ye append — sonra 'cd $VAULT && python "
msg+="tools/wiki_tools.py backlinks && python tools/wiki_tools.py index && python "
msg+="tools/wiki_tools.py lint' çalıştır. Bu oturum önemsiz/geçici ise (küçük "
msg+="düzeltme, sohbet) HİÇBİR ŞEY yazma, sadece kısaca 'ikinci beyne yazılacak "
msg+="kalıcı bir şey yok' de ve dur."
jq -n --arg r "$msg" '{decision:"block", reason:$r}'
