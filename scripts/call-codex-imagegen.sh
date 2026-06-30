#!/usr/bin/env bash
# call-codex-imagegen.sh — Image generation bridge via Codex CLI (gpt-image-2)
#
# Calls Codex CLI's built-in image_gen tool to generate images, then extracts
# the file path for downstream pipelines (sprite processing, map compositing,
# or any workflow that needs generated images).
#
# Designed for use with Claude Code (via prism/skill), other AI agents, CI, or
# standalone. Requires: Codex CLI (npm i -g @openai/codex) + OPENAI_API_KEY.
#
# Usage:
#   call-codex-imagegen.sh "pixel art warrior on solid #FF00FF magenta, 2x2 grid"
#   call-codex-imagegen.sh -o ./assets/warrior.png "warrior sprite sheet"
#   call-codex-imagegen.sh -i ref.png "match this style, create idle animation"
#   call-codex-imagegen.sh --size 1024x1024 "forest tilemap"
#   call-codex-imagegen.sh --dry-run "test prompt"
#
# Exit codes:
#   0   success (image path printed to stdout)
#   1   usage / argument error
#   2   image not found after generation
#   124 soft timeout
#   *   codex CLI error (passthrough)

set -euo pipefail

# ============================================================================
# Defaults
# ============================================================================
MODEL="${CODEX_MODEL:-}"
SANDBOX="workspace-write"
OUTPUT=""
REF_IMAGES=()
SIZE=""
QUALITY=""
BG=""
SKIP_GIT_CHECK=false
DRY_RUN=false
LOG_DIR="${MULTI_AI_LOG_DIR:-$HOME/.claude/logs}"
mkdir -p "$LOG_DIR"

# image gen is slower than text Q&A — default 5 min
TIMEOUT_S="${CLAUDE_PRISM_TIMEOUT:-300}"

# ============================================================================
# Log rotation (from call-codex.sh architecture)
# ============================================================================
LOG_FILE="$LOG_DIR/multi-ai-$(date -u +%Y-%m).log"
LOG_LATEST="$LOG_DIR/multi-ai.log"
if [[ -e "$LOG_LATEST" && ! -L "$LOG_LATEST" ]]; then
    mv -n "$LOG_LATEST" "$LOG_DIR/multi-ai-archive-pre-rotation.log" 2>/dev/null || true
fi
ln -sf "$(basename "$LOG_FILE")" "$LOG_LATEST" 2>/dev/null || true

# ============================================================================
# Observability
# ============================================================================
START_TS=$(date +%s)
MAIN_PID=$$
CALLER="${CLAUDE_PRISM_CALLER:-unknown}"
CWD=$(pwd | tr -d '\n"')
CC_VER="${AI_AGENT:-unknown}"

_log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [codex-imagegen] [$level] [pid=$$] $*" >> "$LOG_FILE"
}

STAGE="entry"
_log_signal() { _log WARN "signal SIG$1 stage=$STAGE"; }
trap '_log_signal HUP' HUP
trap '_log_signal INT; exit 130' INT
trap '_log_signal TERM; exit 143' TERM
_log INFO "invoke ppid=$PPID caller=\"$CALLER\" cwd=\"$CWD\" cc_ver=\"$CC_VER\""

# ============================================================================
# Parse flags
# ============================================================================
STAGE="parse_flags"

_usage() {
    cat >&2 <<'USAGE'
Usage: call-codex-imagegen.sh [OPTIONS] "image prompt"

Options:
  -o, --output <path>     Save generated image to this path
  -i, --image <file>      Reference image (repeatable)
  -m, --model <model>     Codex model override
  --size <WxH>            Image size (e.g. 1024x1024, 1536x864)
  --quality <level>       Quality: low | medium | high | auto
  --bg <color>            Background color hint (e.g. "#FF00FF")
  --sandbox <mode>        Sandbox mode (default: workspace-write)
  --dry-run               Show command without calling API
  --timeout <seconds>     Soft timeout (default: 300)
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            [[ $# -ge 2 ]] || { echo "Error: -o requires a path" >&2; exit 1; }
            OUTPUT="$2"; shift 2 ;;
        -i|--image)
            [[ $# -ge 2 ]] || { echo "Error: -i requires a file" >&2; exit 1; }
            [[ -f "$2" ]] || { echo "Error: reference image not found: $2" >&2; exit 1; }
            REF_IMAGES+=("$2"); shift 2 ;;
        -m|--model)
            [[ $# -ge 2 ]] || { echo "Error: -m requires a model name" >&2; exit 1; }
            MODEL="$2"; shift 2 ;;
        --size)
            [[ $# -ge 2 ]] || { echo "Error: --size requires dimensions" >&2; exit 1; }
            SIZE="$2"; shift 2 ;;
        --quality)
            [[ $# -ge 2 ]] || { echo "Error: --quality requires a level" >&2; exit 1; }
            QUALITY="$2"; shift 2 ;;
        --bg)
            [[ $# -ge 2 ]] || { echo "Error: --bg requires a color" >&2; exit 1; }
            BG="$2"; shift 2 ;;
        --sandbox)
            [[ $# -ge 2 ]] || { echo "Error: --sandbox requires a mode" >&2; exit 1; }
            case "$2" in
                read-only|workspace-write|danger-full-access) : ;;
                *) echo "Error: invalid sandbox mode '$2'" >&2; exit 1 ;;
            esac
            SANDBOX="$2"; shift 2 ;;
        --timeout)
            [[ $# -ge 2 ]] || { echo "Error: --timeout requires seconds" >&2; exit 1; }
            TIMEOUT_S="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=true; shift ;;
        -h|--help)
            _usage ;;
        *)  break ;;
    esac
done

ART_PROMPT="${1:-}"
[[ -z "$ART_PROMPT" ]] && { echo "Error: image prompt required" >&2; _usage; }

# ============================================================================
# Build meta-prompt for Codex
# ============================================================================
STAGE="build_prompt"

# Resolve output path to absolute (parent dir may not exist yet)
if [[ -n "$OUTPUT" ]]; then
    case "$OUTPUT" in
        /*) OUTPUT_ABS="$OUTPUT" ;;
        *)  OUTPUT_ABS="$(pwd)/$OUTPUT" ;;
    esac
    # Normalize /./ and //
    OUTPUT_ABS=$(printf '%s' "$OUTPUT_ABS" | sed 's|/\./|/|g; s|//|/|g')
else
    OUTPUT_ABS=""
fi

SPECS=""
[[ -n "$SIZE" ]]    && SPECS="${SPECS}
- Image size: $SIZE"
[[ -n "$QUALITY" ]] && SPECS="${SPECS}
- Quality: $QUALITY"
[[ -n "$BG" ]]      && SPECS="${SPECS}
- Background: solid $BG fill, no gradients"

if [[ -n "$OUTPUT_ABS" ]]; then
    SAVE_INSTRUCTION="After generation:
1. Find the generated image file under the generated_images directory.
2. Create parent directories if needed: mkdir -p $(dirname "$OUTPUT_ABS")
3. Copy the generated image to: $OUTPUT_ABS
4. On a line by itself, print exactly: IMAGE_PATH:$OUTPUT_ABS"
else
    SAVE_INSTRUCTION="After generation:
1. Find the generated image file under the generated_images directory.
2. On a line by itself, print exactly: IMAGE_PATH:<absolute path to the generated image>"
fi

META_PROMPT="Use your built-in image_gen tool to generate exactly ONE image.

Do NOT use code, canvas, SVG, HTML, or any other method to create the image.
Use ONLY the built-in image_gen tool.

IMAGE PROMPT:
$ART_PROMPT
${SPECS}

$SAVE_INSTRUCTION

Print nothing after the IMAGE_PATH line."

_log INFO "art_prompt_len=${#ART_PROMPT} output=$OUTPUT ref_images=${#REF_IMAGES[@]} size=$SIZE"

# ============================================================================
# Git repo check
# ============================================================================
STAGE="git_check"
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    _log WARN "not inside a git repo — passing --skip-git-repo-check"
    SKIP_GIT_CHECK=true
fi

# ============================================================================
# Dry run
# ============================================================================
STAGE="dry_run"
if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY RUN] Meta-prompt (${#META_PROMPT} chars):"
    echo "$META_PROMPT"
    echo ""
    echo "[DRY RUN] Command: codex exec --sandbox $SANDBOX${MODEL:+ --model $MODEL}${REF_IMAGES:+ -i ...}"
    _log INFO "dry run complete"
    exit 0
fi

# ============================================================================
# Resolve codex binary
# ============================================================================
STAGE="binary_resolve"
CODEX_BIN="${CODEX_BIN:-}"
if [[ -z "$CODEX_BIN" ]]; then
    for candidate in \
        "$HOME/.npm-global/bin/codex" \
        "$(command -v codex 2>/dev/null || true)" \
        "/usr/local/bin/codex"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            CODEX_BIN="$candidate"
            break
        fi
    done
fi

if [[ -z "$CODEX_BIN" ]]; then
    _log ERROR "codex CLI not found"
    echo "Error: CLI_NOT_FOUND: Codex CLI not installed. Install: npm i -g @openai/codex" >&2
    exit 1
fi

# ============================================================================
# Execute Codex
# ============================================================================
STAGE="exec"
CMD=("$CODEX_BIN" exec --sandbox "$SANDBOX")
[[ "$SKIP_GIT_CHECK" == true ]] && CMD+=(--skip-git-repo-check)
[[ -n "$MODEL" ]] && CMD+=(--model "$MODEL")
for img in "${REF_IMAGES[@]}"; do
    CMD+=(-i "$img")
done

ERR_TMP=$(mktemp)
OUT_TMP="${CLAUDE_PRISM_OUT_TMP:-$(mktemp "${LOG_DIR}/pi-codex-imagegen-last-XXXXXX")}"

if [[ -f "$OUT_TMP" ]]; then
    : > "$OUT_TMP" 2>/dev/null || true
fi

# Validate timeout
if ! [[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || (( TIMEOUT_S > 3600 )); then
    _log WARN "invalid timeout=$TIMEOUT_S — falling back to 300"
    TIMEOUT_S=300
fi

TIMEOUT_MARKER=$(mktemp "${TMPDIR:-/tmp}/codex-imagegen-timeout.XXXXXX")

printf '%s' "$META_PROMPT" | "${CMD[@]}" - 2>"$ERR_TMP" | tee "$OUT_TMP" &
LAST=$!

# Heartbeat: log progress every 30s
(
    while sleep 30; do
        kill -0 "$LAST" 2>/dev/null || break
        bytes=$([ -f "$OUT_TMP" ] && wc -c < "$OUT_TMP" 2>/dev/null | tr -d ' \n' || echo 0)
        elapsed=$(($(date +%s) - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [codex-imagegen] [DEBUG] [pid=$MAIN_PID] alive elapsed_s=$elapsed bytes=$bytes" >> "$LOG_FILE"
    done
) &
HBPID=$!

# Soft timeout watcher
(
    sleep "$TIMEOUT_S"
    if kill -0 "$LAST" 2>/dev/null; then
        echo "$TIMEOUT_S" > "$TIMEOUT_MARKER"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [codex-imagegen] [WARN] [pid=$$] soft_timeout elapsed_s=$TIMEOUT_S" >> "$LOG_FILE"
        pkill -TERM -P $$ 2>/dev/null || true
    fi
) &
WPID=$!

trap 'kill "$WPID" "$HBPID" 2>/dev/null || true; pkill -KILL -P $$ 2>/dev/null || true; rm -f "$ERR_TMP" "$TIMEOUT_MARKER"' EXIT

set +e
wait "$LAST" 2>/dev/null
rc=$?
set -e

# Symlink for fallback reads
if [[ -z "${CLAUDE_PRISM_OUT_TMP:-}" ]]; then
    ln -sf "$(basename "$OUT_TMP")" "${LOG_DIR}/pi-codex-imagegen-last.out" || true
fi

# ============================================================================
# Soft timeout check
# ============================================================================
if { [[ $rc -eq 143 ]] || [[ $rc -eq 137 ]]; } && [[ -s "$TIMEOUT_MARKER" ]]; then
    out_bytes=$([ -f "$OUT_TMP" ] && wc -c < "$OUT_TMP" 2>/dev/null | tr -d ' \n' || echo 0)
    echo "[CODEX-IMAGEGEN: soft-timeout after ${TIMEOUT_S}s]" >&2
    _log ERROR "soft_timeout after ${TIMEOUT_S}s output_bytes=$out_bytes"
    exit 124
fi

# ============================================================================
# Error handling
# ============================================================================
if [[ $rc -ne 0 ]]; then
    err_text=$(cat "$ERR_TMP")
    err_lower=$(printf '%s' "$err_text" | tr '[:upper:]' '[:lower:]')
    if [[ $rc -eq 137 || $rc -eq 143 ]]; then
        diag="TIMEOUT: Codex CLI killed (signal $((rc - 128)))."
    elif [[ "$err_lower" =~ 429|rate.limit|quota|capacity ]]; then
        diag="RATE_LIMIT: OpenAI rate limit. Try again later."
    elif [[ "$err_lower" =~ auth|token|api.key|credential|403 ]]; then
        diag="AUTH_ERROR: Check OPENAI_API_KEY."
    elif [[ "$err_lower" =~ network|connect|econnrefused|etimedout|dns ]]; then
        diag="NETWORK: Cannot reach OpenAI API."
    elif [[ "$err_lower" =~ sandbox|permission ]]; then
        diag="SANDBOX: Need workspace-write. Use --sandbox workspace-write."
    else
        diag="CLI_ERROR: Codex exited with code $rc."
    fi
    err_text_safe=$(printf '%s' "$err_text" | tr '\n' ' ')
    _log ERROR "$diag: $err_text_safe"
    echo "Error: $diag" >&2
    [[ -n "$err_text" ]] && echo "Details: $err_text" >&2
    exit $rc
fi

# ============================================================================
# Extract image path
# ============================================================================
STAGE="extract_path"

IMAGE_PATH=""

# Strategy 1: parse IMAGE_PATH: marker from output
if [[ -f "$OUT_TMP" ]]; then
    IMAGE_PATH=$(grep -m1 "IMAGE_PATH:" "$OUT_TMP" 2>/dev/null | sed 's/^.*IMAGE_PATH:[[:space:]]*//' | tr -d '\r' || true)
fi

# Strategy 2: if marker not found, look for common image path patterns
if [[ -z "$IMAGE_PATH" || ! -f "$IMAGE_PATH" ]]; then
    CANDIDATE=$(grep -oE '(/[^ "]+\.(png|jpg|jpeg|webp))' "$OUT_TMP" 2>/dev/null | tail -1 || true)
    if [[ -n "$CANDIDATE" && -f "$CANDIDATE" ]]; then
        IMAGE_PATH="$CANDIDATE"
    fi
fi

# Strategy 3: if output path was specified, check if Codex already copied it
if [[ -z "$IMAGE_PATH" || ! -f "$IMAGE_PATH" ]] && [[ -n "$OUTPUT_ABS" && -f "$OUTPUT_ABS" ]]; then
    IMAGE_PATH="$OUTPUT_ABS"
fi

# Strategy 4: find most recent image in ~/.codex or workspace
if [[ -z "$IMAGE_PATH" || ! -f "$IMAGE_PATH" ]]; then
    CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
    CANDIDATE=$(find "$CODEX_HOME" -name "*.png" -newer "$ERR_TMP" -type f 2>/dev/null | head -1 || true)
    if [[ -n "$CANDIDATE" && -f "$CANDIDATE" ]]; then
        IMAGE_PATH="$CANDIDATE"
        if [[ -n "$OUTPUT_ABS" ]]; then
            mkdir -p "$(dirname "$OUTPUT_ABS")"
            cp "$CANDIDATE" "$OUTPUT_ABS"
            IMAGE_PATH="$OUTPUT_ABS"
        fi
    fi
fi

if [[ -z "$IMAGE_PATH" || ! -f "$IMAGE_PATH" ]]; then
    _log ERROR "image not found after generation"
    echo "Error: IMAGE_NOT_FOUND: Codex ran successfully but generated image could not be located." >&2
    echo "Codex output:" >&2
    cat "$OUT_TMP" >&2
    exit 2
fi

# ============================================================================
# Success
# ============================================================================
STAGE="done"
FILE_SIZE=$(wc -c < "$IMAGE_PATH" | tr -d ' ')
_log INFO "success image=$IMAGE_PATH size_bytes=$FILE_SIZE elapsed_s=$(($(date +%s) - START_TS))"
echo "$IMAGE_PATH"
