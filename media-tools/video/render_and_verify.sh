#!/usr/bin/env bash
# Portable Remotion runner and FFmpeg verification gate.
set -euo pipefail

TOLERANCE="${VIDEO_STUDIO_DURATION_TOLERANCE:-1.0}"

die() { printf 'render_and_verify: FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf 'render_and_verify: %s\n' "$*" >&2; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found on PATH: $1"
}

duration_of() {
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null
}

expected_seconds() {
  case "$1" in
    ''|*[!0-9.]*)
      [ -f "$1" ] || die "expected duration is neither a number nor a file: $1"
      duration_of "$1" || die "cannot read duration from $1"
      ;;
    *) printf '%s\n' "$1" ;;
  esac
}

duration_matches() {
  awk -v actual="$1" -v expected="$2" -v tolerance="$TOLERANCE" \
    'BEGIN { delta=actual-expected; if (delta<0) delta=-delta; exit !(delta<=tolerance) }'
}

decode_clean() {
  local errors
  errors="$(mktemp)"
  if ! ffmpeg -hide_banner -nostats -v error -i "$1" -f null - 2>"$errors"; then
    sed 's/^/    /' "$errors" >&2
    rm -f "$errors"
    return 1
  fi
  if [ -s "$errors" ]; then
    sed 's/^/    /' "$errors" >&2
    rm -f "$errors"
    return 1
  fi
  rm -f "$errors"
}

gate() {
  local media="$1" expected="$2" actual
  [ -f "$media" ] || { log "media not found: $media"; return 1; }
  actual="$(duration_of "$media")" || {
    log "unreadable media container: $media"
    return 1
  }
  [ -n "$actual" ] || { log "media duration is empty: $media"; return 1; }
  duration_matches "$actual" "$expected" || {
    log "duration ${actual}s differs from expected ${expected}s by more than ${TOLERANCE}s"
    return 1
  }
  decode_clean "$media" || {
    log "full decode failed: $media"
    return 1
  }
  log "verified $media (${actual}s)"
}

cover_gate() {
  local image="$1" dimensions
  [ -f "$image" ] || { log "cover not found: $image"; return 1; }
  dimensions="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0 "$image" 2>/dev/null)" || return 1
  [ "$dimensions" = "1280,720" ] || {
    log "cover is $dimensions; expected 1280,720"
    return 1
  }
  ffmpeg -hide_banner -nostats -v error -i "$image" -f null - >/dev/null 2>&1
}

loudness_gate() {
  local media="$1" target="${2:--14}" tolerance="${3:-1.0}" summary measured
  [ -f "$media" ] || return 1
  summary="$(ffmpeg -hide_banner -nostats -i "$media" \
    -af loudnorm=print_format=summary -f null - 2>&1)" || return 1
  measured="$(printf '%s\n' "$summary" | awk -F: \
    '/Input Integrated/{gsub(/[^0-9.+-]/,"",$2); print $2; exit}')"
  [ -n "$measured" ] || return 1
  awk -v value="$measured" -v target="$target" -v tolerance="$tolerance" \
    'BEGIN { delta=value-target; if (delta<0) delta=-delta; exit !(delta<=tolerance) }'
}

require_command ffmpeg
require_command ffprobe

case "${1:-}" in
  --verify-only)
    [ "$#" -eq 3 ] || die "usage: $0 --verify-only <media> <expected-seconds-or-media>"
    gate "$2" "$(expected_seconds "$3")"
    exit
    ;;
  --cover-gate-only)
    [ "$#" -eq 2 ] || die "usage: $0 --cover-gate-only <cover.png>"
    cover_gate "$2"
    exit
    ;;
  --loudness-gate-only)
    [ "$#" -ge 2 ] && [ "$#" -le 4 ] || die "usage: $0 --loudness-gate-only <media> [target] [tolerance]"
    loudness_gate "$2" "${3:--14}" "${4:-1.0}"
    exit
    ;;
esac

[ "$#" -ge 4 ] || die "usage: $0 <project> <composition> <output.mp4> <expected> [concurrency] [flags]"
project="$1"
composition="$2"
output="$3"
expected="$(expected_seconds "$4")"
concurrency="${5:-3}"
shift "$(( $# >= 5 ? 5 : 4 ))"

skip_pronunciation=0
narration=""
narration_text=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-pronunciation-gate) skip_pronunciation=1; shift ;;
    --narration)
      [ "$#" -ge 2 ] || die "--narration needs a path"
      narration="$2"; shift 2
      ;;
    --narration-text)
      [ "$#" -ge 2 ] || die "--narration-text needs a path"
      narration_text="$2"; shift 2
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -d "$project" ] || die "project directory not found: $project"
case "$concurrency" in ''|*[!0-9]*) die "concurrency must be a positive integer" ;; esac
[ "$concurrency" -ge 1 ] || die "concurrency must be a positive integer"

if [ "$skip_pronunciation" -eq 0 ]; then
  [ -n "$narration" ] || die "pass --narration or explicitly --skip-pronunciation-gate"
  [ -f "$narration" ] || die "narration not found: $narration"
  [ -n "$narration_text" ] || narration_text="$(dirname "$narration")/narration.txt"
  [ -f "$narration_text" ] || die "narration text not found: $narration_text"
  verifier="$(cd "$(dirname "$0")/../narration" && pwd)/verify_pronunciation.py"
  python3 "$verifier" --audio "$narration" --text-file "$narration_text" \
    --json-out "$(dirname "$narration")/pronunciation-gate-report.json" ||
    die "pronunciation verification failed"
fi

require_command npx
project="$(cd "$project" && pwd)"
case "$output" in
  /*) ;;
  *) output="$(pwd)/$output" ;;
esac
mkdir -p "$(dirname "$output")"
raw="${output%.mp4}.candidate.mp4"
remux="${output%.mp4}.remux-candidate.mp4"
trap 'rm -f "$raw" "$remux"' EXIT INT TERM

entry=""
for candidate in src/index.ts src/index.tsx index.ts remotion/index.ts; do
  if [ -f "$project/$candidate" ]; then
    entry="$candidate"
    break
  fi
done

attempt="$concurrency"
while [ "$attempt" -ge 1 ]; do
  rm -f "$raw" "$remux"
  command=(npx --no-install remotion render)
  [ -z "$entry" ] || command+=("$entry")
  command+=("$composition" "$raw" "--concurrency=$attempt")
  log "rendering composition $composition at concurrency $attempt"
  (
    cd "$project"
    "${command[@]}"
  ) || true
  if [ -f "$raw" ] && gate "$raw" "$expected"; then
    [ ! -e "$output" ] || die "refusing to overwrite existing output: $output"
    mv "$raw" "$output"
    trap - EXIT INT TERM
    log "render complete: $output"
    exit 0
  fi
  if [ -f "$raw" ] && ffmpeg -y -hide_banner -v error -i "$raw" +      -c copy -movflags +faststart "$remux" 2>/dev/null &&
      gate "$remux" "$expected"; then
    [ ! -e "$output" ] || die "refusing to overwrite existing output: $output"
    mv "$remux" "$output"
    rm -f "$raw"
    trap - EXIT INT TERM
    log "render complete after container remux: $output"
    exit 0
  fi
  attempt="$((attempt - 1))"
done

die "render failed verification at every concurrency level"
