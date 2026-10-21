import ExactAuditCore
import Submission

open ExactAudit in
/-- `exactAudit [--malformed] [cases] [seed]` on the entry, or `exactAudit --selftest`. -/
def main (args : List String) : IO UInt32 := do
  if args == ["--selftest"] then return ← selftest
  if args.head? == some "--mutants" then
    return ← mutationRun (((args.drop 1).head? >>= String.toNat?).getD 20)
  if args.head? == some "--coverage" then
    coverage (((args.drop 1).head? >>= String.toNat?).getD 20)
    return 0
  let malformed := args.head? == some "--malformed"
  let args := if malformed then args.drop 1 else args
  let cases := (args.head? >>= String.toNat?).getD 20
  let seed := ((args.drop 1).head? >>= String.toNat?).getD 1 |>.toUInt32
  let t0 ← IO.monoMsNow
  let (total, found) := audit Submission.kernels ExactRuntime.specKernels malformed cases seed
  let t1 ← IO.monoMsNow
  IO.println s!"{total} cases ({kernelNames.length} kernels × {kinds.length} kinds × {cases}), seed {seed}, {t1 - t0} ms"
  if found.isEmpty then
    IO.println "every output equal to the spec"
    return 0
  IO.println s!"{found.size} mismatches:"
  for line in summary found do IO.println line
  for m in found.toList.take 10 do
    IO.println s!"  {m.kernel} {m.kind} seed={m.seed} {m.shape}: {m.detail}"
  return 1
