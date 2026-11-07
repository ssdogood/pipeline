# DPL (Data Pipeline)

Internal data extraction pipeline for pulling data from Oracle and Active Directory into staging, then loading to Parquet and DuckLake.

## Environment Setup

```bash
# 1. Build and activate the environment from the shipped definition
#    (environment.yml provides the INTERPRETER only; pyproject.toml owns
#    every Python package - the single-owner rule)
conda env create -f environment.yml
conda activate dpl

# 2. Register dpl AND all its dependencies into it (editable - a
#    DEVELOPER setup; a bundle receiver instead follows README-FIRST.txt
#    inside the unpacked bundle: pip install ".[test]")
python -m pip install -e ".[dev]"

# 3. Point DPL_HOME at the project root
export DPL_HOME=/path/to/your/pipeline

# 4. Optional: write logs to a different folder
export DPL_LOG_DIR=/path/to/your/logs

# 5. Optional: place the settings configuration in a different folder
export DPL_SETTINGS=/path/to/your/config

# 6. Optional but recommended: put lines 1-3 (and 4/5 if used) in your
#    shell profile so every new terminal is ready.
```
Add to ~/.zshrc or ~/.bashrc for persistence. Note: cron jobs must set this explicitly — they don't inherit your shell profile.

## Documentation

| Document | Purpose |
|---|---|
| `docs/Pass Manager Operations Guide.md` | Vault/password operations, environment setup (Step 0), known issues |
| `docs/Release Procedure.md` | How releases are built, self-certified, and published |
| `tests/integration/README.md` | Live-server integration tests (Oracle, LDAP) |
| `README-FIRST.txt` (inside release bundles) | Receiver instructions — not in the repo; fabricated at build time |

## Running
### Wrapper entry point

Main orchestrator. Handles profile selection, manifest writing, and history tracking:
```bash
# Full run (all tables)
dpl.wrapper.run --profile full

# Single extractor profile
dpl.wrapper.run --profile ad

# Manual run ID
dpl.wrapper.run --run-id manual_$(date +%Y%m%d_%H%M%S)
```

## Update history manually
After recovering failed runs or reconciling manifests:
```bash
dpl.wrapper.update-history --reconcile
```

## Extractors (direct)
Oracle extraction (usually called from wrapper):
```bash
python -m dpl.extractors.oracle_to_stg
```

Active Directory extraction:
```bash
python -m dpl.extractors.ad_to_stg
```

## Schema regeneration
Regenerate schema_master_auto.csv after config changes:
```bash
python -m dpl.lib.generate_schemas --force
```

Use --tables TABLE_A,TABLE_B to regenerate specific tables only.

## Project Structure
```
pipeline
├── bin
│   └── sops_manifest.txt               # Pinned SOPS/Age checksums + download URLs
├── build
├── config                              # Runtime configuration
│   ├── extractors                      # Extractor specific configurations
│   │   └── backups
│   ├── logging.yaml                    # Logging configuration
│   ├── pass_mgr.laptop.yaml            # Password configuration - Dev
│   ├── pass_mgr.prod.yaml              # Password configuration - Prod
│   ├── run.yaml                        # Orchestrator configuration
│   ├── settings.laptop.yaml            # Application level settings
│   └── wrapper.yaml                    # Wrapper profiles configurations
├── data
│   ├── agg                             # gold: aggregates/marts
│   ├── cur                             # silver: typed, deduped, conformed
│   ├── raw                             # bronze: ingested as-is
│   ├── scratch                         # transient writer files, cleaned per run
│   └── staging                         # landing zone: <date>/<folder>/ parquet extracts
│       ├── _history                    # history is maintained in SQLite database
│       └── _runs                       # manifest files are maintained
├── docs                                # Documentation
├── logs                                # Logs of execution
├── scripts                             # Build and vault scripts
├── secrets                             # Encrypted password vaults
│   ├── backups
│   └── logs
├── src
│   ├── dpl
│   │   ├── extractors                  # Extraction modules
│   │   ├── lib                         # Shared utilities
│   │   └── wrapper                     # Orchestration
├── tests
│   ├── integration                     # Integration tests (live servers; deselect by default)
│   └── unit                            # Offline unit tests
├── environment.yml
├── LICENSE
├── pyproject.toml
├── run_offline_tests.bat               # Certification (test-kit side; not in production bundles)
├── run_offline_tests.sh
├── README.md
├── setup_secrets.bat
├── setup_secrets.sh
├── vault.bat
└── vault.sh
```

## Configuration Files
### oracle_to_stg.yaml / ad_to_stg.yaml
Per-extractor config. Defines connections, tables to extract, and folder destinations. Folder names must be unique across all connections — validation fails at startup if violated.

### schema_master_*.csv
Single source of truth for column definitions. Regenerate with generate_schemas --force after changing config. Tables in schema master must match config.yaml exactly.

### table_rules_*.csv
Curated deduplication and sort rules. Per-table settings for:
* Dedup columns (all, none, or specific columns)
* Sort order (column_name- for descending)

### settings.*.yaml
Global runtime settings: batch sizes, compression, memory limits, and path roots. Machine-specific variants (laptop/prod) override defaults.

### wrapper.yaml
Wrapper orchestration config: profiles, manifest retention, history DB path.

### logging.yaml
Log format, level, and file handler configuration. One centralized config in lib/logger.py.

## Exit Codes
Wrapper returns exit codes for cron integration:
|Code|	Meaning|
|----|---------|
|0|	Success, all tables extracted|
|1|	Some extractions failed|
|2|	SQLite history DB failure|
|3|	Config error (invalid YAML, missing schema)|

## Testing

Offline test suite (self-contained, no real connections):
```bash
pytest tests/
# or the certified green run:
run_offline_tests.sh    # macOS / Linux
run_offline_tests.bat   # Windows
```

Receivers of a release bundle certify via the companion test kit —
see README-FIRST.txt inside the unpacked bundle (the bundle itself
ships no test code, by design).

### Known Constraints
* Production deployments use the release bundle + test-kit certification flow — see `docs/Release Procedure.md`
* Manifests retained 90 days by default — adjust in wrapper.yaml
* Logs rotate at 5MB max, keep 5 backups — configure in logging.yaml
* Paths configured in settings yaml. For server, use settings.server.yaml
* Older/pre-AVX2 CPUs: polars aborts (0xc000001d) — install `polars-lts-cpu` once per machine; see `docs/Pass Manager Operations Guide.md`, Known Issues

## Contact
You can contact me on silencesdogood at pm dot me.

#### End Note
I only designed the application and tested it. The code is written by AI including the yaml files.
