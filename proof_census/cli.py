"""Command line interface: `proof-census [options] PATH...`"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .lean import audit_project, deduplicate, find_projects, is_lean_project


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proof-census",
        description="Audit Lean 4 projects for axiom hygiene: how many theorems are actually "
                    "kernel-clean (no sorry, no custom axiom)?",
        epilog="A CLEAN verdict means no sorry and no custom axiom. It does not mean the "
               "theorem is interesting, or that it says what you think it says.",
    )
    p.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                   help="Lean project directories, or directories to scan for them")
    p.add_argument("--build", action="store_true",
                   help="run `lake build` for projects with no build output (default: skip them)")
    p.add_argument("--json", type=Path, metavar="FILE",
                   help="write the full per-project results as JSON")
    p.add_argument("--resume", action="store_true",
                   help="with --json, skip projects already recorded as certified in that file")
    p.add_argument("--trust-level", type=int, default=1024, metavar="N",
                   help="importModules trust level. Default 1024 trusts the oleans as built; "
                        "0 makes the kernel re-check imported proofs (stronger, much slower)")
    p.add_argument("--timeout", type=int, default=1800, metavar="SECS",
                   help="per-project timeout for build and for audit (default: 1800)")
    p.add_argument("--min-ram-gb", type=float, default=0.0, metavar="GB",
                   help="with --build, wait until this much memory is free before each build "
                        "(default: 0, no gate)")
    p.add_argument("--max-depth", type=int, default=4, metavar="N",
                   help="how deep to scan for projects (default: 4)")
    p.add_argument("--names", action="store_true",
                   help="print the name of every sorry-tainted and axiom-dependent theorem")
    p.add_argument("--quiet", "-q", action="store_true", help="only print the summary")
    p.add_argument("--version", action="version", version=f"proof-census {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for path in args.paths:
        if not path.exists():
            print(f"error: no such path: {path}", file=sys.stderr)
            return 2

    explicit = [p.resolve() for p in args.paths if is_lean_project(p)]
    to_scan = [p for p in args.paths if not is_lean_project(p)]
    projects = explicit + [p for p in find_projects(to_scan, args.max_depth) if p not in explicit]

    if not projects:
        print("no Lean projects found (looked for lakefile.lean / lakefile.toml)", file=sys.stderr)
        return 1

    results: dict[str, dict] = {}
    if args.resume and args.json and args.json.exists():
        results = json.loads(args.json.read_text())

    def log(*a):
        if not args.quiet:
            print(*a, flush=True)

    log(f"auditing {len(projects)} project(s)")
    for root in projects:
        key = str(root)
        if results.get(key, {}).get("status") == "certified":
            log(f"[skip] {root} (already certified)")
            continue
        res = audit_project(
            root,
            build=args.build,
            trust_level=args.trust_level,
            timeout=args.timeout,
            min_ram_gb=args.min_ram_gb,
            log=log,
        )
        results[key] = res.to_json()
        if res.status == "certified":
            log(f"[{res.status} {res.secs}s] {root}: "
                f"clean={res.clean} theorems={res.theorems} sorry={res.sorry} axiom={res.axiom}")
            if args.names:
                for n in res.sorry_names:
                    log(f"    SORRY {n}")
                for n in res.axiom_names:
                    log(f"    AXIOM {n}")
        else:
            log(f"[{res.status}] {root}: {res.message[:200]}")
        if args.json:
            args.json.write_text(json.dumps(results, indent=2))

    certified = [r for r in results.values() if r.get("status") == "certified"]
    unique, total = deduplicate(results)

    print()
    print("=== summary ===")
    print(f"projects audited:        {len(certified)} of {len(results)}")
    print(f"authored theorems:       {sum(r['theorems'] for r in certified)}")
    print(f"clean (kernel-trusted):  {sum(r['clean'] for r in certified)}")
    print(f"  unique names:          {unique}" + (f"  ({total - unique} duplicate copies"
                                                  " across projects)" if total != unique else ""))
    print(f"sorry-tainted:           {sum(r['sorry'] for r in certified)}")
    print(f"axiom-dependent:         {sum(r['axiom'] for r in certified)}")
    for status in ("unbuilt", "build_failed", "build_no_oleans", "audit_failed"):
        n = sum(1 for r in results.values() if r.get("status") == status)
        if n:
            print(f"{status}: {n}")
    if args.json:
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
