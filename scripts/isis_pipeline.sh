#!/usr/bin/env bash
#
# isis_pipeline.sh — radiometrically calibrate Hayabusa2 ONC-W2 frames and export PNGs.
#
# RUNS INSIDE THE CI ISIS CONTAINER ONLY. It is never executed on the local
# machine (ISIS is not installed there). Locally you can only:
#     bash -n scripts/isis_pipeline.sh                       # syntax check
#     bash scripts/isis_pipeline.sh --dry-run IN OUT         # print the command plan
#
# Pipeline per frame:  <id>.fit
#     hyb2onc2isis  ->  <id>.cub
#     spiceinit     (local kernels, fall back to the SPICE web service)
#     hyb2onccal    ->  <id>.cal.cub        (bias/dark/flat/smear + radiometric)
#     isis2std      ->  OUT/<id>.png        (0.5–99.5% linear stretch, 8-bit)
#
# Usage:
#     isis_pipeline.sh [--dry-run] [--keep-intermediates] INPUT_DIR OUTPUT_DIR
#
# Everything is logged verbosely (this is the only view into the run) and a copy
# is written to ./pipeline.log. Individual frame failures do not stop the batch;
# the script exits non-zero if any frame failed.

set -Eeuo pipefail

DRY_RUN=0
KEEP_INTERMEDIATES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --keep-intermediates) KEEP_INTERMEDIATES=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        --) shift; break ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done

if [[ $# -ne 2 ]]; then
    echo "usage: $0 [--dry-run] [--keep-intermediates] INPUT_DIR OUTPUT_DIR" >&2
    exit 2
fi

INPUT_DIR="${1%/}"
OUTPUT_DIR="${2%/}"
WORK_DIR="${WORK_DIR:-work}"
LOG_FILE="${LOG_FILE:-pipeline.log}"

# Mirror all output to pipeline.log from here on.
exec > >(tee -a "$LOG_FILE") 2>&1

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }
banner() { echo; echo "======== $* ========"; }

run() {
    # Echo the command, then run it (unless --dry-run).
    echo "+ $*"
    if [[ $DRY_RUN -eq 0 ]]; then
        "$@"
    fi
}

banner "ENVIRONMENT"
log "pipeline start (dry-run=${DRY_RUN})"
log "input : ${INPUT_DIR}"
log "output: ${OUTPUT_DIR}"
log "work  : ${WORK_DIR}"
echo "ISISROOT=${ISISROOT:-<unset>}"
echo "ISISDATA=${ISISDATA:-<unset>}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FINALIZE="${SCRIPT_DIR}/finalize_png.py"

if [[ $DRY_RUN -eq 0 ]]; then
    for bin in hyb2onc2isis spiceinit hyb2onccal isis2raw getkey python3; do
        command -v "$bin" >/dev/null 2>&1 || {
            log "FATAL: '$bin' not on PATH — this script must run inside the ISIS container"
            exit 3
        }
    done
    echo "--- isis version ---";      cat "${ISISROOT:-/dev/null}/version" 2>/dev/null || echo "(no version file)"
    echo "--- python ---";            python3 --version
    echo "--- ISISDATA top level ---"; ls -la "${ISISDATA:-/dev/null}" 2>/dev/null || true
    echo "--- ISISDATA/hayabusa2 ---"; ls -la "${ISISDATA:-/dev/null}/hayabusa2" 2>/dev/null || true
    echo "--- disk usage ---";        df -h . "${ISISDATA:-/}" 2>/dev/null || true
    echo "--- ISISDATA sizes ---";    du -sh "${ISISDATA:-/dev/null}"/* 2>/dev/null || true
    [[ -f "$FINALIZE" ]] || { log "FATAL: finalize script not found at $FINALIZE"; exit 3; }
fi

mkdir -p "$OUTPUT_DIR" "$WORK_DIR"

banner "DISCOVER INPUT"
shopt -s nullglob
FITS=( "${INPUT_DIR}"/*_w2f_l2a.fit "${INPUT_DIR}"/*_w2f_l2a.FIT )
shopt -u nullglob
if [[ ${#FITS[@]} -eq 0 ]]; then
    log "FATAL: no *_w2f_l2a.fit files in ${INPUT_DIR}"
    exit 4
fi
log "found ${#FITS[@]} frame(s):"
printf '  %s\n' "${FITS[@]}"

process_frame() {
    local fit="$1"
    local id cub cal raw png
    id="$(basename "$fit")"; id="${id%.*}"
    cub="${WORK_DIR}/${id}.cub"
    cal="${WORK_DIR}/${id}.cal.cub"
    raw="${WORK_DIR}/${id}.cal.raw"
    png="${OUTPUT_DIR}/${id}.png"

    banner "FRAME ${id}"

    run hyb2onc2isis from="$fit" to="$cub"

    # SPICE: try locally-installed kernels first, then the web service.
    if [[ $DRY_RUN -eq 0 ]]; then
        if spiceinit from="$cub" 2>&1; then
            log "spiceinit: local kernels OK"
        else
            log "spiceinit: local kernels failed, retrying via SPICE web service"
            run spiceinit from="$cub" web=true
        fi
    else
        echo "+ spiceinit from=$cub   (local, with web=true fallback)"
    fi

    # Bias + dark + (flat, if available) radiometric calibration. Note: ISIS
    # hyb2onccal does NOT correct readout smear (see finalize_png.py).
    run hyb2onccal from="$cub" to="$cal"

    # Hand the calibrated pixels to the pure-Python finalizer as a raw float32
    # raster: it does readout-smear correction + an asinh display stretch + PNG.
    local samples lines
    if [[ $DRY_RUN -eq 0 ]]; then
        samples="$(getkey from="$cal" grpname=Dimensions keyword=Samples recursive=true 2>/dev/null || true)"
        lines="$(getkey from="$cal" grpname=Dimensions keyword=Lines recursive=true 2>/dev/null || true)"
        if [[ ! "$samples" =~ ^[0-9]+$ || ! "$lines" =~ ^[0-9]+$ ]]; then
            log "WARN: getkey did not return dimensions (got '${samples}' x '${lines}'); assuming 1024 x 1024"
            samples=1024; lines=1024
        fi
        log "calibrated dimensions: ${samples} x ${lines}"
    else
        samples=SAMPLES; lines=LINES
    fi

    # stretch=none: pass real calibrated DN through unscaled. The default LINEAR
    # stretch clips to a 0.5-99.5 percentile window, which collapses to nothing on
    # the near-approach frames where saturated Earth dominates the histogram.
    run isis2raw from="$cal" to="$raw" bittype=32BIT stretch=none endian=lsb

    # Copy the calibrated float raster next to the PNG *before* the finalize step
    # so it is in the artifact even if finalize fails — lets the display stretch
    # be tuned offline with no CI re-run.
    if [[ $DRY_RUN -eq 0 && -s "$raw" ]]; then
        gzip -c "$raw" > "${OUTPUT_DIR}/${id}.cal.raw.gz" || true
    fi

    run python3 "$FINALIZE" "$raw" "$png" --samples "$samples" --lines "$lines"

    if [[ $DRY_RUN -eq 0 && ! -s "$png" ]]; then
        log "ERROR: expected PNG not produced: ${png}"
        return 1
    fi
    log "frame ${id} -> ${png}"

    if [[ $KEEP_INTERMEDIATES -eq 0 && $DRY_RUN -eq 0 ]]; then
        rm -f "$cub" "$cal" "$raw" "${cub}.ecub" || true
    fi
}

banner "PROCESS"
FAILED=()
for fit in "${FITS[@]}"; do
    if ! process_frame "$fit"; then
        FAILED+=( "$(basename "$fit")" )
        log "frame FAILED: $(basename "$fit") (continuing)"
    fi
done

banner "SUMMARY"
TOTAL=${#FITS[@]}
NFAIL=${#FAILED[@]}
log "processed $((TOTAL - NFAIL))/${TOTAL} frame(s) successfully"
if [[ $NFAIL -gt 0 ]]; then
    log "FAILED frames:"
    printf '  %s\n' "${FAILED[@]}"
    exit 1
fi
log "all frames OK"
