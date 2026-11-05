"""Demo / verification: retrieve secrets through the public interface.
Callers know nothing about methods, paths, keys, or file formats."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dpl" / "lib"))

import pass_mgr


def mask(value: str) -> str:
    """Show enough to prove retrieval, not enough to leak on screen."""
    return value[:4] + "*" * max(len(value) - 4, 4)


# One line of setup. No paths, no method, no key - the environment
# config decides everything.
pass_mgr.init("laptop")

for name in ("ora_db.user_name.password",):
    print(f"{name:45s} -> {mask(pass_mgr.get_secret(name))}")