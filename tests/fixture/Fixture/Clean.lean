/- Three honest theorems. No `sorry`, no custom axiom. -/
namespace Fixture

theorem add_comm_two (a b : Nat) : a + b = b + a := Nat.add_comm a b

theorem trivial_arithmetic : 2 + 2 = 4 := rfl

theorem le_refl_nat (n : Nat) : n ≤ n := Nat.le_refl n

end Fixture
