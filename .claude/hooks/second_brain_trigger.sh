#!/usr/bin/env bash
# UserPromptSubmit hook: kullanıcı "beyne yaz" (veya "beynime yaz/kaydet")
# yazınca ikinci-beyin vault'una yazma talimatını bağlama enjekte eder.
input=$(cat)
prompt=$(printf '%s' "$input" | jq -r '.prompt // ""' 2>/dev/null)
# Türkçe küçük harfe indir (I/İ dahil), tetik kelimeleri ara
low=$(printf '%s' "$prompt" | tr '[:upper:]' '[:lower:]')
if printf '%s' "$low" | grep -qiE 'beyne yaz|beynime (yaz|kaydet)|ikinci beyne (yaz|kaydet)'; then
  VAULT="/Users/i034216/Desktop/obsidian/murat_obsidian"
  ctx="Kullanıcı 'beyne yaz' tetiğini kullandı. Bu oturumdaki/istenen kalıcı bilgiyi "
  ctx+="$VAULT vault'una Karpathy deseniyle işle: sources/ (gerekiyorsa, immutable) + "
  ctx+="wiki/{entities,concepts,synthesis,tutorials}/ sentez sayfası + [[bare-name]] "
  ctx+="bağlar + log.md'ye append; sonra 'cd $VAULT && python tools/wiki_tools.py "
  ctx+="backlinks && python tools/wiki_tools.py index && python tools/wiki_tools.py lint'."
  jq -n --arg c "$ctx" '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$c}}'
fi
exit 0
