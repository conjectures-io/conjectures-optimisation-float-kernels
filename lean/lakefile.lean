import Lake
open Lake DSL

package competition where
  leanOptions := #[⟨`autoImplicit, false⟩, ⟨`relaxedAutoImplicit, false⟩]

require floatlib from git
  "https://github.com/lean-dojo/FloatLib" @ "0d91825727839f597fd06b22fdd038ea21480f0c"

-- TorchLean, for the link proofs under `stage2/` only; FloatLib's pin above wins over its `@ main`.
require TorchLean from git
  "https://github.com/lean-dojo/TorchLean" @ "de3192df4779877ceb65cfe470a4d4ce9d480a5b"

lean_lib Contract where
  srcDir := "contract"
  globs := #[.one `ContractExact, .one `ContractExactChecked, .submodules `ExactToolkit]

lean_lib Submission where
  srcDir := "work"
  roots := #[`Submission]

lean_lib BenchCore where
  srcDir := "bench"
  roots := #[`ExactSuite]

-- Research-only references for exactShowcase; not imported by VerifyExact or benchExact.
lean_lib ScalarReference where
  srcDir := "contract"
  roots := #[`Contract]

-- Earlier proved scalar algorithms used as a showcase comparison.
lean_lib Scalar where
  srcDir := "stage2"
  globs := #[.submodules `Scalar]

-- The exact-runtime slot: its statement check, its model-suite bench, and a developer bench that
-- compares an entry with FloatLib's kernels and native floats.
lean_lib VerifyExact where
  srcDir := "verify"
  roots := #[`VerifyExact]

lean_exe benchExact where
  srcDir := "bench"
  root := `MainExact

lean_exe exactDev where
  srcDir := "bench"
  root := `ExactDev

lean_exe validateExact where
  srcDir := "audit"
  root := `ValidateSuite

lean_exe exactSpecProbe where
  srcDir := "audit"
  root := `SpecProbe

lean_exe auditExactShapes where
  srcDir := "audit"
  root := `SuiteAudit

lean_exe auditExactWorkloads where
  srcDir := "audit"
  root := `WorkloadAudit

-- Operator proofs that every seeded model satisfies the shape contract.
lean_lib ExactSuiteProofs where
  srcDir := "audit"
  roots := #[`ExactSuiteProofs]

-- The exact runtime's showcase: trained digit models, four runtimes, speed, accuracy and ulps.
lean_exe exactShowcase where
  srcDir := "showcase"
  root := `Showcase

-- The exact runtime's audit: each kernel of an entry against the specs, on edge-case inputs.
lean_lib ExactAuditCore where
  srcDir := "audit"
  roots := #[`ExactAuditCore]

lean_exe exactAudit where
  srcDir := "audit"
  root := `ExactAudit

-- One candidate mutant from the autoresearch loop (`autoresearch/Candidate.lean`, not tracked).
lean_exe auditCandidate where
  srcDir := "autoresearch"
  root := `Candidate
