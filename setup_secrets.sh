#!/usr/bin/env bash
#
# setup_secrets.sh — create age identity and Fernet key for an environment.
#
# Usage:   ./setup_secrets.sh laptop   (or prod)
#
# Creates keys under ~/.dpl-keys/<env>/, prints the config values to add
# to pass_mgr.<env>.yaml. Never overwrites existing keys.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR/bin"

fail() {
    echo ""
    echo "SETUP FAILED: $1"
    echo ""
    exit 1
}

# --- Step 1: argument ------------------------------------------------------
if [ $# -ne 1 ]; then
    fail "Usage: ./setup_secrets.sh <laptop|prod>"
fi
ENV="$1"
case "$ENV" in
    laptop|prod) ;;
    *) fail "Environment must be 'laptop' or 'prod', got '$ENV'" ;;
esac

# --- Step 2: paths (defaults, or custom with confirmation) ------------------
DEFAULT_AGE="$HOME/.dpl-keys/$ENV/sops-age/age.key"
DEFAULT_FERNET="$HOME/.dpl-keys/$ENV/fernet/key"

echo ""
echo "=== pass_mgr key setup — environment: $ENV ==="
echo ""
echo "Default key locations:"
echo "  Age key:    $DEFAULT_AGE"
echo "  Fernet key: $DEFAULT_FERNET"
echo ""
read -p "Accept defaults? (y/n) " ACCEPT_DEFAULTS

if [ "$ACCEPT_DEFAULTS" = "y" ]; then
    AGE_PATH="$DEFAULT_AGE"
    FERNET_PATH="$DEFAULT_FERNET"
else
    echo ""
    read -p "Age key path    [Enter = default]: " AGE_INPUT
    read -p "Fernet key path [Enter = default]: " FERNET_INPUT
    AGE_PATH="${AGE_INPUT:-$DEFAULT_AGE}"
    FERNET_PATH="${FERNET_INPUT:-$DEFAULT_FERNET}"
    echo ""
    echo "Using custom locations:"
    echo "  Age key:    $AGE_PATH"
    echo "  Fernet key: $FERNET_PATH"
    read -p "Continue? (y/n) " CONT
    if [ "$CONT" != "y" ]; then
        echo ""; echo "Setup aborted."; exit 0
    fi
fi

# --- Step 3: locate age-keygen ----------------------------------------------
KGEN_BIN=""
for kf in "$BIN_DIR"/age-keygen-*; do
    if [ -f "$kf" ]; then
        KGEN_BIN="$kf"
        break
    fi
done
if [ -z "$KGEN_BIN" ]; then
    fail "No age-keygen binary found in bin/ — run ./run_offline_tests.sh first to verify binaries"
fi

# --- Step 4: secure directories ----------------------------------------------
mkdir -p "$HOME/.dpl-keys"
chmod 700 "$HOME/.dpl-keys"
mkdir -p "$(dirname "$AGE_PATH")" "$(dirname "$FERNET_PATH")"
chmod 700 "$(dirname "$AGE_PATH")" "$(dirname "$FERNET_PATH")"

# --- Step 5: mint age identity (refuse overwrite) ---------------------------
if [ -f "$AGE_PATH" ]; then
    echo ""
    echo "Age key already exists at $AGE_PATH — skipping (protected)."
    echo "Public key: $(grep -o 'age1[a-z0-9]*' "$AGE_PATH" | head -1)"
else
    echo ""
    echo "Minting age identity..."
    OUTPUT=$("$KGEN_BIN" -o "$AGE_PATH" 2>&1)
    if [ $? -ne 0 ]; then
        fail "age-keygen failed: $OUTPUT"
    fi
    echo "$OUTPUT"
fi

# --- Step 6: generate Fernet key (refuse overwrite) --------------------------
if [ -f "$FERNET_PATH" ]; then
    echo ""
    echo "Fernet key already exists at $FERNET_PATH — skipping (protected)."
else
    echo "Generating Fernet key..."
    # Prefer the ACTIVE environment's Python (conda etc.), so the
    # 'cryptography' package resolves to the same one the tests use.
    PYTHON_BIN="${CONDA_PREFIX:+$CONDA_PREFIX/bin/python}"
    PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
    "$PYTHON_BIN" -c "from cryptography.fernet import Fernet; open('$FERNET_PATH','wb').write(Fernet.generate_key())" \
        || fail "Fernet key generation failed (activate the 'dpl' environment first: conda activate dpl)"
fi

# --- Step 7: permissions + report --------------------------------------------
chmod 600 "$AGE_PATH" "$FERNET_PATH"

echo ""
echo "=============================================="
echo "  Setup complete for '$ENV'."
echo ""
echo "  Put these values in config/pass_mgr.$ENV.yaml:"
echo ""
echo "    age_key_file: $AGE_PATH"
echo "    key_file:     $FERNET_PATH"
echo ""
echo "  Then run: ./run_offline_tests.sh"
echo "=============================================="
echo ""
exit 0