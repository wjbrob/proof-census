"""Discover, build and axiom-audit Lean 4 projects.

Every function here is importable and side-effect free at module scope. The audit itself is
done by `audit.lean`, which ships alongside this module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

AUDIT_LEAN = Path(__file__).resolve().parent / "audit.lean"

TRUSTED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")


# --------------------------------------------------------------------------------------
# project discovery
# --------------------------------------------------------------------------------------

LAKEFILES = ("lakefile.lean", "lakefile.toml")

# Directories never worth descending into when scanning for projects.
SKIP_DIRS = {
    ".lake", ".git", ".github", "build", "node_modules", ".venv", "venv",
    "__pycache__", ".elan", "target",
}


def is_lean_project(path: Path) -> bool:
    """A directory is a Lean project if it has a lakefile."""
    return any((path / f).is_file() for f in LAKEFILES)


def find_projects(roots: list[Path], max_depth: int = 4) -> list[Path]:
    """Lean project directories under `roots`, outermost first.

    A project found inside another project (a vendored dependency, say) is not reported:
    its theorems belong to its own authors, not to yours.
    """
    found: list[Path] = []
    for root in roots:
        root = root.resolve()
        if is_lean_project(root):
            found.append(root)
            continue
        for dirpath, dirnames, _ in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            here = Path(dirpath)
            if is_lean_project(here):
                found.append(here)
                dirnames[:] = []  # don't descend into a project's own dependencies
    # de-duplicate while preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# --------------------------------------------------------------------------------------
# toolchain handling
# --------------------------------------------------------------------------------------


def toolchain_libpath(root: Path) -> str | None:
    """The elan lib directories for a project's *own* toolchain, as an LD_LIBRARY_PATH prefix.

    The Lean shared libraries (libleanshared.so and friends) live in `<tc>/lib/lean`, native
    dependencies in `<tc>/lib`. Both must come BEFORE any other toolchain on
    LD_LIBRARY_PATH: otherwise a different version of libleanshared shadows the right one and
    lake dies with `undefined symbol: lean_alloc_small`. If you audit projects pinned to
    several toolchains, this is not optional.
    """
    try:
        toolchain = (root / "lean-toolchain").read_text().strip()
    except OSError:
        return None
    # elan encodes '/' as '--' and ':' as '---' in directory names.
    name = toolchain.replace("/", "--").replace(":", "---")
    base = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / "toolchains" / name
    dirs = [str(d) for d in (base / "lib" / "lean", base / "lib") if d.is_dir()]
    return ":".join(dirs) if dirs else None


def project_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    lib = toolchain_libpath(root)
    if lib:
        base = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib}:{base}" if base else lib
    return env


def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=project_env(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -9, "", "TIMEOUT"
    except FileNotFoundError as e:
        return -1, "", f"not found: {e}"


# --------------------------------------------------------------------------------------
# build artefacts
# --------------------------------------------------------------------------------------


def lean_libs(root: Path) -> list[str]:
    """`lean_lib` target names declared in lakefile.lean / lakefile.toml.

    `lake build` with no target is a no-op in projects that declare no default target, so
    the explicit library names are what we pass.
    """
    libs: list[str] = []
    lf = root / "lakefile.lean"
    if lf.is_file():
        text = lf.read_text(errors="ignore")
        libs += re.findall(r"lean_lib\s+[`«]?([A-Za-z_][\w'.]*)", text)
    lt = root / "lakefile.toml"
    if lt.is_file():
        text = lt.read_text(errors="ignore")
        for block in text.split("[[lean_lib]]")[1:]:
            m = re.search(r'name\s*=\s*"([^"]+)"', block)
            if m:
                libs.append(m.group(1))
    return libs


def lib_dir(root: Path) -> Path | None:
    """The build output directory, but only if it actually contains first-party oleans."""
    for cand in (".lake/build/lib/lean", ".lake/build/lib"):
        d = root / cand
        if not d.is_dir():
            continue
        for dirpath, _, files in os.walk(d):
            if any(f.endswith(".olean") for f in files):
                return d
    return None


def modules(libd: Path) -> list[str]:
    """Module names for every olean under `libd`."""
    mods: list[str] = []
    for dirpath, _, files in os.walk(libd):
        for f in files:
            if f.endswith(".olean"):
                rel = os.path.relpath(os.path.join(dirpath, f), libd)
                mods.append(rel[: -len(".olean")].replace(os.sep, "."))
    return sorted(mods)


def memory_available_gb() -> float:
    """MemAvailable in GiB, or +inf where /proc/meminfo is unavailable."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return float("inf")


def wait_for_memory(min_gb: float, poll_s: int = 20, log=print) -> None:
    """Block until MemAvailable >= min_gb. A no-op when min_gb <= 0.

    Lean builds of large developments are memory-hungry rather than CPU-hungry; running
    several at once is how you get an OOM kill halfway through a census.
    """
    if min_gb <= 0:
        return
    waited = 0
    while memory_available_gb() < min_gb:
        if waited % 120 == 0:
            log(f"[ram-gate] available={memory_available_gb():.1f}GB < {min_gb}GB — waiting")
        time.sleep(poll_s)
        waited += poll_s


# --------------------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------------------


@dataclass
class AuditResult:
    status: str
    root: str
    secs: float = 0.0
    theorems: int = 0
    clean: int = 0
    sorry: int = 0
    axiom: int = 0
    clean_names: list[str] = field(default_factory=list)
    axiom_names: list[str] = field(default_factory=list)
    sorry_names: list[str] = field(default_factory=list)
    n_modules: int = 0
    message: str = ""

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        if not d["message"]:
            del d["message"]
        return d


def _parse_audit_output(out: str) -> tuple[dict | None, list[str], list[str], list[str]]:
    clean, axioms, sorries = [], [], []
    summary = None
    for line in out.splitlines():
        if line.startswith("CLEAN "):
            clean.append(line[6:])
        elif line.startswith("AXIOM "):
            axioms.append(line[6:])
        elif line.startswith("SORRY "):
            sorries.append(line[6:])
        elif line.startswith("CENSUS_RESULT "):
            try:
                summary = json.loads(line[len("CENSUS_RESULT "):])
            except json.JSONDecodeError:
                summary = None
    return summary, clean, axioms, sorries


def audit_project(
    root: Path,
    *,
    build: bool = False,
    trust_level: int = 1024,
    timeout: int = 1800,
    min_ram_gb: float = 0.0,
    log=print,
) -> AuditResult:
    """Audit one Lean project. Builds it first only when `build` is set."""
    root = root.resolve()
    t0 = time.time()
    libd = lib_dir(root)

    if libd is None:
        if not build:
            return AuditResult("unbuilt", str(root), message="no oleans; pass --build")
        wait_for_memory(min_ram_gb, log=log)
        libs = lean_libs(root)
        cmd = ["lake", "build", *libs] if libs else ["lake", "build"]
        log(f"[build] {root}")
        rc, out, err = run(cmd, root, timeout)
        if rc != 0:
            return AuditResult(
                "build_failed", str(root), secs=round(time.time() - t0, 1),
                message=(err or out)[-400:],
            )
        libd = lib_dir(root)
        if libd is None:
            return AuditResult(
                "build_no_oleans", str(root), secs=round(time.time() - t0, 1),
                message=f"build succeeded but produced no oleans (targets: {libs or 'default'})",
            )

    mods = modules(libd)
    if not mods:
        return AuditResult("build_no_oleans", str(root), message="no modules found")

    rc, out, err = run(
        ["lake", "env", "lean", "--run", str(AUDIT_LEAN), f"--trust-level={trust_level}", *mods],
        root, timeout,
    )
    summary, clean, axioms, sorries = _parse_audit_output(out)
    if summary is None:
        return AuditResult(
            "audit_failed", str(root), secs=round(time.time() - t0, 1),
            n_modules=len(mods), message=(err or out)[-500:],
        )
    return AuditResult(
        status="certified",
        root=str(root),
        secs=round(time.time() - t0, 1),
        theorems=summary.get("theorems", 0),
        clean=summary.get("clean", 0),
        sorry=summary.get("sorry", 0),
        axiom=summary.get("axiom", 0),
        clean_names=clean,
        axiom_names=axioms,
        sorry_names=sorries,
        n_modules=len(mods),
    )


def deduplicate(results: dict[str, dict]) -> tuple[int, int]:
    """(unique, total) clean theorem names across projects, deduped by fully-qualified name.

    This matters more than it sounds. A certificate or utility module that several of your
    projects vendor in is counted once per project in the raw totals; the same theorem is
    not new evidence because it was copied. Report the unique figure.
    """
    seen: set[str] = set()
    total = 0
    for rec in results.values():
        if rec.get("status") != "certified":
            continue
        for name in rec.get("clean_names", []):
            total += 1
            seen.add(name)
    return len(seen), total
