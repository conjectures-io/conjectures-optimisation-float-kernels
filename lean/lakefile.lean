import Lake
open Lake DSL

/-!
# The competition's Lean workspace

FloatLib is a pinned dependency; nothing in it is edited. `Contract` is pinned by the operator,
`Submission` is the miner's `Kernel.lean` copied in by `gate.py`, `Verify` is the three-line
statement check. `BenchCore` is the shared timing core and `bench` runs it over
`Submission.add`; the gate pins one `bench` built from the incumbent's own submission, so a
miner's build never touches the binary it is compared against. `micro` is the cost breakdown.
-/

package competition where
  leanOptions := #[⟨`autoImplicit, false⟩, ⟨`relaxedAutoImplicit, false⟩]

require floatlib from git
  "https://github.com/lean-dojo/FloatLib" @ "0d91825727839f597fd06b22fdd038ea21480f0c"

lean_lib Contract where
  srcDir := "contract"
  roots := #[`Contract]

lean_lib Submission where
  srcDir := "work"
  roots := #[`Submission]

lean_lib Verify where
  srcDir := "verify"
  roots := #[`Verify]

lean_lib BenchCore where
  srcDir := "bench"
  roots := #[`Bench]

lean_exe bench where
  srcDir := "bench"
  root := `Main

lean_exe micro where
  srcDir := "bench"
  root := `Micro
