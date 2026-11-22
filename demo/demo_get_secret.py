"""Demo / verification: retrieve secrets through the public interface.
Callers know nothing about methods, paths, keys, or file formats."""
from dpl.lib import pass_mgr


def mask(value: str) -> str:
    """Show enough to prove retrieval, not enough to leak on screen."""
    return value[:4] + "*" * max(len(value) - 4, 4)


# One line of setup. No paths, no method, no key - the ACTIVE
# ENVIRONMENT FOLDER (config/environment.yaml) decides everything.
# PT-040 4a: the vault config is <env-folder>/pass_mgr.yaml.
from dpl.lib.environment import resolve_environment

pass_mgr.init(resolve_environment().folder / "pass_mgr.yaml")

for name in ("ora_db.user_name.password",):
    print(f"{name:45s} -> {mask(pass_mgr.get_secret(name))}")