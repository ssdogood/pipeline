#!/usr/bin/env bash
#
# run_offline_tests.sh - the GREEN SCRIPT.
#
# Gates machine trust for the pipeline tree. A machine's copy of the
# tree is trusted only after this script exits 0:
#
#   Step 1: verify every binary this platform needs, per bin/sops_manifest.txt
#           (checksum verification - 'verify, then trust')
#   Step 2: run the full offline test suite (zero network: pytest test/)
#
# Exits nonzero on ANY failure, with a message naming what to fix.
# Works on macOS, RHEL, and Windows Git-Bash - platform auto-detected.
#
# Usage:   ./run_offline_tests.sh          (from the project root)
# Success: "GREEN - this machine's tree is certified" + exit 0

set -u  # unset variables are errors (not -e: we handle failures ourselves
        # so we can print WHAT failed before exiting)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR/bin"
MANIFEST="$BIN_DIR/sops_manifest.txt"

fail() {
    echo ""
    echo "RED - certification FAILED: $1"
    echo "Fix the issue above and re-run ./run_offline_tests.sh"
    exit 1
}

echo "=== pass_mgr green script - machine certification ==="
echo ""

# --------------------------------------------------------------------------
# Step 0: identify the platform and pick the matching manifest lines
# --------------------------------------------------------------------------
# uname tells us who we are, but each family lies differently:
#   macOS        -> "Darwin"
#   RHEL/other   -> "Linux"
#   Git-Bash/Win -> "MINGW64_NT-10.0-..." or "MSYS_NT-..." or "CYGWIN_NT-..."

KERNEL="$(uname -s)"

case "$KERNEL" in
    Darwin*)
        # One binary family on the Mac: darwin.arm64 (Apple Silicon) -
        # and darwin.amd64 would match Intel Macs too. Match the word.
        PLATFORM_PATTERN="darwin"
        PLATFORM_LABEL="macOS"
        ;;
    Linux*)
        # RHEL server
        PLATFORM_PATTERN="linux.amd64"
        PLATFORM_LABEL="Linux (RHEL)"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        # Windows under Git-Bash. The binaries carry the .exe suffix.
        PLATFORM_PATTERN="amd64.exe"
        PLATFORM_LABEL="Windows (Git-Bash)"
        ;;
    *)
        fail "unrecognized platform '$KERNEL'. Add a case for it in this script."
        ;;
esac

echo "[1/3] Platform: $PLATFORM_LABEL (kernel: $KERNEL)"
echo ""

# --------------------------------------------------------------------------
# Step 1: checksum-verify the pinned binaries for THIS platform
# --------------------------------------------------------------------------

if [ ! -f "$MANIFEST" ]; then
    fail "bin/sops_manifest.txt not found - the manifest is committed in git; your tree is incomplete."
fi

# Build a platform-filtered manifest in a temp file rather than process
# substitution: temp files behave identically on all three shells.
CHECKFILE="$(mktemp)"
grep -E "$PLATFORM_PATTERN" "$MANIFEST" > "$CHECKFILE"

if [ ! -s "$CHECKFILE" ]; then
    rm -f "$CHECKFILE"
    fail "no manifest entries matched pattern '$PLATFORM_PATTERN'. Either bin/sops_manifest.txt lacks this platform's binaries, or the pattern is wrong."
fi

echo "[2/3] Verifying pinned binaries against sops_manifest.txt:"
( cd "$BIN_DIR" && shasum -a 256 -c "$CHECKFILE" )
STATUS=$?
rm -f "$CHECKFILE"

if [ $STATUS -ne 0 ]; then
    fail "checksum verification failed. Copy the pinned binaries (exact upstream names) per the manifest, then re-run."
fi
echo "      all binaries for this platform verified OK"
echo ""

# --------------------------------------------------------------------------
# Step 2: run the offline test suite
# --------------------------------------------------------------------------
# The suite is hermetic: tmp_path worlds, throwaway keys, zero network.
# It runs wherever pytest is on PATH - activate your dpl environment first
# (conda activate dpl) if pytest is not global.

cd "$SCRIPT_DIR"

echo "[3/3] Running offline test suite:"
pytest test/ -v
STATUS=$?

if [ $STATUS -ne 0 ]; then
    fail "test suite is RED - see failures above."
fi

# --------------------------------------------------------------------------
# Certified
# --------------------------------------------------------------------------
echo ""
echo "=================================================="
echo " GREEN - this machine's tree is certified:"
echo "   binaries checksum-verified, full offline"
echo "   suite passing, zero network used."
echo " Trust this copy for extraction work."
echo "=================================================="
exit 0
