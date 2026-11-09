from dpl.lib.pass_mgr._config import load_config
from dpl.lib.pass_mgr._providers import FernetProvider

cfg = load_config("laptop")
section = cfg.method_sections["fernet"]
provider = FernetProvider(section, cfg)
vault = provider.load()

print(f"Fernet vault entries: {sorted(vault.keys())}")
assert vault, "vault is empty - scan first"
print("ACCEPTANCE PASS: tool wrote it, library reads it.")