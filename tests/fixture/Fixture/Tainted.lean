/-
The ways a theorem fails to be certified.

`trivial_arithmetic` in Clean.lean is the other half of the point: it is perfectly CLEAN and
perfectly worthless. Axiom hygiene is necessary, not sufficient.
-/
namespace Fixture

/-- An unproved statement: the auditor must report this as SORRY. -/
theorem unproved (n : Nat) : n + 0 = n := by sorry

/-- A custom axiom. Anything depending on it is not kernel-certified. -/
axiom oracle (n : Nat) : n * 0 = n

/-- Depends on `oracle`: the auditor must report this as AXIOM. -/
theorem uses_axiom (n : Nat) : n * 0 = n := oracle n

/-- Depends on `oracle` transitively, through another theorem. -/
theorem uses_axiom_transitively (n : Nat) : n * 0 + 0 = n := by
  rw [Nat.add_zero]; exact uses_axiom n

end Fixture
