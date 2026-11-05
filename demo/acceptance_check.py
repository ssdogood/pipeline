import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dpl" / "lib"))

import pass_mgr
from pass_mgr._providers import FernetProvider
from pass_mgr._config import load_config

cfg = load_config("laptop")
section = cfg.method_sections["fernet"]
provider = FernetProvider(section, cfg)
vault = provider.load()

print(f"Fernet vault entries: {sorted(vault.keys())}")
assert vault, "vault is empty - scan first"
print("ACCEPTANCE PASS: tool wrote it, library reads it.")