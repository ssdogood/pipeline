#!/usr/bin/env bash
#
# vault.sh — in-memory vault operations for pass_mgr.
#
# Usage:
#   ./vault.sh list  <env>              # print secret names (never values)
#   ./vault.sh scan  <env> [--dry-run]  # sync vault to match config files
#
# Policies enforced:
#   - decrypted plaintext exists only in memory, never written to disk
#   - values are never echoed, printed, or logged
#   - atomic replacement of the vault file
#   - verification round-trip before the old vault is replaced

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CANDIDATES=()
if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
    CANDIDATES+=("$CONDA_PREFIX/bin/python")
fi
if command -v pytest >/dev/null 2>&1; then
    CANDIDATES+=("$(dirname "$(command -v pytest)")/python")
fi
CANDIDATES+=("$(command -v python3)")

PYTHON_BIN=""
for cand in "${CANDIDATES[@]}"; do
    if [ -x "$cand" ] && "$cand" -c "import yaml" 2>/dev/null; then
        PYTHON_BIN="$cand"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo ""
    echo "VAULT ERROR: no Python found with PyYAML installed."
    echo "Fix:  conda install -n dpl pyyaml"
    echo "Then re-run this command."
    echo ""
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/scripts/vault_ops.py" "$@"