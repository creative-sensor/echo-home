#!/bin/bash
# llamark-test.sh — feel the speedup of a restored per-file KV cache vs. cold prefill.
# Usage: llamark-test.sh [--cold] <uvian file> ["question"] [slot]
#   --cold  also run the same question with no cache (full prefill of the file)
# Env: LLAMARK_URL (default http://localhost:8080), MAXTOK (default 1024)
set -euo pipefail

COLD=0; [ "${1:-}" = "--cold" ] && { COLD=1; shift; }
FILE=${1:?usage: llamark-test.sh [--cold] <uvian file> [question] [slot]}
Q=${2:-"Summarize this document in one sentence."}
SLOT=${3:-0}
URL=${LLAMARK_URL:-http://localhost:8080}
LM="$(dirname "$0")/llamark.sh"

hr() { printf '%s\n' "------------------------------------------------------------"; }

ask() {  # $1 = cache_prompt true|false — system message is the file itself, byte-exact
  jq -n --rawfile s "$FILE" --arg q "$Q" --argjson sl "$SLOT" --argjson c "$1" \
    '{messages:[{role:"system",content:$s},{role:"user",content:$q}],
      id_slot:$sl, cache_prompt:$c, max_tokens:(env.MAXTOK//"1024"|tonumber), temperature:0}' \
  | curl -sf -w '\n%{time_total}' "$URL/v1/chat/completions" -H 'Content-Type: application/json' -d @-
}

report() {  # $1 = label, $2 = raw response (json + "\n" + wall seconds)
  local json wall; json=$(printf '%s' "$2" | sed '$d'); wall=$(printf '%s' "$2" | tail -1)
  printf '%s\n' "$json" | jq -r --arg l "$1" --arg w "$wall" '
    "[\($l)]",
    "  tokens reused from cache : \(.timings.cache_n // 0)",
    "  tokens prefilled now     : \(.timings.prompt_n)",
    "  prefill time             : \((.timings.prompt_ms/10|round)/100) s",
    "  generation               : \(.timings.predicted_n) tok @ \((.timings.predicted_per_second*10|round)/10) tok/s",
    "  wall time (request)      : \($w) s",
    "  thinking chars           : \(.choices[0].message.reasoning_content // "" | length)",
    "  answer: \(.choices[0].message.content // "" | gsub("\n";" ") | .[0:300])"'
}

SHA=$("$LM" sha "$FILE")
echo "file: $FILE"
echo "sha256: $SHA"
hr
echo "[load] llamark.sh load $SHA"
t0=$(date +%s)
LLAMARK_URL=$URL "$LM" load "$SHA" "$FILE" "$SLOT"
echo "  load wall time: $(( $(date +%s) - t0 )) s"
report "warm: question on restored cache" "$(ask true)"
hr

if [ $COLD = 1 ]; then
  curl -sf -X POST "$URL/slots/$SLOT?action=erase" >/dev/null
  echo "[cold] slot erased, prefilling the file again…"
  report "cold: same question, no cache" "$(ask false)"
  hr
fi
