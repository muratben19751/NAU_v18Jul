#!/usr/bin/env bash
# Knowledge-graph "add" loop enforcement (PostToolUse, Write|Edit).
#
# When a .py file or a web/templates file is edited, this:
#   1) runs the MECHANICAL wiki sync (backlinks + index) so the graph's
#      auto-generated sections stay current, and
#   2) injects a reminder for the agent to finish the "add" loop — update the
#      relevant nautilus_wiki page and add/refresh the module docstring's
#      Wiki References [[link]]s.
#
# Non-matching paths exit silently. Reads the PostToolUse hook JSON on stdin.
set -euo pipefail

f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null || true)
[ -n "$f" ] || exit 0

case "$f" in
  *.py|*/web/templates/*) ;;
  *) exit 0 ;;
esac

[ -f "$f" ] || exit 0

D="${CLAUDE_PROJECT_DIR:-$(pwd)}"
TOOLS="$D/nautilus_wiki/tools/wiki_tools.py"
[ -f "$TOOLS" ] || exit 0

python3 "$TOOLS" backlinks >/dev/null 2>&1 || true
python3 "$TOOLS" index >/dev/null 2>&1 || true

# Emit additionalContext back to the model (suppressOutput hides raw stdout).
jq -n --arg f "$f" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("knowledge-graph ekle-döngüsü (" + $f + " değişti): mekanik sync (backlinks+index) çalıştırıldı. Şimdi ekle-döngüsünü tamamla: (1) ilgili nautilus_wiki sayfasını güncelle, (2) modül docstring Wiki References + [[link]] ekle/güncelle. Küçük iç refactor ise atla ve kullanıcıya belirt.")
  },
  suppressOutput: true
}'
