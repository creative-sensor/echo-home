#!/bin/bash
# llamark.sh — per-file KV-cache store for llama-server.
#
# One .bin per memory file, named by the file's sha256:
#   ~/echo-home/memory/llamark/<model_name>/<sha256>.bin
# The server must run with --slot-save-path pointing at that <model_name> dir.
#
# Commands:
#   llamark.sh load  <sha256> [file] [slot]  restore <sha256>.bin into slot (default 0);
#                                             if missing and <file> given, build it first
#   llamark.sh build <file> [slot]           prefill <file> as the system turn, save <sha256>.bin
#   llamark.sh ls                             list cached bins for the server's model
#   llamark.sh sha   <file>                   print the file's sha256 (portable macOS/Linux)
#   llamark.sh debug [sha256|file] [slot]     print step-by-step commands to test restore by hand
#
# Contract with the chat client: the first message must be
#   {"role":"system","content": <exact bytes of the file>}
# sent with "id_slot": <slot> and "cache_prompt": true — then the cached prefix is reused.
#
# Env: LLAMARK_URL (default http://localhost:8080), LLAMARK_HOME (default ~/echo-home/memory/llamark)
#      LLAMARK_DIR  exact bin dir (= the server's --slot-save-path); overrides HOME/<model_name>
#      If no model name comes back from $LLAMARK_URL/props, bins go in $LLAMARK_HOME/.bin
set -euo pipefail
# Never read the caller's stdin (e.g. a parent's protocol pipe): on Git Bash a stray read
# there blocks until a newline arrives. Only explicit pipes below (`... | curl -d @-`) feed stdin.
exec </dev/null
curl() { command curl --connect-timeout "${LLAMARK_CONNECT_TIMEOUT:-5}" "$@"; }  # fail fast if server is down

URL=${LLAMARK_URL:-http://localhost:8080}
HOME_DIR=${LLAMARK_HOME:-$HOME/echo-home/memory/llamark}

die() { echo "llamark: $*" >&2; exit 1; }
sha_of() {  # portable sha256: GNU sha256sum (Linux), shasum (macOS), openssl fallback
  local h
  [ -f "$1" ] || die "no such file: $1"
  if command -v sha256sum >/dev/null 2>&1; then h=$(sha256sum -b -- "$1")
  elif command -v shasum >/dev/null 2>&1;  then h=$(shasum -a 256 -b -- "$1")
  elif command -v openssl >/dev/null 2>&1; then h=$(openssl dgst -sha256 -r "$1")
  else die "need sha256sum, shasum or openssl"; fi
  h=${h#\\}; h=${h%% *}; h=$(printf '%s' "$h" | tr 'A-F' 'a-f')   # GNU prefixes '\' for odd names
  [[ $h =~ ^[0-9a-f]{64}$ ]] || die "bad sha256 for $1: '$h'"
  echo "$h"
}
model_dir() {
  if [ -n "${LLAMARK_DIR:-}" ]; then echo "$LLAMARK_DIR"; return; fi
  local m; m=$(curl -sf "$URL/props" 2>/dev/null | jq -r '.model_path // empty' 2>/dev/null) || m=
  m=${m##*/}; m=${m%.gguf}; m=${m%.GGUF}
  if [ -z "$m" ]; then
    echo "llamark: no model name from $URL/props — using $HOME_DIR/.bin" >&2
    echo "$HOME_DIR/.bin"; return
  fi
  echo "$HOME_DIR/$m"
}
slot_call() {  # action slot [filename] — on failure show the server's error body, return 1
  local out code
  out=$(curl -s -w '\n%{http_code}' -X POST "$URL/slots/$2?action=$1" -H 'Content-Type: application/json' \
        -d "{\"filename\":\"${3:-}\"}") || { echo "llamark: server not reachable at $URL" >&2; return 1; }
  code=${out##*$'\n'}; out=${out%$'\n'*}
  if [ "$code" != 200 ]; then
    echo "llamark: slot $1 ${3:-} failed (HTTP $code): $(printf '%s' "$out" | jq -r '.error.message // .' 2>/dev/null || echo "$out")" >&2
    echo "llamark: check --slot-save-path == $(model_dir), and that --ctx-size/--parallel, -ctk/-ctv and -fa match the build" >&2
    return 1
  fi
  printf '%s\n' "$out"
}

build() {  # file slot
  local file=$1 slot=${2:-0} sha dir prompt
  [ -f "$file" ] || die "no such file: $file"
  sha=$(sha_of "$file"); dir=$(model_dir); mkdir -p "$dir"
  # Render via the model's chat template so tokens match /v1/chat/completions exactly.
  prompt=$(jq -n --rawfile c "$file" '{messages:[{role:"system",content:$c}], add_generation_prompt:false}' \
    | curl -sf "$URL/apply-template" -H 'Content-Type: application/json' -d @- | jq -r .prompt)
  slot_call erase "$slot" >/dev/null || exit 1
  echo "build: prefilling $(basename "$file") into slot $slot" >&2
  jq -n --arg p "$prompt" --argjson s "$slot" '{prompt:$p, n_predict:0, cache_prompt:true, id_slot:$s}' \
    | curl -sf "$URL/completion" -H 'Content-Type: application/json' -d @- \
    | jq -c '{tokens:.tokens_evaluated, prefill_s:((.timings.prompt_ms/10|round)/100)}' >&2
  { slot_call save "$slot" "$sha.bin" || exit 1; } | jq -c '{saved:.filename, tokens:.n_saved, bytes:.n_written}' >&2
  echo "$sha"
}

load() {  # sha [file] [slot]
  local sha file=${2:-} slot=${3:-0} dir out
  sha=$(printf '%s' "$1" | tr 'A-F' 'a-f'); sha=${sha%.bin}
  [[ $sha =~ ^[0-9a-f]{64}$ ]] || die "not a sha256: $1"
  [ -z "$file" ] || [ "$(sha_of "$file")" = "$sha" ] || die "sha256 of $file does not match $sha"
  # Always ask the server first. The bins live in the *server's* --slot-save-path; when
  # llama-server runs on another host that dir is invisible here, so a local `-f` check
  # would always miss and rebuild (overwriting the good bin on the server) every time.
  # slot_call writes nothing to stderr on success, so 2>&1 only captures errors.
  if out=$(slot_call restore "$slot" "$sha.bin" 2>&1); then
    printf '%s\n' "$out" | jq -c '{restored:.filename, tokens:.n_restored, restore_s:((.timings.restore_ms/10|round)/100)}' >&2
    return
  fi
  if [ -z "$file" ]; then
    printf '%s\n' "$out" >&2
    die "restore of $sha.bin failed; pass the file to (re)build it (debug: $0 debug $sha $slot)"
  fi
  case $out in
    *"not reachable"*) printf '%s\n' "$out" >&2; exit 1 ;;   # server down: building would fail too
  esac
  dir=$(model_dir 2>/dev/null)
  if [ -f "$dir/$sha.bin" ]; then   # only meaningful when the server is local
    echo "llamark: restore rejected (bin from another machine/config?) — rebuilding" >&2
    printf '%s\n' "$out" | head -1 >&2
    echo "llamark: to investigate: $0 debug $file $slot" >&2
  else
    echo "llamark: $sha.bin not restorable on server (first use?) — building" >&2
  fi
  build "$file" "$slot" >/dev/null
}

debug() {  # [sha256|file] [slot] — print copy-paste commands to check restore by hand (runs nothing)
  local arg=${1:-} slot=${2:-0} sha="<sha256>" file="<file>" dir bin
  if [ -f "$arg" ]; then file=$arg; sha=$(sha_of "$arg")
  elif [ -n "$arg" ]; then sha=$(printf '%s' "${arg%.bin}" | tr 'A-F' 'a-f'); fi
  dir=$(model_dir 2>/dev/null); bin="$dir/$sha.bin"
  cat <<EOF
# llamark restore debug — copy/paste each step (nothing below was run)
# URL=$URL  bin dir=$dir  slot=$slot

# 1. tools present? (Linux needs sha256sum, curl, jq, od)
command -v sha256sum shasum curl jq od

# 2. sha of the memory file == bin name?
sha256sum -b '$file'        # expect: $sha
ls -l '$bin'

# 3. bin header: magic 'qsgg' + version + saved token count
od -An -c  -N4 '$bin'       # expect: q s g g
od -An -tu4 -j4 -N8 '$bin'  # -> <version> <n_tokens>

# 4. server: model + context per slot (must be >= n_tokens from step 3)
curl -s '$URL/props' | jq '{model_path, total_slots, n_ctx: .default_generation_settings.n_ctx}'
curl -s '$URL/slots' | jq '.[] | {id, n_ctx, is_processing}'   # 501 => server started with --no-slots

# 5. server must have been started with this exact save path (check its command line)
ps -eo args | grep '[l]lama-server' | tr ' ' '\n' | grep -A1 -- '--slot-save-path'   # expect: $dir

# 6. raw restore — shows HTTP code + server error message
curl -s -w '\nHTTP %{http_code}\n' -X POST '$URL/slots/$slot?action=restore' \\
     -H 'Content-Type: application/json' -d '{"filename":"$sha.bin"}'
#    200 + n_restored>0 => restore works
#    400 "Invalid filename"            => name has '/', '..' or odd chars
#    500 "Unable to restore slot..."   => file missing in --slot-save-path, or n_tokens > slot n_ctx,
#                                         or KV config differs from build (-ctk/-ctv, -fa, model file)
#    server log line: grep -i 'restore\|state' in llama-server output

# 7. confirm the restored prefix is reused (cache_n should be ~n_tokens)
jq -n --rawfile s '$file' --argjson sl $slot \\
  '{messages:[{role:"system",content:\$s},{role:"user",content:"hi"}],id_slot:\$sl,cache_prompt:true,max_tokens:8}' \\
  | curl -s '$URL/v1/chat/completions' -H 'Content-Type: application/json' -d @- \\
  | jq '.timings | {cache_n, prompt_n}'

# 8. if still rejected: rebuild on this machine and retry step 6
$0 build '$file' $slot
EOF
}

case "${1:-}" in
  debug) debug "${2:-}" "${3:-0}" ;;
  load) [ $# -ge 2 ] || die "usage: load <sha256> [file] [slot]"; load "$2" "${3:-}" "${4:-0}" ;;
  build) [ $# -ge 2 ] || die "usage: build <file> [slot]"; build "$2" "${3:-0}" ;;
  sha)   [ $# -ge 2 ] || die "usage: sha <file>"; sha_of "$2" ;;
  ls)    d=$(model_dir)
         ls -lh "$d"/*.bin 2>/dev/null || echo "(none in $d — if llama-server is on another host, list its --slot-save-path there)" ;;
  *)     sed -n '2,20p' "$0"; exit 1 ;;
esac
