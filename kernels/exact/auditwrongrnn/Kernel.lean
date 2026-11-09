import ContractExact

/-!
# Exact runtime, `auditwrongrnn` fixture

The spec's kernels, except that `lstm` reads its gate blocks in ONNX's order (`i, o, f, c`)
instead of the contract's PyTorch order (`i, f, g, o`), with no proof. Like `auditwrong`, it
stands for a wrong kernel that got past the proof checks: the proof stages reject this file, but
the audit (`exactAudit`, `gate.py --audit`) must catch it from its outputs alone.
-/

namespace Submission

open ExactRuntime

/-- Gate block `b` of `rows`-word blocks taken from block `order b`: an LSTM written for ONNX's
`i, o, f, c` reads the contract's `f` block as `o`, `g` as `f` and `o` as `g`. -/
def reorderGates (rows : Nat) (a : Array Word) : Array Word :=
  let order : Nat → Nat := fun b => if b = 1 then 3 else if b = 2 then 1 else if b = 3 then 2 else 0
  Array.ofFn (n := a.size) fun q =>
    a[order (q.val / rows) * rows + q.val % rows]!

def kernels : Kernels :=
  { specKernels with rnn := { specRnn with
      lstm := fun T inW H x wih whh bih bhh =>
        lstmSpec T inW H x (reorderGates (H * inW) wih) (reorderGates (H * H) whh)
          (reorderGates H bih) (reorderGates H bhh) } }

end Submission
