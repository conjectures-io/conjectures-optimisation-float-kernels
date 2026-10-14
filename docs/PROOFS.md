# Proofs and source policy

## Full-entry obligations

A full entry imports only `ContractTensor`, defines all four kernels under
`namespace Submission`, and supplies `add_spec`, `mul_spec`, `sum_spec`, and
`bmm_spec`. Start from [the full reference](../kernels/full/incumbent/Kernel.lean).

| Kernel | Required equality |
| --- | --- |
| `add` | `∀ x y, add x y = ExecFloat.Spec.add x y` |
| `mul` | `∀ x y, mul x y = ExecFloat.Spec.mul x y` |
| `sum` | `∀ n (x : Vector Binary32 n), sum x = sumSpec x.get` |
| `bmm` | `∀ batch m n p A B, (bmm batch m n p A B).get = bmmSpec batch m n p A.get B.get` |

Exact types are in [Contract.lean](../lean/contract/Contract.lean) and
[ContractTensor.lean](../lean/contract/ContractTensor.lean).
The full verifier checks the conjunction of all four obligations.

`Binary32` is FloatLib's binary format with eight exponent bits and 23 fraction
bits. Proofs must cover all values required by the specification, including NaNs,
infinities, subnormals, and signed zeros.

`sumSpec` starts at positive zero and adds elements in index order.
`bmmSpec` uses row-major buffers; each output starts at positive zero and folds
`add acc (mul a b)` in increasing reduction-index order. Total reads return
positive zero outside the buffer. Every addition rounds, so reassociation and
replacing separate multiply/add with a fused operation are not generally exact.

The tensor rules are restated over FloatLib so the gate need not build TorchLean.
[Stage2/Link.lean](../lean/stage2/Stage2/Link.lean) proves their equality to the
pinned TorchLean definitions; its build is checked separately.

## Reusing existing proofs


For addition, the obligation in [Contract.lean](../lean/contract/Contract.lean) is:

```lean
def Obligation (kernel : Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y : Binary32, kernel x y = ExecFloat.Spec.add x y
```

The recommended route: prove your kernel equals the incumbent, then reuse the incumbent's theorem.
The following is an unfinished proof sketch, not an uploadable submission.
The fast-path goal must be proved and the placeholder removed:

```lean
theorem add_eq_incumbent (x y : Binary32) : add x y = ExecFloat.add x y := by
  unfold add
  dsimp only          -- zeta-reduce the lets so `split` sees the branches
  split
  · rfl               -- the fallback branch: it is ExecFloat.add by definition
  · sorry             -- the fast path: the lemma you actually prove

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y := fun x y => by
  rw [add_eq_incumbent, ExecFloat.Proof.add_eq_spec]
```

What the fast-path lemma rests on. These are public, so you can use them (paths under
`lean/.lake/packages/floatlib/FloatLib/`):

| theorem | file | says |
|---|---|---|
| `ExecFloat.Proof.add_eq_spec` | generic over the `ExecFloat.Add` instance | `ExecFloat.add x y = ExecFloat.Spec.add x y` |
| `Model.AddBackend.word_eq_spec` | `Floats/ExecFloat/Backends/Dispatch/Add/Proof.lean` | the word dispatcher equals `Spec.add`; for binary32 it runs `NativeBinary32.addFiniteImpl?` and sends specials to `generic` |
| `NativeBinary32.addFiniteImpl_eq` | `Floats/ExecFloat/Backends/Word/Narrow/Addition/Proof.lean` | the certified word kernel `addFiniteImpl?` equals `addFinite?` |
| `NativeBinary32.addFinite_eq` | `Floats/ExecFloat/Backends/Word/Narrow/Agreement.lean` | `addFinite?` is `roundDyadic (addDyadic dx dy)` on finite operands: the exact-dyadic reference |

What is **not** available: helper lemmas behind `addFiniteImpl_eq` (alignment, rounding
under shift, the far-gap case) are `private`. A kernel with the incumbent's
structure does not need them: reuse the whole theorem. A kernel with a different structure, such
as the textbook adder, needs its own alignment and rounding argument.

**Route 1: reuse the word kernel's theorem.** The examples in `kernels/add/direct`,
`mirror` and `fastlog` demonstrate this approach. These fragments assume the
namespace openings and helper definitions in those files:

```lean
abbrev Fmt := ExecFloat.Binary.format (exponentBits := 8) (fractionBits := 23)
abbrev Plan := Configured.StoragePlan.forKnownWidth Fmt (1 + 8 + 23) (by rfl)
abbrev Fam := Configured.Family Fmt (Configured.Code Plan) Plan

def dec (z : Binary32) : Model Fmt :=                       -- the codec, instance pinned
  ExecFloat.ModelCodec.decode (F := Fam) (plan := Plan) (Model := Model Fmt) z
theorem dec_eq (z : Binary32) : dec z = ofUInt32 z.raw.1 := rfl

theorem word32 (a b : Model Fmt) :                           -- binary32 dispatch, unfolded
    Model.AddBackend.word a b =
      match addFiniteImpl? a b with | some s => s | none => Model.AddBackend.generic a b := by
  unfold Model.AddBackend.word; rw [dif_pos (by decide)]; rfl

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y := fun x y => by
  rw [add_eq_word]; exact Configured.Backend.wordAdd_eq_spec x y
```

Your part is `add_eq_word : add x y = Configured.Backend.wordAdd x y`: apply
`ExecFloat.ModelCodec.decode_injective`, rewrite with `decode_liftBinary`, `word32` and `dec_eq`,
and show the word kernel returns your bits (`impl_eq` in `mirror`: unfold `addFiniteImpl?` and your
`core`, `simp` away the excluded cases, `split_ifs <;> rfl`). Useful details when adapting these proofs:

- `ExecFloat.add x y = Configured.Backend.wordAdd x y` holds by `rfl`; use it on the fallback branch.
- `Binary32` and `Configured.NativeFPU.ofBinary32Bits` spell the format two ways (`Binary.format 8 23` and `binary32`), equal by definition but not syntactically. Wrap the constructor, `@[inline] def ofBits (w : UInt32) : Binary32 := Configured.NativeFPU.ofBinary32Bits w`, or `rw` and `split` refuse the goal as ill-typed.
- Name your fallback test (`special a b : Bool`) and `by_cases` on it; `split` picks the first `match` it finds, often the wrong one.
- Case-split before `decode_injective`, then decode inside the fast branch.

**Changing a helper, not the algorithm** (`fastlog`): prove the new helper equals the old one on
every input, then `rw` it into FloatLib's function. `fastLog2_eq : fastLog2 x = x.log2`
uses `Nat.log2_eq_iff`, one step lemma used six times, and `omega`. `tuned` adds
similar lemmas: `sp1_eq` (the one-compare test is the four comparisons, by moving to
`toNat` and letting `omega` handle the shifts and masks), `rseAny_eq` (a word shift rounds like the
natural-number one) and `mkW_ofNat` (packing on words is `mkBits`, which only reads its fields
through `UInt32.ofNat`).

**Route 2: a new algorithm down to the dyadic model.** Rewrite with `addFiniteImpl_eq` and
`addFinite_eq`, and prove your bits equal `roundDyadic (addDyadic dx dy)` on the fast path. This is
how FloatLib proves its own kernel and the kind of argument needed for a different rounding algorithm.

Tactics that pass the axioms stage: `simp`, `omega`, `grind`, `decide`, `norm_num`,
`interval_cases`, `rfl`, and everything else in Mathlib that ends in a kernel-checked term.
Rejected: `sorry`, `admit`, `native_decide`, and **`bv_decide`** when it leaves
native axioms. The axiom audit is authoritative: a
tactic is usable only if its resulting proof depends on the allowed axioms.
## Tensor composition

The full reference builds `sum` and `bmm` from its certified scalar functions.
If replacement `add` and `mul` equal the same specifications, their equalities
can be used to rewrite tensor loops back to `sumSpec` and `bmmSpec`.
Keep accumulation order and indexing semantics unchanged unless you prove the
new implementation equivalent for every shape.

Zero shortcuts need particular care: multiplication by zero can involve signed
zero, infinity, or NaN, and adding a signed zero can change a zero accumulator's
sign. A shortcut must be proved for its guarded domain; elsewhere use the
certified fallback.

## Source policy

The gate scans raw source text, including comments. Put proof sketches using
banned words in separate notes rather than in the uploaded file.

| Rule | Purpose |
| --- | --- |
| Only the slot's contract import: `ContractTensor` for full/tensor entries, `Contract` for scalar experiments | Fix the trusted contract |
| No `sorry`, `admit`, `native_decide`, or new `axiom` declarations | Require kernel-checked proofs |
| No `unsafe`, `Unchecked`, or `dbg` prefixes | Prevent purity and certificate escapes |
| No `implemented_by`, `extern`, `export`, `csimp`, or initialization hooks | Prevent compiled-code substitution |
| No file-inclusion commands, `IO`, macros, custom elaborators, or `#` commands | Restrict build-time execution |
| Only allowed attributes and bounded proof options | Preserve checking and resource limits |

Allowed attributes are `inline`, `noinline`, `always_inline`, `specialize`,
`simp`, `reducible`, and `irreducible`. Allowed numeric options are
`maxHeartbeats`, `maxRecDepth`, and `synthInstance.maxHeartbeats`; see the caps and
exact matching rules in [gate.py](../gate.py). The scan is conservative and is
backed by sandboxing, kernel replay, and the axiom audit.

## Gate stages

| Stage | Checks |
| --- | --- |
| `intake` | File exists, size limit, UTF-8 |
| `policy` | Imports, banned constructs, permitted options and attributes |
| `statement` | Compile the entry and the fixed obligation in a sandbox |
| `kernel` | Replay declarations of the submission and verification module with `leanchecker` |
| `axioms` | Proof dependencies contain only `propext`, `Classical.choice`, and `Quot.sound` |
| `score` | Compare checksums and measure against the reference |

Builds and benchmark processes run under bubblewrap with filesystem isolation,
no network, and resource limits. Submitted compiled artifacts are not accepted;
the gate builds from source. A matching benchmark checksum is a finite test,
not a substitute for the proof.

Examples of rejected constructs are in [kernels/exploits](../kernels/exploits).
Their expected gate outcomes are recorded in
[expected.tsv](../kernels/expected.tsv).
