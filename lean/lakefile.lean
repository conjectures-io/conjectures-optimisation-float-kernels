import Lake
open Lake DSL

/-!
# The competition's Lean workspace

FloatLib is a pinned dependency; nothing in it is edited. `Contract` is pinned by the operator,
`Submission` is the miner's `Kernel.lean` copied in by `gate.py`, `Verify` is the three-line
statement check (`VerifyMul`, `VerifyFma` for the other slots). `BenchCore` is the shared timing
core; `bench`, `benchMul` and `benchFma` run it over `Submission.add`, `.mul`, `.fma`. The gate pins
each slot's bench built from that slot's incumbent submission, so a miner's build never touches
the binary it is compared against. `micro` is the cost breakdown.

`stage2/` is the stage-2 draft: tensor slots stated against TorchLean's specs. Only its targets
import TorchLean, and the gate never builds them, so a validator needs neither TorchLean's build nor
a C compiler; building them does (`lake build Stage2 stage2bench fused`).
-/

package competition where
  leanOptions := #[⟨`autoImplicit, false⟩, ⟨`relaxedAutoImplicit, false⟩]

require floatlib from git
  "https://github.com/lean-dojo/FloatLib" @ "0d91825727839f597fd06b22fdd038ea21480f0c"

-- Stage 2 only. FloatLib's pin above wins over TorchLean's `@ main`.
require TorchLean from git
  "https://github.com/lean-dojo/TorchLean" @ "de3192df4779877ceb65cfe470a4d4ce9d480a5b"

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

lean_lib VerifyMul where
  srcDir := "verify"
  roots := #[`VerifyMul]

lean_exe benchMul where
  srcDir := "bench"
  root := `MainMul

lean_lib VerifyFma where
  srcDir := "verify"
  roots := #[`VerifyFma]

lean_exe benchFma where
  srcDir := "bench"
  root := `MainFma

lean_exe micro where
  srcDir := "bench"
  root := `Micro

-- Stage 2: stage 1's best proven add and mul, copied with their namespace renamed
-- (tests/test_stage2_copies.py keeps them in step with kernels/).
lean_lib Scalar where
  srcDir := "stage2"
  globs := #[.submodules `Scalar]

-- Stage 2: the contract draft against TorchLean's specs, the kernels and their proofs.
lean_lib Stage2 where
  srcDir := "stage2"
  globs := #[.submodules `Stage2]

-- Stage 2: reference kernels vs the proved stage-1 composition; FloatLib only.
lean_exe stage2bench where
  srcDir := "stage2/bench"
  root := `Stage2Bench

-- Stage 2: unproved headroom probes (fusion, interleaving, zero-skip); all must match the reference.
lean_exe fused where
  srcDir := "stage2/bench"
  root := `Fused
