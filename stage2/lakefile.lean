import Lake
open Lake DSL

/-!
# Stage 2 draft: tensor kernels against TorchLean's Binary32 specs

A workspace of its own, so the stage-1 gate (`../lean`) builds exactly as before. FloatLib is
pinned at stage 1's revision; the root pin wins over TorchLean's `@ main`. TorchLean supplies the
specs (`reduceSumLeftSpec`, `bmmSpec`) and needs a C compiler even to be imported.

`Scalar` holds stage 1's best proven add (`submissions/tuned`) and mul (`submissions/mul-fast`),
copied with their namespace renamed; `Stage2` the contract draft, kernels and proofs; `bench`
the timing executables.
-/

package stage2 where
  leanOptions := #[⟨`autoImplicit, false⟩, ⟨`relaxedAutoImplicit, false⟩]

require floatlib from git
  "https://github.com/lean-dojo/FloatLib" @ "0d91825727839f597fd06b22fdd038ea21480f0c"

require TorchLean from git
  "https://github.com/lean-dojo/TorchLean" @ "de3192df4779877ceb65cfe470a4d4ce9d480a5b"

@[default_target]
lean_lib Scalar where
  globs := #[.submodules `Scalar]

@[default_target]
lean_lib Stage2 where
  globs := #[.submodules `Stage2]

-- Reference kernels vs the proved stage-1 composition; FloatLib only.
@[default_target]
lean_exe stage2bench where
  srcDir := "bench"
  root := `Stage2Bench

-- Unproved headroom probes (fusion, interleaving, zero-skip); all must match the reference bits.
@[default_target]
lean_exe fused where
  srcDir := "bench"
  root := `Fused
