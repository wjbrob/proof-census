# proof-census

<p>
  <a href="../../actions/workflows/ci.yml"><img src="../../actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Lean-4-6E4AFF?style=flat-square" alt="Lean 4">
  <img src="https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT">
</p>

**How many theorems in your Lean 4 project are actually proved?**

Not "how many `theorem` keywords are in the source" — how many have a complete proof that the
kernel accepts, with no `sorry` anywhere in their dependency graph and no custom `axiom`
holding them up. For a single file you can read the output of `#print axioms`. For a
repository, or forty of them, you want this.

```console
$ proof-census tests/fixture --names
auditing 1 project(s)
[certified 2.2s] /home/you/proof-census/tests/fixture: clean=3 theorems=6 sorry=1 axiom=2
    SORRY Fixture.unproved
    AXIOM Fixture.uses_axiom_transitively :: Fixture.oracle
    AXIOM Fixture.uses_axiom :: Fixture.oracle

=== summary ===
projects audited:        1 of 1
authored theorems:       6
clean (kernel-trusted):  3
  unique names:          3
sorry-tainted:           1
axiom-dependent:         2
```

Every theorem is classified by its **transitive** axiom set, via Lean's own `CollectAxioms`:

| Verdict | Meaning |
|---|---|
| `CLEAN` | axioms ⊆ `{propext, Classical.choice, Quot.sound}` — the three Mathlib relies on |
| `SORRY` | depends on `sorryAx`, directly or through any dependency. Not proved |
| `AXIOM` | depends on some other custom axiom, which is reported by name |

Transitivity is the point. `Fixture.uses_axiom_transitively` above never mentions the axiom; it
calls a lemma that calls it. Grepping for `sorry` and `axiom` will not find that, and in a
large development that distance grows.

## What a CLEAN verdict does not tell you

This is a hygiene check, and hygiene is a necessary condition, never a sufficient one. The
tool cannot tell you whether a theorem is worth proving. In particular:

- **`2 + 2 = 4` is CLEAN.** So is every re-proof of something already in Mathlib.
- **A vacuous theorem is CLEAN.** Quantify over a hypothesis nothing satisfies, or state a
  property of a function that is defined to return the answer, and you get a kernel-certified
  proof of nothing. There is a test in this repository asserting exactly that
  (`test_a_clean_but_trivial_theorem_is_still_clean`), because the honest limitation is worth
  pinning down in code.
- **A theorem that does not mean what you think it means is CLEAN.** The kernel checks the
  statement you wrote, not the one you intended.

So if you use this to count your own work, the clean total is a **ceiling**, not a score. The
number of clean theorems that are also substantive and also original is smaller, often by an
order of magnitude, and no tool will compute it for you — that part is reading.

## Install

```bash
pip install git+https://github.com/wjbrob/proof-census
```

Needs Python 3.10+ (no dependencies beyond the standard library) and a working `lake` on
`PATH`, from [elan](https://github.com/leanprover/elan).

## Use

```bash
proof-census path/to/project                  # audit one already-built project
proof-census ~/lean-projects                  # scan a directory for projects and audit each
proof-census ~/proj --build --min-ram-gb 8    # build anything unbuilt first, memory-gated
proof-census ~/proj --json results.json       # record full per-project results
proof-census ~/proj --json results.json --resume   # skip what already succeeded
proof-census path/to/project --names          # name every sorry and axiom-dependent theorem
```

Projects need to be built: the auditor reads `.olean` files, it does not elaborate sources.
Without `--build`, unbuilt projects are reported as `unbuilt` and skipped.

### Counting across many projects

Raw totals across projects double-count. If six of your developments vendor the same
certificate module, its theorems appear six times, and copying a proof does not make it new
evidence. The summary therefore reports unique fully-qualified names alongside the raw total:

```
clean (kernel-trusted):  3806
  unique names:          3240  (566 duplicate copies across projects)
```

Lead with the unique figure.

### Toolchain handling

If your projects are pinned to different Lean versions, each needs its own toolchain
directories first on `LD_LIBRARY_PATH`, or `lake` fails with
`undefined symbol: lean_alloc_small`. `proof-census` derives that per project from its
`lean-toolchain` file — including elan's directory encoding, where `/` becomes `--` and `:`
becomes `---`. You should not have to think about it.

### Trust level

By default the auditor imports oleans with `trustLevel := 1024`, which trusts the build
artefacts as `lake` produced them. That is the right setting for auditing your own build, and
it is fast. To make the kernel re-check the imported proofs instead:

```bash
proof-census path/to/project --trust-level 0
```

Stronger, and much slower. Worth doing once on anything you are about to publish a claim about.

## JSON output

One record per project, keyed by path:

```json
{
  "/path/to/project": {
    "status": "certified",
    "root": "/path/to/project",
    "secs": 2.6,
    "theorems": 6,
    "clean": 3,
    "sorry": 1,
    "axiom": 2,
    "clean_names": ["Fixture.add_comm_two", "..."],
    "axiom_names": ["Fixture.uses_axiom :: Fixture.oracle"],
    "sorry_names": ["Fixture.unproved"],
    "n_modules": 3
  }
}
```

`status` is one of `certified`, `unbuilt`, `build_failed`, `build_no_oleans`, `audit_failed`.
Anything other than `certified` carries a `message` with the tail of the error.

## How it works

[`proof_census/audit.lean`](proof_census/audit.lean) is the whole idea, in 150 lines of Lean
metaprogramming. It imports your built modules, walks `env.constants`, keeps the theorems whose
home module is one of yours, and runs `CollectAxioms` on each.

Two details earn their keep:

- **Compiler-generated declarations are skipped.** `injEq`, `casesOn`, `noConfusion`, match
  equations and friends are not authored theorems, and counting them inflates a census
  severalfold. The filter is a name-shape heuristic (`isGenerated`), so it is approximate; if
  it drops something of yours, that is a bug worth reporting.
- **Name collisions do not abort the run.** Importing every module of a project at once fails
  if two sibling modules define the same name — common in a repository holding several
  variants of one development. Rather than giving up, the auditor drops the colliding module,
  retries, and then audits the dropped modules in a second environment, counting each theorem
  exactly once across both.

The Python side is discovery, toolchain setup, build orchestration and reporting.

## Scope

Lean 4 only, for now. The same approach works for Coq (`Print Assumptions`) and Isabelle, and
earlier private versions of this tool did both; those drivers were not good enough to publish
and are being reworked. Note that Coq and Isabelle do not give you anything as clean as
`CollectAxioms`, so the equivalent checks are weaker and need to be described more carefully —
which is most of the work.

## contrib/overload_guard.py

A standalone watchdog for machines doing heavy proof builds. Large Lean and Coq builds fail by
exhausting memory rather than time, and an orphaned `coqc` can sit on tens of gigabytes
indefinitely. This polls every 15 seconds and kills **only** processes whose `comm` is in an
explicit allowlist (`coqc`, `lean`, `poly`), under three rules: a runaway over the RSS cap, an
orphan reparented to PID 1, or, if available memory stays under the floor for two consecutive
polls, the largest target. Because it matches on `comm` against an allowlist rather than on a
command-line pattern, it cannot reach your editors, shells, databases or VMs — and it cannot
match itself, which is the classic way a `pkill -f` script kills its own shell.

```bash
nohup python3 contrib/overload_guard.py >/dev/null 2>&1 &
kill $(cat contrib/overload_guard.pid)
```

Shipped as-is, configured by constants at the top of the file.

## Development

```bash
pip install -e ".[dev]"
pytest          # builds tests/fixture (a Lean project with no Mathlib dependency) and audits it
ruff check proof_census tests
```

The fixture is deliberately mixed: 3 clean theorems, 1 `sorry`, 2 depending on a custom axiom,
one of them only transitively. The tests assert those exact counts, so a regression in the
classifier fails CI rather than quietly inflating someone's numbers.

## License

MIT, see [LICENSE](LICENSE).
