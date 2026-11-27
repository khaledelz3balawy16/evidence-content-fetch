#!/usr/bin/env bash
# Nightly Aptem -> Neon sync for the Linux VPS.
#
# Started at 00:00 by the systemd timer in deploy/systemd/ (see
# backend/docs/vps-nightly-sync.md). Safe to run by hand at any time:
#
#   ./scripts/nightly_sync.sh
#
# Steps (each one runs even if the previous one failed; exit code is non-zero
# if any step failed):
#   1. run_modern_reviews_cycle --apply   capture Aptem (read-only) -> verified
#                                         local stage -> per-learner apply to Neon
#   2. process_source_evidence            download + extract + analyse new evidence
#
# Tunables (environment, e.g. via `sudo systemctl edit aptem-nightly.service`):
#   NIGHTLY_WORKERS=3            capture workers (1-4)
#   NIGHTLY_VERIFY_UI=0          1 = also pass --verify-ui to the capture
#   NIGHTLY_RUN_EVIDENCE=1       0 = skip step 2
#   NIGHTLY_EVIDENCE_LIMIT=0     0 = process every pending evidence item
#   NIGHTLY_LOG_KEEP_DAYS=60     delete nightly_*.log older than this
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
WORKERS="${NIGHTLY_WORKERS:-3}"
VERIFY_UI="${NIGHTLY_VERIFY_UI:-0}"
RUN_EVIDENCE="${NIGHTLY_RUN_EVIDENCE:-1}"
EVIDENCE_LIMIT="${NIGHTLY_EVIDENCE_LIMIT:-0}"
LOG_DIR="$ROOT/logs"
LOG_KEEP_DAYS="${NIGHTLY_LOG_KEEP_DAYS:-60}"

mkdir -p "$LOG_DIR"

# Single instance. Two overlapping cycles race on the same learners (the apply
# step's advisory locks / "older inventory" checks then fail one of them).
exec 9>"$LOG_DIR/.nightly.lock"
if ! flock -n 9; then
    echo "Another nightly sync is still running; skipping this start."
    exit 0
fi

LOG="$LOG_DIR/nightly_$(date -u +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

if [ ! -x "$PYTHON" ]; then
    log "Python not found at $PYTHON (run deploy/install_vps.sh first)"
    exit 2
fi

cd "$BACKEND"
status=0

run_step() {
    local name="$1"; shift
    log "START $name"
    local started=$SECONDS
    "$@"
    local rc=$?
    if [ $rc -eq 0 ]; then
        log "OK    $name ($((SECONDS - started))s)"
    else
        log "FAIL  $name exit=$rc ($((SECONDS - started))s)"
        status=1
    fi
}

log "Nightly sync starting (host $(hostname), log $LOG)"

cycle_args=(--workers "$WORKERS" --apply)
[ "$VERIFY_UI" = "1" ] && cycle_args+=(--verify-ui)
run_step "modern reviews cycle" "$PYTHON" -u manage.py run_modern_reviews_cycle "${cycle_args[@]}"

if [ "$RUN_EVIDENCE" = "1" ]; then
    if [ "$EVIDENCE_LIMIT" -gt 0 ] 2>/dev/null; then
        evidence_args=(--limit "$EVIDENCE_LIMIT")
    else
        evidence_args=(--all)
    fi
    run_step "evidence download + analysis" "$PYTHON" -u manage.py process_source_evidence "${evidence_args[@]}"
else
    log "SKIP  evidence download + analysis (NIGHTLY_RUN_EVIDENCE=$RUN_EVIDENCE)"
fi

find "$LOG_DIR" -maxdepth 1 -name 'nightly_*.log' -mtime +"$LOG_KEEP_DAYS" -delete 2>/dev/null || true

log "Nightly sync finished with status $status"
exit $status
