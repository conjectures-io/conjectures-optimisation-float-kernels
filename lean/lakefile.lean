import Lake
open Lake DSL

/-!
# The competition's Lean workspace

FloatLib is a pinned dependency; nothing in it is edited. `Contract` is pinned by the operator,
`Submission` is the miner's `Kernel.lean` copied in by `gate.py`, `Verify` is the three-line
statement check (`VerifyMul`, `VerifyFma`, `VerifyDiv`, `VerifySqrt` for the other slots).
`BenchCore` is the shared timing core; `bench`, `benchMul`, `benchFma`, `benchDiv` and `benchSqrt`
run it over `Submission.add`, `.mul`, `.fma`, `.div`, `.sqrt`. The gate pins
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
  -- ContractTensor (the sum slot's rule) is its own module so the scalar slots compile as calibrated.
  roots := #[`Contract, `ContractTensor]

lean_lib Submission where
  srcDir := "work"
  roots := #[`Submission]

lean_lib Verify where
  srcDir := "verify"
  roots := #[`Verify]

lean_lib BenchCore where
  srcDir := "bench"
  -- BenchDivSqrt and BenchTensor are separate so the add, mul and fma binaries compile exactly as
  -- calibrated.
  roots := #[`Bench, `BenchDivSqrt, `BenchTensor, `BenchBmm, `BenchFull]

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

lean_lib VerifyDiv where
  srcDir := "verify"
  roots := #[`VerifyDiv]

lean_exe benchDiv where
  srcDir := "bench"
  root := `MainDiv

lean_lib VerifySum where
  srcDir := "verify"
  roots := #[`VerifySum]

-- The full entry, the one paid board: add, mul, sum and bmm checked and timed together.
lean_lib VerifyFull where
  srcDir := "verify"
  roots := #[`VerifyFull]

lean_exe benchFull where
  srcDir := "bench"
  root := `MainFull

lean_lib VerifyBmm where
  srcDir := "verify"
  roots := #[`VerifyBmm]

lean_exe benchBmm where
  srcDir := "bench"
  root := `MainBmm

lean_exe benchSum where
  srcDir := "bench"
  root := `MainSum

lean_lib VerifySqrt where
  srcDir := "verify"
  roots := #[`VerifySqrt]

lean_exe benchSqrt where
  srcDir := "bench"
  root := `MainSqrt

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
