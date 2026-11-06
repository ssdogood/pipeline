# DPL (Data Pipeline)

Internal data extraction pipeline for pulling data from Oracle and Active Directory into staging, then loading to Parquet and DuckLake.

## Environment Setup

```bash
conda create --name dpl python=3.11
conda activate dpl
pip install -e .
```

Or use the provided environment.yml:
```bash
conda env create -f environment.yml
conda activate dpl
```

Required: Set DPL_HOME in your shell:
```bash
export DPL_HOME="/path/to/pipeline"
```

Add to ~/.zshrc or ~/.bashrc for persistence. Note: cron jobs must set this explicitly — they don't inherit your shell profile.
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
```bash
pipeline
├── config                                                  # Runtime configuration
│   ├── extractors                                          # Extractor specific configurations
│   │   ├── backups                                         # Backup of schemas
│   │   ├── oracle_to_stg.yaml
│   │   ├── schema_master_oracle_to_stg.csv
│   │   └── table_rules_oracle_to_stg.csv
│   ├── logging.yaml
│   ├── settings.laptop.yaml
│   └── wrapper.yaml
├── data
│   ├── lakehouse.db                                        # DuckLake catalog (attaches to the layers)
│   ├── agg                                                 # bronze: ingested as-is
│   ├── cur                                                 # silver: typed, deduped, conformed
│   ├── raw                                                 # gold: aggregates/marts
│   ├── scratch                                             # transient writer files, cleaned per run
│   └── staging                                             # landing zone: <date>/<folder>/ parquet extracts
│       ├── _history                                        # history is maintained in SQLite database
│       └── _runs                                           # manifest files are maintained
├── docs                                                    # Documentation
├── logs                                                    # Logs of execution
├── secrets                                                 # DB credentials (db.ora.sec)
├── src
│   ├── dpl
│   │   ├── extractors                                      # Extraction modules
│   │   │   └── oracle_to_stg.py
│   │   ├── lib                   # Shared utilities
│   │   │   ├── common.py
│   │   │   ├── generate_schemas.py
│   │   │   ├── logger.py
│   │   │   ├── ora_connect.py
│   │   │   ├── paths.py
│   │   │   └── write_parquet.py
│   │   └── wrapper                                         # Orchestration
│   │       ├── profiles.py
│   │       ├── run.py
│   │       └── update_history.py
└── test                                                    # All tests
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
Smoke tests (self-contained, no real connections):
```bash
python -m dpl.lib.common
```

Unit tests:
```bash
pytest tests/
```

### Known Constraints
* Production server has no Git — changes deployed via tarball backup
* Secrets never committed — db.ora.sec excluded via .gitignore
* Manifests retained 90 days by default — adjust in wrapper.yaml
* Logs rotate at 10MB max, keep 5 backups — configure in logging.yaml
* Paths configured in settings yaml. For server, use settings.server.yaml

## Contact
You can contact me on silencesdogood at pm dot me.

#### End Note
I only designed the application and tested it. The code is written by AI including the yaml files. 
