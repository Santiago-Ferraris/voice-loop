"""Packaging: every subpackage in the source tree has to reach the runtime.

This is the one class of bug the rest of the suite is blind to. Tests import
`voiceloop` from the clone, where a subpackage resolves whether or not
packaging knows about it — so the suite stays green while the *installed*
runtime crashes on startup. That is exactly how `voiceloop.stt` shipped:
`pyproject.toml` carried a literal `packages = ["voiceloop"]`, the wheel
omitted `voiceloop/stt/`, and launchd got

    ModuleNotFoundError: No module named 'voiceloop.stt'

with `launchctl list` showing `-  1` instead of a pid. Nothing in 745 passing
tests could have caught it, because nothing looked at what gets installed.

Deliberately depends on nothing outside the stdlib: `setuptools` is absent from
a modern venv, so importing it here would make this check itself unreliable.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "voiceloop"


def source_packages() -> set[str]:
    """Every importable package under voiceloop/, by dotted name."""
    found = {"voiceloop"}
    for init in SOURCE.rglob("__init__.py"):
        if init.parent == SOURCE:
            continue
        rel = init.parent.relative_to(SOURCE).as_posix().replace("/", ".")
        found.add(f"voiceloop.{rel}")
    return found


def _packaging_config() -> dict:
    tomllib = pytest.importorskip("tomllib", reason="stdlib from 3.11; CI also runs 3.12")
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return config.get("tool", {}).get("setuptools", {})


def declared_packages() -> set[str]:
    """What the build backend would actually ship, per pyproject.toml."""
    setuptools_cfg = _packaging_config()
    packages = setuptools_cfg.get("packages")

    # A hand-kept list ships exactly what it names — and nothing else, forever.
    if isinstance(packages, list):
        return set(packages)

    find_cfg = packages.get("find", {}) if isinstance(packages, dict) else {}
    include = find_cfg.get("include") or ["*"]
    exclude = find_cfg.get("exclude") or []
    return {
        name
        for name in source_packages()
        if any(fnmatch(name, pat) for pat in include)
        and not any(fnmatch(name, pat) for pat in exclude)
    }


def test_every_source_subpackage_ships():
    missing = source_packages() - declared_packages()
    assert not missing, (
        f"these packages exist in the source but would not be installed: {sorted(missing)}. "
        "The clone keeps importing them and the suite stays green, but the daemon dies at "
        "startup. Prefer [tool.setuptools.packages.find] over a literal list."
    )


def test_stt_specifically_ships():
    """The subpackage that actually broke, pinned by name."""
    assert "voiceloop.stt" in source_packages()
    assert "voiceloop.stt" in declared_packages()


def test_the_check_can_actually_fail():
    """Non-vacuous: the exact config that shipped broken must be rejected.

    Without this, a check that always passes would look like protection.
    """
    shipped_broken = {"voiceloop"}  # what `packages = ["voiceloop"]` yields
    assert source_packages() - shipped_broken, (
        "the source tree has no subpackages, so this guard protects nothing"
    )
    assert "voiceloop.stt" in source_packages() - shipped_broken
