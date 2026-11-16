/-
Axiom auditor for a built Lean 4 project.

  lake env lean --run audit.lean [--trust-level=N] <FirstPartyModule>...

Imports the given (already-built) modules, then for every theorem *defined in those
modules* computes its transitive axiom set with `CollectAxioms` and classifies it:

  CLEAN  : axiom set ⊆ {propext, Classical.choice, Quot.sound}   (kernel-trusted)
  SORRY  : depends on sorryAx                                    (NOT certified)
  AXIOM   : depends on some other custom axiom                   (NOT certified)

Compiler-generated declarations (`injEq`, `casesOn`, match equations, …) are skipped, so
the totals count authored theorems. Each theorem is counted once even when a module has to
be imported in more than one environment (see `importDropping`).

Output: `SORRY <name>` / `AXIOM <name> :: <axioms>` lines, one `CENSUS_RESULT {json}` line,
then a `CLEAN <name>` line per clean theorem.

`--trust-level=N` is passed to `importModules`. The default, 1024, trusts the `.olean` files
as lake built them, which is what you want for an audit of your own build. Pass
`--trust-level=0` to make the kernel re-check the imported proofs instead; that is stronger
and much slower.

WHAT THIS DOES NOT TELL YOU: a CLEAN verdict means no `sorry` and no custom axiom. It says
nothing about whether the theorem is *interesting*. `2 + 2 = 4` is CLEAN. A theorem
quantified over a vacuous hypothesis is CLEAN. An axiom-free proof of a statement that does
not mean what you think it means is CLEAN. Axiom hygiene is a necessary condition for
trusting a proof, never a sufficient one.
-/
import Lean
open Lean Meta

def trusted : List Name := [``propext, ``Classical.choice, ``Quot.sound]

/-- last-component names that are compiler-generated (not authored proofs) -/
def genSuffix : List String :=
  ["injEq", "sizeOf_spec", "noConfusion", "noConfusionType", "below", "ibelow",
   "brecOn", "binductionOn", "rec", "recOn", "casesOn", "induct", "ind", "mk",
   "eq_def", "eq_unfold", "fold"]

/-- true if `n` is a compiler-generated declaration rather than an authored theorem -/
def isGenerated (n : Name) : Bool :=
  n.isInternalDetail
  || (match n with
      | .str _ s =>
        genSuffix.contains s
        || (s.startsWith "eq_" && (s.drop 3).all Char.isDigit && s.length > 3)
        || s.startsWith "match_" || s.startsWith "proof_"
      | _ => true)
  || n.components.any (fun c => match c with
      | .str _ s => s.startsWith "_" || s == "match_" || s.startsWith "proof_"
      | _ => false)

/-- transitive axioms of a constant -/
def axiomsOf (env : Environment) (n : Name) : Array Name :=
  let (_, s) := ((CollectAxioms.collect n).run env).run {}
  s.axioms

/-- Classify every first-party theorem in `env` whose home module is in `modSet`
    and that we have not already `seen`.  Emits SORRY/AXIOM lines, accumulates clean
    names, and returns updated (total, clean, sorry, axiom, cleanNames, seen). -/
def classifyEnv (env : Environment) (modSet : NameSet)
    (acc : Nat × Nat × Nat × Nat × Array Name × NameSet) :
    Nat × Nat × Nat × Nat × Array Name × NameSet × Array String := Id.run do
  let (t0, c0, s0, a0, cn0, seen0) := acc
  let mut total := t0; let mut clean := c0; let mut nsorry := s0; let mut naxiom := a0
  let mut cleanNames := cn0; let mut seen := seen0
  let mut lines : Array String := #[]
  for (name, info) in env.constants.toList do
    let isThm := match info with | .thmInfo _ => true | _ => false
    if !isThm then continue
    let some midx := env.getModuleIdxFor? name | continue
    let mname := env.header.moduleNames[midx.toNat]!
    if !modSet.contains mname then continue
    if isGenerated name then continue
    if seen.contains name then continue          -- count each theorem once across envs
    seen := seen.insert name
    total := total + 1
    let ax := axiomsOf env name
    let hasSorry := ax.any (· == ``sorryAx)
    let hasBad := ax.any (fun a => !(trusted.contains a))
    if hasSorry then
      nsorry := nsorry + 1
      lines := lines.push s!"SORRY {name}"
    else if hasBad then
      naxiom := naxiom + 1
      let bad := ax.filter (fun a => !(trusted.contains a) && a != ``sorryAx)
      let badStr := String.intercalate " " (bad.toList.map (fun a => a.toString))
      lines := lines.push s!"AXIOM {name} :: {badStr}"
    else
      clean := clean + 1
      cleanNames := cleanNames.push name
  return (total, clean, nsorry, naxiom, cleanNames, seen, lines)

/-- Parse the colliding module out of an importModules error like
    "import Foo.Bar failed, environment already contains 'X' from Foo.Baz". -/
def parseFailedModule (msg : String) : Option Name :=
  match (msg.splitOn " failed").head? with
  | some pre =>
    match (pre.splitOn "import ").getLast? with
    | some modStr =>
      let s := modStr.trim
      if s.isEmpty || s == pre then none else some s.toName
    | none => none
  | none => none

/-- Import as many of `mods` together as possible, dropping any single module that
    collides ("environment already contains X") and retrying.  Returns the built
    environment plus the list of modules that had to be dropped. -/
unsafe def importDropping (mods : Array Name) (trustLevel : UInt32) :
    IO (Environment × Array Name) := do
  let mut cur := mods
  let mut dropped : Array Name := #[]
  let mut iters := 0
  while true do
    iters := iters + 1
    if iters > mods.size + 2 then
      throw (IO.userError "importDropping: too many collision retries")
    try
      let env ← importModules (cur.map (fun m => {module := m})) {} (trustLevel := trustLevel)
      return (env, dropped)
    catch e =>
      match parseFailedModule e.toString with
      | some bad =>
        if cur.contains bad then
          dropped := dropped.push bad
          cur := cur.filter (· != bad)
        else
          throw e
      | none => throw e
  throw (IO.userError "unreachable")

unsafe def main (args : List String) : IO Unit := do
  -- (`--isolate` accepted for backward-compat; collision handling is now always on)
  let trustLevel : UInt32 :=
    match args.find? (·.startsWith "--trust-level=") with
    | some a => (a.drop "--trust-level=".length).toNat?.getD 1024 |>.toUInt32
    | none => 1024
  let modArgs := args.filter (fun a => a != "--isolate" && !a.startsWith "--trust-level=")
  let mods := modArgs.toArray.map (fun s => s.toName)
  let modSet : NameSet := mods.foldl (·.insert ·) {}
  initSearchPath (← findSysroot)
  let mut acc : Nat × Nat × Nat × Nat × Array Name × NameSet := (0, 0, 0, 0, #[], {})
  -- main group: import everything, dropping collisions
  let (env, dropped) ← importDropping mods trustLevel
  let (t, c, s, a, cn, seen, lines) := classifyEnv env modSet acc
  for l in lines do IO.println l
  acc := (t, c, s, a, cn, seen)
  -- second pass over the dropped modules (their unique theorems still count once)
  let mut rest := dropped
  let mut guard := 0
  while rest.size > 0 && guard < mods.size + 2 do
    guard := guard + 1
    let (env2, dropped2) ← importDropping rest trustLevel
    let (t2, c2, s2, a2, cn2, seen2, lines2) := classifyEnv env2 modSet acc
    for l in lines2 do IO.println l
    acc := (t2, c2, s2, a2, cn2, seen2)
    if dropped2.size == rest.size then
      -- no progress (these collide with each other); report and stop
      for m in dropped2 do IO.eprintln s!"UNRESOLVED_MODULE {m}"
      rest := #[]
    else
      rest := dropped2
  let (total, clean, nsorry, naxiom, cleanNames, _) := acc
  IO.println s!"CENSUS_RESULT \{\"theorems\":{total},\"clean\":{clean},\"sorry\":{nsorry},\"axiom\":{naxiom}}"
  for n in cleanNames do
    IO.println s!"CLEAN {n}"
