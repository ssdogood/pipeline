#!/usr/bin/env bash
#
# setup_secrets.sh - create age identity and Fernet key for the ACTIVE
#                    environment (PT-046 addendum: selector-only).
#
# Usage:   ./setup_secrets.sh            (no arguments - the environment
#                                         is discovered from
#                                         config/environment.yaml)
#
# Creates keys under ~/.dpl/keys/<env>/, never overwrites existing keys,
# and prints the config values to add to <env-folder>/pass_mgr.yaml.
#
# First-day sequence (fresh machine):
#   1. unzip/untar the shipment (or git clone/pull the bundle)
#   2. mint the env folder:   cp -r config/_templates config/<env>
#   3. fill values in the new folder (settings, pass_mgr, ...)
#   4. declare the environment in config/environment.yaml
#   5. run this script          <- keys are minted for THAT environment
#   6. ./vault.sh scan          <- capture and seal secrets
#   7. run the gate, then the first real run

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR/bin"
CONFIG_DIR="$SCRIPT_DIR/config"
SELECTOR="$CONFIG_DIR/environment.yaml"

fail() {
    echo ""
    echo "SETUP FAILED: $1"
    echo ""
    exit 1
}

# --- Step 1: the selector - the ONE declaration -----------------------------
# The environment is never an argument. It is discovered from
# config/environment.yaml, validated against the fixed vocabulary,
# and the env folder must already exist (minted from _templates/).
if [ $# -ne 0 ]; then
    fail "This script takes NO arguments. The environment is discovered from $SELECTOR (one place only). Got: $*"
fi

if [ ! -f "$SELECTOR" ]; then
    fail "$SELECTOR not found. The environment MUST be declared there before keys can be minted (one place only: environment: development|test|production)"
fi

ENV="$(grep -E '^[[:space:]]*environment:' "$SELECTOR" | head -1 | sed 's/^[[:space:]]*environment:[[:space:]]*//;s/[[:space:]]*$//' | tr -d '\"'"'"'')"
if [ -z "$ENV" ]; then
    fail "$SELECTOR: missing or empty 'environment' key (one place only: environment: development|test|production)"
fi

case "$ENV" in
    development|test|production) ;;
    *)
        fail "$SELECTOR names environment '$ENV' - must be one of: development|test|production (the folder is not selectable by any other name)"
        ;;
esac

ENV_DIR="$CONFIG_DIR/$ENV"
if [ ! -d "$ENV_DIR" ]; then
    fail "$SELECTOR names environment '$ENV' but folder $ENV_DIR is missing. First-day sequence: cp -r config/_templates $ENV_DIR, fill the values, then re-run this script."
fi

if [ ! -f "$ENV_DIR/pass_mgr.yaml" ]; then
    fail "$ENV_DIR/pass_mgr.yaml is missing - the env folder was minted but not filled. Copy the pass_mgr skeleton from _templates, fill the values, then re-run."
fi

KEYS_ROOT="$HOME/.dpl/keys"
ENV_KEY_DIR="$KEYS_ROOT/$ENV"
DEFAULT_AGE="$ENV_KEY_DIR/sops-age/age.key"
DEFAULT_FERNET="$ENV_KEY_DIR/fernet/key"

echo ""
echo "=== pass_mgr key setup - environment: $ENV (folder: $ENV_DIR) ==="
echo ""

# --- Step 2: key paths (defaults, or custom with confirmation) --------------
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
KERNEL="$(uname -s)"
case "$KERNEL" in
    Darwin*)
        KGEN_PATTERN="age-keygen-*darwin*" ;;
    Linux*)
        KGEN_PATTERN="age-keygen-*linux*" ;;
    MINGW*|MSYS*|CYGWIN*)
        KGEN_PATTERN="age-keygen-*amd64.exe" ;;
    *)
        fail "unrecognized platform '$KERNEL' for age-keygen selection" ;;
esac

KGEN_BIN=""
for kf in "$BIN_DIR"/$KGEN_PATTERN; do
    if [ -f "$kf" ]; then
        KGEN_BIN="$kf"
        break
    fi
done
if [ -z "$KGEN_BIN" ]; then
    fail "No age-keygen binary found in bin/ - run ./run_offline_tests.sh first to verify binaries"
fi

# --- Step 4: secure directories ----------------------------------------------
mkdir -p "$HOME/.dpl"
chmod 700 "$HOME/.dpl"
mkdir -p "$KEYS_ROOT" "$(dirname "$AGE_PATH")" "$(dirname "$FERNET_PATH")"
chmod 700 "$KEYS_ROOT" "$(dirname "$AGE_PATH")" "$(dirname "$FERNET_PATH")"

# --- Step 5: mint age identity (refuse overwrite) ---------------------------
if [ -f "$AGE_PATH" ]; then
    echo ""
    echo "Age key already exists at $AGE_PATH - skipping (protected)."
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
    echo "Fernet key already exists at $FERNET_PATH - skipping (protected)."
else
    echo "Generating Fernet key..."
    # Prefer the ACTIVE conda environment's Python, so the
    # 'cryptography' package resolves to the same one the tests use.
    PYTHON_BIN="${CONDA_PREFIX:+$CONDA_PREFIX/bin/python}"
    PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
    "$PYTHON_BIN" -c "from cryptography.fernet import Fernet; open('$FERNET_PATH','wb').write(Fernet.generate_key())" \
        || fail "Fernet key generation failed (activate the 'dpl' environment first: conda activate dpl)"
fi

# --- Step 6b: generate stg_keys master key (PT-048) ---------------------------
# The staging-key bin has its OWN master key - independent blast radius
# from the password vault (design 2.1). Same ceremony, separate lock.
# Refuses overwrite like every other key in this script.
STG_KEYS_DIR="$ENV_KEY_DIR/stg_keys"
STG_MASTER="$STG_KEYS_DIR/key"

if [ -f "$STG_MASTER" ]; then
    echo ""
    echo "stg_keys master already exists at $STG_MASTER - skipping (protected)."
else
    mkdir -p "$STG_KEYS_DIR"
    chmod 700 "$STG_KEYS_DIR"
    echo "Generating stg_keys master key..."
    "$PYTHON_BIN" -c "from cryptography.fernet import Fernet; open('$STG_MASTER','wb').write(Fernet.generate_key())" \
        || fail "stg_keys master generation failed (activate the 'dpl' environment first: conda activate dpl)"
fi
chmod 600 "$STG_MASTER" 2>/dev/null || true

# --- Step 7: permissions + report --------------------------------------------
chmod 600 "$AGE_PATH" "$FERNET_PATH"

echo ""
echo "=============================================="
echo "  Setup complete for '$ENV'."
echo ""
echo "  Put these values in $ENV_DIR/pass_mgr.yaml:"
echo ""
echo "    age_key_file: ~/.dpl/keys/$ENV/sops-age/age.key"
echo "    key_file:     ~/.dpl/keys/$ENV/fernet/key"
echo ""
echo "  (custom locations: use the paths you chose, in tilde form)"
echo ""
echo "  stg_keys bin master (PT-048) was also ensured at:"
echo "    ~/.dpl/keys/$ENV/stg_keys/key"
echo "  (no config entry needed - the bin locates it automatically)"
echo ""
echo "  Then run: ./run_offline_tests.sh"
echo "=============================================="
echo ""
exit 0
