# tests/unit/test_environment.py
"""PT-040 Phase 1: selector + resolver tests.

Covers every loud-refusal rule of resolve_environment() plus the
happy path, the banner, and the committed selector file itself.
Uses a scratch config tree - never the real config/.
"""

from pathlib import Path

import pytest

from dpl.lib.environment import (
    ResolvedEnvironment,
    format_banner,
    resolve_environment,
)

# ---------------------------------------------------------------- fixtures

def _make_tree(tmp_path: Path, selector: str | None = "environment: development",
               folders: tuple[str, ...] = ("development",)) -> Path:
    """Scratch config tree under tmp_path."""
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    if selector is not None:
        (cfg / "environment.yaml").write_text(selector, encoding="utf-8")
    for name in folders:
        (cfg / name).mkdir(exist_ok=True)
    return cfg


# ---------------------------------------------------------------- happy path

def test_resolve_happy_path(tmp_path):
    cfg = _make_tree(tmp_path)
    env = resolve_environment(config_dir=cfg)
    assert isinstance(env, ResolvedEnvironment)
    assert env.name == "development"
    assert env.folder == cfg / "development"


def test_each_valid_environment_resolves(tmp_path):
    for name in ("development", "test", "production"):
        cfg = _make_tree(tmp_path, selector=f"environment: {name}",
                         folders=(name,))
        env = resolve_environment(config_dir=cfg)
        assert env.name == name
        assert env.folder == cfg / name


def test_value_with_surrounding_whitespace_is_accepted_and_trimmed(tmp_path):
    cfg = _make_tree(tmp_path, selector="environment: ' development '",
                     folders=("development",))
    assert resolve_environment(config_dir=cfg).name == "development"


# ---------------------------------------------------------------- rule 1: file missing

def test_missing_selector_refuses_with_guidance(tmp_path):
    cfg = _make_tree(tmp_path, selector=None)
    with pytest.raises(ValueError) as ei:
        resolve_environment(config_dir=cfg)
    msg = str(ei.value)
    assert "environment.yaml not found" in msg
    assert "environment: development" in msg          # shows the fix
    assert "config/_templates/" in msg                 # points at the skeleton


# ---------------------------------------------------------------- rule 2: invalid value

@pytest.mark.parametrize("bad", [
    "dev", "prod", "Dev", "DEVELOPMENT", "Testing", "staging", "",
    "example", "_templates", "_example", "laptop",
])
def test_invalid_names_refuse_naming_the_vocabulary(tmp_path, bad):
    cfg = _make_tree(tmp_path, selector=f"environment: '{bad}'",
                     folders=("development",))
    with pytest.raises(ValueError) as ei:
        resolve_environment(config_dir=cfg)
    msg = str(ei.value)
    # empty value takes the earlier 'must be one of' branch; every other
    # refusal lands on the 'is invalid' branch naming the vocabulary
    assert ("is invalid" in msg) or ("must be one of" in msg)
    for word in ("development", "test", "production"):
        assert word in msg                            # vocabulary always named
    if bad in ("example", "_templates"):
        assert "never selectable" in msg              # template trap named


def test_lowercase_full_words_only_by_design(tmp_path):
    # 'DEVELOPMENT' must fail even though the folder exists - exact match rule
    cfg = _make_tree(tmp_path, selector="environment: DEVELOPMENT",
                     folders=("development",))
    with pytest.raises(ValueError, match="is invalid"):
        resolve_environment(config_dir=cfg)


# ---------------------------------------------------------------- rule 3: extra keys

def test_extra_keys_refuse_single_declaration_rule(tmp_path):
    cfg = _make_tree(tmp_path,
                     selector="environment: development\ntier: production")
    with pytest.raises(ValueError) as ei:
        resolve_environment(config_dir=cfg)
    msg = str(ei.value)
    assert "accepts only the key 'environment'" in msg
    assert "tier" in msg
    assert "exactly one place" in msg


def test_non_mapping_selector_refuses(tmp_path):
    cfg = _make_tree(tmp_path, selector="- development")
    with pytest.raises(ValueError, match="must be a mapping"):
        resolve_environment(config_dir=cfg)


def test_invalid_yaml_refuses(tmp_path):
    cfg = _make_tree(tmp_path, selector="environment: [unclosed")
    with pytest.raises(ValueError, match="invalid YAML"):
        resolve_environment(config_dir=cfg)


def test_empty_value_refuses(tmp_path):
    cfg = _make_tree(tmp_path, selector="environment:")
    with pytest.raises(ValueError, match="must be one of"):
        resolve_environment(config_dir=cfg)


# ---------------------------------------------------------------- rule 5: folder missing

def test_missing_env_folder_refuses_pointing_at_template(tmp_path):
    cfg = _make_tree(tmp_path, selector="environment: test",
                     folders=("development",))  # test/ absent
    with pytest.raises(ValueError) as ei:
        resolve_environment(config_dir=cfg)
    msg = str(ei.value)
    assert "does not exist on this machine" in msg
    assert "config/_templates/" in msg


# ---------------------------------------------------------------- banner

def test_banner_names_environment_and_folder(tmp_path):
    cfg = _make_tree(tmp_path)
    env = resolve_environment(config_dir=cfg)
    banner = format_banner(env)
    assert "RUNNING ENVIRONMENT:" in banner
    assert "development" in banner
    assert str(cfg / "development") in banner


# ---------------------------------------------------------------- committed selector sanity

def test_committed_selector_exists_and_is_parseable():
    """The real config/environment.yaml must exist and declare a valid
    vocabulary word (folder existence checked separately per machine)."""
    from dpl.lib.environment import SELECTOR_FILENAME, VALID_ENVIRONMENTS
    from dpl.lib.paths import get_project_root

    selector = get_project_root() / "config" / SELECTOR_FILENAME
    assert selector.is_file(), f"committed selector missing: {selector}"

    import yaml
    raw = yaml.safe_load(selector.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert set(raw) == {"environment"}
    # The VALUE is machine-specific and may name a folder absent in a
    # checkout elsewhere - only vocabulary membership is asserted here.
    assert raw["environment"] in VALID_ENVIRONMENTS
