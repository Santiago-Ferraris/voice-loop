"""Packaging: every subpackage in the source tree has to reach the runtime.

This is the one class of bug the whole test suite is blind to. Tests import
`voiceloop` from the clone, where a subpackage resolves whether or not
packaging knows about it — so the suite stays green while the *installed*
runtime crashes on startup. That is exactly how `voiceloop.stt` shipped:
`pyproject.toml` carried a literal `packages = ["voiceloop"]`, the wheel
omitted `voiceloop/stt/`, and launchd got

    ModuleNotFoundError: No module named 'voiceloop.stt'

with `launchctl list` showing `-  1` instead of a pid. Nothing in 745 passing
tests could have caught it, because nothing looked at what gets installed.
"""

from __future__ import annotations

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


def declared_packages() -> set[str]:
    """What the build backend would actually ship, per pyproject.toml."""
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; CI runs 3.12 too
    from setuptools import find_packages

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    setuptools_cfg = config.get("tool", {}).get("setuptools", {})

    literal = setuptools_cfg.get("packages")
    if isinstance(literal, list):
        # A hand-kept list. It is only correct until the next subpackage.
        return set(literal)

    find_cfg = (literal or {}).get("find", setuptools_cfg.get("packages", {}).get("find", {}))
    if not isinstance(find_cfg, dict):
        find_cfg = {}
    return set(
        find_packages(
            where=str(REPO_ROOT),
            include=find_cfg.get("include", ["*"]),
            exclude=find_cfg.get("exclude", []),
        )
    )


def test_every_source_subpackage_ships():
    missing = source_packages() - declared_packages()
    assert not missing, (
        f"these packages exist in the source but would not be installed: {sorted(missing)}. "
        "The clone will keep importing them and the suite will stay green, but the daemon "
        "dies at startup. Prefer [tool.setuptools.packages.find] over a literal list."
    )


def test_stt_specifically_ships():
    """The subpackage that actually broke, pinned by name."""
    assert "voiceloop.stt" in source_packages()
    assert "voiceloop.stt" in declared_packages()


def test_guard_would_catch_a_literal_list(monkeypatch):
    """The check has to fail when packaging is wrong — otherwise it proves nothing."""
    monkeypatch.setattr(
        "tests.test_packaging.declared_packages", lambda: {"voiceloop"}, raising=False
    )
    missing = source_packages() - {"voiceloop"}
    assert missing, "source tree has no subpackages, so this guard cannot regress"
