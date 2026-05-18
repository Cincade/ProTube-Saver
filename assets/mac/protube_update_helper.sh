#!/bin/bash
# ProTube Saver in-app update helper.
# Runs detached AFTER the main app exits. Swaps the installed .app bundle
# with a freshly-downloaded version, then relaunches it.
#
# Args:
#   $1 = path to the staged new .app (extracted from the downloaded dmg/zip)
#   $2 = path to the installed .app to replace (e.g. /Applications/ProTube Saver.app)
#
# All output is logged to data/update.log inside the app's data dir so the
# user can see what happened if the update fails.

set -u

STAGED="${1:-}"
INSTALLED="${2:-}"
LOG="$HOME/Library/Application Support/ProTube Saver/data/update.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

log "----- Updater starting -----"
log "  staged    = $STAGED"
log "  installed = $INSTALLED"

if [ -z "$STAGED" ] || [ -z "$INSTALLED" ]; then
    log "ABORT: missing required args"
    exit 1
fi

# Wait for the parent app process to fully exit. 2 seconds is generous;
# WebKit + Python typically tears down in well under 1s.
sleep 2

if [ ! -d "$STAGED" ]; then
    log "ABORT: staged .app does not exist at $STAGED"
    exit 1
fi
if [ ! -f "$STAGED/Contents/MacOS/ProTube Saver" ]; then
    log "ABORT: staged .app missing MacOS binary"
    exit 1
fi

# Move the old install to Trash (recoverable) rather than rm -rf (catastrophic
# if path is wrong). If the move fails, abort — leave the old app in place
# rather than half-replace.
BACKUP=""
if [ -d "$INSTALLED" ]; then
    BACKUP="$HOME/.Trash/ProTube Saver (replaced $(date +%s)).app"
    if ! mv "$INSTALLED" "$BACKUP" 2>>"$LOG"; then
        log "ABORT: could not move old .app to Trash — user may need to install manually."
        exit 1
    fi
    log "Moved old install to: $BACKUP"
fi

# cp -R preserves bundle structure & resource forks; -p keeps timestamps.
if ! cp -Rp "$STAGED" "$INSTALLED" 2>>"$LOG"; then
    log "cp failed — attempting to restore old install from Trash."
    if [ -n "$BACKUP" ] && [ -d "$BACKUP" ]; then
        mv "$BACKUP" "$INSTALLED" 2>>"$LOG"
    fi
    exit 1
fi
log "Installed new version at: $INSTALLED"

# Strip the quarantine attribute so Gatekeeper doesn't pop the "downloaded
# from the internet" dialog on launch. The user already accepted the bundle
# once on initial install; this is just an in-place version swap.
xattr -dr com.apple.quarantine "$INSTALLED" 2>>"$LOG" || true

# Cleanup staging dir (parent of $STAGED) — defensive: only delete if it
# actually looks like our staging path, never a user folder.
STAGING_DIR="$(dirname "$STAGED")"
case "$STAGING_DIR" in
    */update_staging|*/update_staging/extracted)
        rm -rf "$STAGING_DIR" 2>>"$LOG"
        log "Cleaned staging: $STAGING_DIR"
        ;;
esac

# Launch the new version. `open` handles dock activation, LSRegister, etc.
if open "$INSTALLED" 2>>"$LOG"; then
    log "Launched new version. Done."
    exit 0
else
    log "ERROR: open failed. User must launch manually."
    exit 1
fi
