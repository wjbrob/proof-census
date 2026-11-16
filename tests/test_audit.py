"""Tests for the auditor.

The end-to-end test builds `tests/fixture`, a tiny Lean project with no Mathlib dependency
whose theorems are deliberately mixed: 3 clean, 1 `sorry`, 2 depending on a custom axiom. If
the auditor ever stops catching those, these tests fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from proof_census.lean import (
    AUDIT_LEAN,
    audit_project,
    deduplicate,
    find_projects,
    is_lean_project,
    lean_libs,
    lib_dir,
    memory_available_gb,
    modules,
    project_env,
    toolchain_libpath,
    wait_for_memory,
)

FIXTURE = Path(__file__).parent / "fixture"
HAS_LAKE = shutil.which("lake") is not None
needs_lake = pytest.mark.skipif(not HAS_LAKE, reason="lake not installed")


# --------------------------------------------------------------------------- unit tests


def test_audit_lean_ships_with_the_package():
    assert AUDIT_LEAN.is_file(), "audit.lean must be packaged alongside the module"
    assert "CollectAxioms" in AUDIT_LEAN.read_text()


def test_is_lean_project():
    assert is_lean_project(FIXTURE)
    assert not is_lean_project(FIXTURE / "Fixture")


def test_find_projects_locates_the_fixture_by_scanning():
    found = find_projects([FIXTURE.parent])
    assert FIXTURE.resolve() in found


def test_find_projects_does_not_descend_into_a_project():
    """A project inside a project is a dependency; its theorems are not yours."""
    found = find_projects([FIXTURE])
    assert found == [FIXTURE.resolve()]


def test_lean_libs_reads_lakefile_toml():
    assert lean_libs(FIXTURE) == ["Fixture"]


def test_toolchain_libpath_handles_a_missing_toolchain(tmp_path):
    assert toolchain_libpath(tmp_path) is None


def test_toolchain_libpath_encodes_elan_directory_names(tmp_path, monkeypatch):
    """elan writes '/' as '--' and ':' as '---'. Getting this wrong is why lake dies with
    `undefined symbol: lean_alloc_small`."""
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
    elan = tmp_path / "elan"
    base = elan / "toolchains" / "leanprover--lean4---v4.27.0"
    (base / "lib" / "lean").mkdir(parents=True)
    monkeypatch.setenv("ELAN_HOME", str(elan))
    # lib/lean must come first: it holds libleanshared.so, and a different toolchain's copy
    # earlier on the path is what causes `undefined symbol: lean_alloc_small`.
    assert toolchain_libpath(tmp_path) == f"{base / 'lib' / 'lean'}:{base / 'lib'}"


def test_project_env_prefixes_ld_library_path(tmp_path, monkeypatch):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n")
    elan = tmp_path / "elan"
    libdir = elan / "toolchains" / "leanprover--lean4---v4.27.0" / "lib"
    libdir.mkdir(parents=True)
    monkeypatch.setenv("ELAN_HOME", str(elan))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/some/other/toolchain")
    env = project_env(tmp_path)
    assert env["LD_LIBRARY_PATH"].startswith(str(libdir))
    assert env["LD_LIBRARY_PATH"].endswith("/some/other/toolchain")


def test_unbuilt_project_is_reported_not_built(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "empty"\n')
    res = audit_project(tmp_path, build=False)
    assert res.status == "unbuilt"


def test_memory_gate_is_a_noop_when_disabled():
    wait_for_memory(0)  # must return immediately
    assert memory_available_gb() > 0


def test_deduplicate_counts_each_name_once():
    results = {
        "/a": {"status": "certified", "clean_names": ["X.thm", "X.other"]},
        "/b": {"status": "certified", "clean_names": ["X.thm", "Y.thm"]},
        "/c": {"status": "build_failed"},
    }
    unique, total = deduplicate(results)
    assert (unique, total) == (3, 4)


# --------------------------------------------------------------------- end-to-end tests


@pytest.fixture(scope="module")
def built_fixture() -> Path:
    env = project_env(FIXTURE)
    p = subprocess.run(["lake", "build"], cwd=FIXTURE, env=env, check=False,
                       capture_output=True, text=True, timeout=900)
    assert lib_dir(FIXTURE) is not None, f"fixture failed to build:\n{p.stdout}\n{p.stderr}"
    return FIXTURE


@needs_lake
def test_fixture_modules_are_discovered(built_fixture):
    mods = modules(lib_dir(built_fixture))
    assert set(mods) == {"Fixture", "Fixture.Clean", "Fixture.Tainted"}


@needs_lake
def test_auditor_classifies_the_fixture_exactly(built_fixture):
    res = audit_project(built_fixture)
    assert res.status == "certified", res.message
    assert res.clean == 3, f"expected 3 clean, got {res.clean}: {res.clean_names}"
    assert res.sorry == 1, f"expected 1 sorry, got {res.sorry}: {res.sorry_names}"
    assert res.axiom == 2, f"expected 2 axiom-dependent, got {res.axiom}: {res.axiom_names}"
    assert res.theorems == 6


@needs_lake
def test_sorry_is_named(built_fixture):
    res = audit_project(built_fixture)
    assert any("unproved" in n for n in res.sorry_names)


@needs_lake
def test_axiom_dependency_is_reported_with_the_offending_axiom(built_fixture):
    res = audit_project(built_fixture)
    assert any("uses_axiom" in n and "Fixture.oracle" in n for n in res.axiom_names)


@needs_lake
def test_transitive_axiom_dependency_is_caught(built_fixture):
    """The point of collectAxioms: a theorem two steps away from an axiom is still tainted."""
    res = audit_project(built_fixture)
    assert any("uses_axiom_transitively" in n for n in res.axiom_names)


@needs_lake
def test_a_clean_but_trivial_theorem_is_still_clean(built_fixture):
    """`2 + 2 = 4` is kernel-clean and worthless. The tool cannot tell the difference, and
    the README must not pretend otherwise."""
    res = audit_project(built_fixture)
    assert any("trivial_arithmetic" in n for n in res.clean_names)


@needs_lake
def test_generated_declarations_are_not_counted(built_fixture):
    """Compiler-generated lemmas (injEq, casesOn, match equations) are not authored theorems."""
    res = audit_project(built_fixture)
    for name in res.clean_names:
        assert not name.endswith((".injEq", ".casesOn", ".noConfusion"))
