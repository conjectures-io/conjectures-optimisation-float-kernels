# Mining the full entry: FloatLib add and mul, TorchLean sum and bmm - the manual

## Quick Summary

- **One entry, one board.** The paid entry is one `Kernel.lean` with four kernels and their proofs: binary32 `add` and `mul` equal to FloatLib's IEEE spec, and TorchLean's `sum` and batched matrix multiply `bmm` equal to TorchLean's specification, built from that `add` and `mul`. All four are timed together, the ratio is the total time against the incumbent's, and the leader is paid the competition's share (section 15). The champion is the best proven work so far, at 0.56x of FloatLib's own code, so copying the published examples wins nothing. The goal is faster, proven TorchLean kernels; faster FloatLib arithmetic is how you get them. You need not optimise all four: keep the incumbent's code for the rest and one faster kernel can clear the bar, weighed by its share of the time (`bmm` 38%, `add` 23%, `sum` 21%, `mul` 18%).
- **Practice boards.** Every kernel also has its own unpaid board (`--slot add`, `mul`, `fma`, `div`, `sqrt`, `sum`, `bmm`), the quickest way to try one idea. Sections 1 to 11 teach the gate on the `add` board; sections 13 and 14 cover the other kernels; section 15 puts them together.
- You submit one Lean file, `Kernel.lean`: a binary32 addition `Submission.add` and a proof `Submission.add_spec` that it equals FloatLib's reference `ExecFloat.Spec.add` on every pair of inputs, NaNs, infinities and signed zeros included. The gate checks the proof, re-checks it in the kernel, audits axioms, then times your kernel against the incumbent. Lower time wins.
- The incumbent is FloatLib's own certified kernel (`ExecFloat.add`, about 30 to 50 ns per addition depending on the machine). The best proven kernel so far, `kernels/add/tuned`, runs at **0.50x** of its time (`fastlog`, 0.53x); an unproved textbook adder reaches **0.40x**. The gap between those two is what is left to win.
- Setup is one command, `./setup.sh` (about 15 minutes the first time), and `selftest.py` checks the whole environment against 48 expected verdicts in about 30 minutes. Measure before you prove: `gate.py --bench` times an unproved kernel in about 30 seconds. `gate.py` then runs the real six-stage gate.
- The proof is smaller than it looks. Fall back to `ExecFloat.add` for every case you do not handle, and reuse FloatLib's own theorems: `direct`, `mirror`, `fastlog` and `tuned` are four accepted kernels whose proofs add no floating-point reasoning at all, only the plumbing in section 7.
- Four more slots, binary32 `mul`, `fma`, `div` and `sqrt`, run on the same gate with `--slot` (section 13). `kernels/mul/fast` is a proven multiplication at **0.55x**, built almost entirely from `tuned`'s lemmas; nothing faster is proven for `fma`, `div` or `sqrt`.
- The gate, the bench and the submission service are real and tested; the round rules, rewards included, are in section 12.

```bash
./setup.sh                                                  # toolchain, Mathlib cache, build, .venv
export PATH=$HOME/.elan/bin:$PATH
.venv/bin/python selftest.py                                # 48/48 cases as expected = ready
cp -r kernels/add/textbook-unproved my-kernel               # the fast adder, one sorry left
.venv/bin/python gate.py --slot add --bench my-kernel       # speed only, proof not checked
.venv/bin/python gate.py --slot add my-kernel               # the real gate: all six stages
```

## 1. Set up and test the environment

### Requirements

| | |
|---|---|
| OS | Linux x86_64; tested on Ubuntu 26.04 (12 cores, 60 GB RAM) |
| disk | about 14 GB: 11 GB under `lean/.lake` once built (Mathlib 8 GB, FloatLib build 2.6 GB), 2.9 GB for the Lean toolchain in `~/.elan` |
| packages | `sudo apt install python3 python3-venv curl git bubblewrap util-linux` |
| network | only during setup: elan, the toolchain, FloatLib, Mathlib and TorchLean from GitHub, and the Mathlib cache; the gate itself runs offline |
| time | about 15 minutes for a first setup (3 for the Mathlib cache, 12 for the build); seconds afterwards |

The repository holds only the competition: `lean/` is a small Lean workspace whose `lakefile.lean`
pins FloatLib at commit `0d91825` as a dependency. `setup.sh` fetches FloatLib and Mathlib into
`lean/.lake/packages`; nothing in FloatLib is edited. It also fetches TorchLean (about 160 MB of
sources) for the stage-2 draft in `lean/stage2/`; the gate never builds it.

### Install

```bash
git clone <this repo> && cd conjectures-floatlib-competition
./setup.sh --check        # what is present; installs nothing, exit 1 if something is missing
./setup.sh                # elan + Lean v4.34.0, FloatLib + Mathlib, the build, .venv with loguru
export PATH=$HOME/.elan/bin:$PATH
```

`setup.sh` is idempotent: rerun it after an interruption and it continues where it stopped.

### Test the environment

```bash
.venv/bin/python selftest.py     # about 15 minutes
```

The self-test gates every kernel in `kernels/` and compares each verdict with
`kernels/expected.tsv`: the stage it must stop at, and for accepted kernels a ratio range.
It also reruns `exploits/forge` and `exploits/tamper` with policy skipped, to show the stages behind
policy hold on their own. A healthy machine ends with:

```
[selftest] ok   exploits/forge          add  nopolicy
[selftest] ok   exploits/tamper         add  nopolicy
[selftest] ok   mul/incumbent          mul  gate
...
[selftest] ok   fma/wrong              fma  gate
[selftest] 48/48 cases as expected
```

Exit code 0 means the toolchain, the build, the sandbox, the kernel re-check and the timing all
work. Any `FAIL` line says which case and why, and the full log is `logs/selftest-<utc>.log`. A
ratio just outside its range on an accepted kernel usually means a busy machine: close other work
and rerun.

### Variations

| situation | what to do | effect |
|---|---|---|
| bubblewrap missing, or every build fails with a `bwrap` permission error | on Ubuntu 24.04+: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`; otherwise `GATE_SANDBOX=0` | the sysctl lets bwrap create its sandbox; `GATE_SANDBOX=0` runs builds unconfined, fine on your own machine, never on a validator |
| the gate exits 2 with `systemd resource limits are unavailable` | `loginctl enable-linger $USER` (a user systemd outside a login session); on your own machine `GATE_BUILD_MEMORY_MB=0 GATE_BENCH_MEMORY_MB=0` | the gate caps each build and bench in a user systemd scope and refuses to run uncapped by accident |
| no `taskset` | `BENCH_CPU=` | runs unpinned; timing noise grows from under 1% to about 3% |
| a busy core 3 | `BENCH_CPU=5` (any idle core) | same pinning on another core |

Everything runs from the repository root. A submission directory can live anywhere; pass its path.
The gate builds `pinned/<slot>/bench_incumbent`, each slot's incumbent timing binary, on its first
run and rebuilds it whenever the bench, the contract, the lakefile or the toolchain changes.

## 2. What you submit

On the `add` practice board, which these sections use to teach the gate (the paid full entry
holds four such kernels in one file: section 15), one directory with exactly one file, UTF-8 text:

```
my-kernel/
  Kernel.lean    def Submission.add, theorem Submission.add_spec
```

The minimum passing file is the incumbent itself (`kernels/add/incumbent/Kernel.lean`):

```lean
import Contract

namespace Submission

open FloatLib.Floats Competition

def add (x y : Binary32) : Binary32 := ExecFloat.add x y

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y :=
  fun x y => ExecFloat.Proof.add_eq_spec x y

end Submission
```

- `import Contract` is the only import allowed. `Contract` imports all of FloatLib and Mathlib, so every public FloatLib definition and theorem is in scope.
- `Binary32` is `ExecFloat.Binary (exponentBits := 8) (fractionBits := 23)`. Its IEEE bits are `x.raw.1 : UInt32`, and `Configured.NativeFPU.ofBinary32Bits w` builds one from bits (open `FloatLib.Floats.Formats.BinaryInterchange`).
- The names `Submission.add` and `Submission.add_spec` and the statement of `add_spec` are fixed. The gate checks `theorem accepted : Competition.Obligation Submission.add := Submission.add_spec`; a weaker or different statement does not typecheck.
- You may add any helper definitions and lemmas inside `namespace Submission`.

## 3. What to achieve

The score is your kernel's time as a fraction of the incumbent's, over three operand classes of 500,000 pairs each, summed:

| class | operands | why it is there |
|---|---|---|
| `near-one` | random sign and fraction, exponent within 3 of one | where real additions live |
| `wide-gap` | one operand 20 to 30 binades below the other | the sticky-bit and early-return edge |
| `cancel` | same exponent, opposite signs, close fractions | cancellation and exact ties |
| `raw` | random 32-bit patterns: NaNs, infinities, subnormals, zeros | checksummed only, not timed |

Where the ladder stands today, on the reference machine:

| submission | what | verdict | time vs incumbent |
|---|---|---|---|
| `incumbent` | `ExecFloat.add` | accepted | 1.000x (self-noise 0.997 to 1.006) |
| `word` | `Configured.Backend.wordAdd` called directly | accepted | 1.000x: the dispatch layer is free, calling deeper buys nothing |
| `direct` | the word kernel `addFiniteImpl?` on raw bits, skipping the carrier codec | accepted | 0.80x |
| `mirror` | `addFiniteImpl?` step for step on bare words, no `Option` or `Model` | accepted | 0.78x |
| `fastlog` | `mirror` with a binary-search `log2` in the rounding step | **accepted** | 0.53x (near-one 0.61, wide-gap 0.44, cancel 0.57) |
| `tuned` | `fastlog` with a one-compare special test and the rounding step on machine words | **accepted** | **0.50x** (near-one 0.56, wide-gap 0.40, cancel 0.55) |
| `textbook-unproved` | guard/round/sticky adder in `UInt32`/`UInt64`, falls back for specials | rejected, `sorry` | **0.40x** (near-one 0.40, wide-gap 0.27, cancel 0.57) |
| `wrong` | subtraction with the addition theorem | rejected at statement | |

Where the time went, measured on the same seed: the carrier codec and the `Option`/`Model`
wrappers cost about 20% (`direct`, `mirror`); `UInt64.log2`, which the runtime computes one bit at
a time, cost about 30% more (`fastlog`). Removing the `Nat` conversions in the rounding step bought
nothing before `fastlog`; on top of it, together with a one-compare special-operand test, it buys
6.5% (`tuned`, 0.934 of `fastlog`'s time over three paired runs). What is left between 0.50x and
the textbook adder's 0.40x is the algorithm itself: the
certified kernel rounds a 64-bit aligned sum through a general-purpose `roundProduct`, the textbook
adder keeps three guard bits and rounds directly. Beating `fastlog` by a lot needs a new algorithm
and a new rounding proof.

## 4. How to measure

```bash
.venv/bin/python gate.py --slot add --bench my-kernel      # intake, build, score; no policy, no proof check
```

```
[score] near-one  inc 51.63 ns  sub 20.87 ns  ratio 0.404  spread 1.329/1.117
[score] wide-gap  inc 57.48 ns  sub 15.58 ns  ratio 0.271  spread 1.268/1.109
[score] cancel    inc 40.44 ns  sub 22.99 ns  ratio 0.569  spread 1.500/1.050
[score] ratio 0.397 of the incumbent over all timed classes
[bench] textbook-unproved passed=True stage=score ratio=0.397 (UNPROVED - bench only, not a verdict)
```

- `--bench` accepts `sorry`, so you can measure every idea before proving it. A bench run never counts for anything.
- Even in bench mode, a checksum mismatch against the incumbent on any class fails the run: your kernel is wrong somewhere.
- The incumbent and your kernel run as separate processes, alternating over 25 rounds on one CPU. Each round takes the fastest of 3 sweeps, and the score takes the fastest round for each side.
- Knobs: `BENCH_SEED` (fixed operands, for comparing two ideas fairly), `BENCH_ROUNDS` (default 25), `BENCH_PAIRS` (default 500000), `BENCH_CPU` (default 3).
- Every run writes `logs/gate-<utc>.log` and `results/<name>-<mode>-<utc>.json` with per-class numbers, the seed and the pinned incumbent's sha256.

**A matching checksum is not a proof.** While writing `textbook-unproved` we introduced an
exponent-overflow bug (a normalisation carry and a rounding carry together at the top binade
wrapped the exponent into the sign bit). It matched the incumbent's checksum on all four classes,
because no class reaches that edge. The proof is what catches that, and it is why the gate asks for one.

## 5. The gate, stage by stage

| stage | checks | typical rejection |
|---|---|---|
| `intake` | `Kernel.lean` exists, at most 256 KB | `my-kernel/Kernel.lean is missing` |
| `policy` | imports, banned names, allowed options and attributes (section 8) | `banned name 'sorry' at line 54` |
| `statement` | `lake build Verify bench` in a sandbox; the build may write only its own modules | `the obligation does not typecheck: ... Type mismatch` |
| `kernel` | `leanchecker Submission`, `leanchecker Verify`: every declaration replayed through the Lean kernel | `leanchecker rejects Submission` |
| `axioms` | `#print axioms accepted` is `propext`, `Classical.choice`, `Quot.sound` and nothing else | `extra axioms ['Submission.add_spec._native.native_decide.ax_1_1']` |
| `score` | section 3; all four class checksums equal the incumbent's | `checksum mismatch on class cancel` |

Builds run under bubblewrap: read-only filesystem, no network, writes going into a throwaway
overlay layer. Your file is compiled, never trusted.

## 6. Writing a fast kernel

- **Fall back freely.** `if <case you do not handle> then ExecFloat.add x y else <fast path>` costs one branch and removes that case from your proof. `textbook-unproved` handles only normal operands with normal results.
- **Work on bits.** Read `x.raw.1`, compute in `UInt32`/`UInt64`, return `Configured.NativeFPU.ofBinary32Bits bits`. Avoid `Nat`, `Int` and `Array` on the hot path; they allocate.
- **Use `@[inline]`** on the kernel and its helpers. `@[specialize]`, `@[noinline]`, `@[always_inline]` are also allowed.
- **Watch every class.** A shortcut that is fast on `near-one` but slow on `cancel` loses on the sum; `wide-gap` punishes a slow sticky-bit computation.
- **Compare with a fixed seed.** `BENCH_SEED=42` for both runs when you compare two versions of your kernel.
- **Know the runtime.** `UInt64.log2` is a loop over bits; a six-step binary search is 30% of the kernel. Tuples allocate: the same search written with `(r, x)` pairs through a helper ran at 1.41x, slower than the incumbent; two scalar helpers (`sx`, `sr` in `fastlog`) run at full speed. Small `Nat` values are cheap; removing them gained nothing.

## 7. What you must prove, and what you need not

The obligation, pinned in `lean/contract/Contract.lean`:

```lean
def Obligation (kernel : Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y : Binary32, kernel x y = ExecFloat.Spec.add x y
```

The recommended route: prove your kernel equals the incumbent, then reuse the incumbent's theorem.
This skeleton compiles today on `textbook-unproved`, with one `sorry` left:

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

What is **not** available: the 13 helper lemmas behind `addFiniteImpl_eq` (alignment, rounding
under shift, the far-gap case; about 450 lines) are `private`. A kernel with the incumbent's
structure does not need them: reuse the whole theorem. A kernel with a different structure, such
as the textbook adder, re-proves its own versions; that is the two-to-four-week job.

**Route 1: reuse the word kernel's theorem.** This is how all three accepted faster kernels work,
and the plumbing is written, in `kernels/add/direct`, `mirror` and `fastlog`:

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
`core`, `simp` away the excluded cases, `split_ifs <;> rfl`). Four things that cost us hours:

- `ExecFloat.add x y = Configured.Backend.wordAdd x y` holds by `rfl`; use it on the fallback branch.
- `Binary32` and `Configured.NativeFPU.ofBinary32Bits` spell the format two ways (`Binary.format 8 23` and `binary32`), equal by definition but not syntactically. Wrap the constructor, `@[inline] def ofBits (w : UInt32) : Binary32 := Configured.NativeFPU.ofBinary32Bits w`, or `rw` and `split` refuse the goal as ill-typed.
- Name your fallback test (`special a b : Bool`) and `by_cases` on it; `split` picks the first `match` it finds, often the wrong one.
- Case-split before `decode_injective`, then decode inside the fast branch.

**Changing a helper, not the algorithm** (`fastlog`): prove the new helper equals the old one on
every input, then `rw` it into FloatLib's function. `fastLog2_eq : fastLog2 x = x.log2` is about 70 lines
of `Nat.log2_eq_iff`, one step lemma used six times, and `omega`. `tuned` adds three more of the
same kind, 128 lines in all: `sp1_eq` (the one-compare test is the four comparisons, by moving to
`toNat` and letting `omega` handle the shifts and masks), `rseAny_eq` (a word shift rounds like the
natural-number one) and `mkW_ofNat` (packing on words is `mkBits`, which only reads its fields
through `UInt32.ofNat`).

**Route 2: a new algorithm down to the dyadic model.** Rewrite with `addFiniteImpl_eq` and
`addFinite_eq`, and prove your bits equal `roundDyadic (addDyadic dx dy)` on the fast path. This is
how FloatLib proves its own kernel and what the textbook adder needs. Nobody has done it.

Tactics that pass the axioms stage: `simp`, `omega`, `grind`, `decide`, `norm_num`,
`interval_cases`, `rfl`, and everything else in Mathlib that ends in a kernel-checked term.
Rejected: `sorry`, `admit`, `native_decide`, and **`bv_decide`**. Once it reaches the SAT solver it
leaves an axiom named `<theorem>._native.bv_decide.ax_...`, just as `native_decide` does, and Lean
4.34 has no kernel-checked mode for it. It passes only on goals its preprocessing closes before the
solver runs, so do not plan a proof around it.

## 8. What gets you rejected (policy)

The policy runs on the raw text, comments included, so keep these words out of your comments too.

| rule | why |
|---|---|
| every line containing `import` is exactly `import Contract` | `public import` or a second import would reach unchecked code |
| no `sorry`, `admit`, `native_decide`, `axiom` | escape the proof |
| no `unsafe...`, `Unchecked...`, `dbg...` (as prefixes: `unsafeBaseIO` counts) | escape purity or the certificates |
| no `implemented_by`, `extern`, `export`, `csimp`, `init`, `initialize`, `builtin_initialize` | swap compiled code or run code at start-up |
| no `include_str`, `include_bytes`, `includeStr` | read a file at build time; kernels need no file input |
| no `#` commands (`#eval`, `#print`, `#exit`...), `run_cmd`, `run_elab`, `run_meta`, `run_tac`, `macro`, `elab`, `syntax`, `attribute`, `IO`, and no `simproc`/`dsimproc`, `register_...`, `..._elab` or `..._syntax_...` declarations | run your code while the gate elaborates the file |
| `set_option` only `maxHeartbeats` (1 to 4,000,000), `maxRecDepth` (1 to 8192), `synthInstance.maxHeartbeats` (1 to 400,000), as plain digits; options are read with comments removed, and one the policy cannot read is refused | `debug.skipKernelTC` would switch off the kernel, and `0` would let elaboration run forever |
| `@[...]` only `inline`, `noinline`, `always_inline`, `specialize`, `simp`, `reducible`, `irreducible` | `@[command_elab]` and friends register your own elaborator |

`kernels/exploits/*` has one file per rule, each rejected; the two that matter most
(`exploits/forge`, `exploits/tamper`) are also rejected by the later stages when policy is skipped.

## 9. A worked example

```bash
# output abridged to the key lines
cp -r kernels/add/textbook-unproved my-kernel
.venv/bin/python gate.py --slot add --bench my-kernel
#   [bench] my-kernel passed=True stage=score ratio=0.397 (UNPROVED - bench only, not a verdict)
.venv/bin/python gate.py --slot add my-kernel
#   [policy] REJECTED my-kernel: banned name 'sorry' at line 63
# ... prove the fast path, delete the sorry ...
.venv/bin/python gate.py --slot add my-kernel
#   [intake] ok  [policy] ok  [statement] ok  [kernel] ok  [axioms] ok
#   [score] ratio 0.40 of the incumbent over all timed classes
#   [gate] my-kernel passed=True stage=score ratio=0.40
```

## 10. When it fails

| symptom | cause and fix |
|---|---|
| `the obligation does not typecheck` | your theorem's statement differs from the obligation, or the proof has an error; the reason shows the first Lean errors, the full log is in `logs/` |
| `not UTF-8: invalid byte at offset N` | save `Kernel.lean` as UTF-8; the service refuses such a file at upload |
| `checksum mismatch on class X` | your kernel is wrong on some pair of class X; bisect with `BENCH_SEED` and a small `BENCH_PAIRS` |
| `leanchecker rejects Submission` | a declaration does not replay in the kernel; only possible through something the policy should have caught, so report it |
| `extra axioms [...]` | a tactic left an axiom; usually `native_decide` hidden in a Mathlib tactic, or `sorryAx` |
| every build fails with a `bwrap` permission error | see section 1, Variations |
| large `spread` values, ratio moves between runs | the machine is busy; close other work, keep `BENCH_CPU` pinned, raise `BENCH_ROUNDS` |

## 11. Read in this order

1. `lean/contract/Contract.lean` - the obligations, 31 lines.
2. `kernels/add/incumbent/Kernel.lean`, then `kernels/add/fastlog/Kernel.lean` and `kernels/add/tuned/Kernel.lean` - the start and the best proven kernels, with every proof pattern of section 7.
3. `kernels/add/textbook-unproved/Kernel.lean` - the unproved target at 0.40x.
4. `lean/.lake/packages/floatlib/FloatLib/Floats/ExecFloat/Backends/Word/Narrow/Addition/Runtime.lean` - the incumbent's kernel, 80 lines.
5. `.../Word/Narrow/Addition/Proof.lean` - its proof, 563 lines: the argument you will adapt.
6. `.../Dispatch/Add/Runtime.lean` and `Proof.lean` - how specials reach `generic`, and `word_eq_spec`.
7. `lean/bench/Bench.lean` - exactly what is timed.

## 12. Submitting, and the round rules

**Submitting is live.** A validator runs the service in `validator/`, and `miner/submit.py` talks
to it. You need a hotkey registered on the subnet, and the service's URL from the round
announcement. The API is laid out like the conjectures platform's, and the client signs the
platform's message, so the same client works against the conjectures platform's API.

```bash
.venv/bin/pip install -r requirements/miner.txt         # once: the client's signing and HTTP libraries
python miner/submit.py submit my-kernel --hotkey ~/.bittensor/wallets/<w>/hotkeys/<h> --url http://<validator>:9300
#   (the default slot is full, the paid entry; --slot add|mul|... for a practice board)
#   submission 12 (full) queued  digest f5e2f885...
python miner/submit.py status 12 --url http://<validator>:9300
#   submission 12 (full)  state accepted
#   stage score
#   time vs incumbent 0.5547x (lower is faster)
python miner/submit.py report 12 --url http://<validator>:9300    # the gate's full verdict
python miner/submit.py leaderboard --url http://<validator>:9300    # --slot add|mul|... for a practice board
#   rank    ratio qualifies       admission  hotkey
#      1   0.5547       yes          leader  5Grw...  <- leader, paid
```

Each slot is its own competition slug: `floatlib` is the full entry, `floatlib-add`,
`floatlib-mul`, `floatlib-fma`, `floatlib-div`, `floatlib-sqrt`, `floatlib-sum` and `floatlib-bmm` the practice boards. Every endpoint below sits under `/v1/competitions/<slug>`.

| endpoint | what it does |
|---|---|
| `POST …/submissions` | one multipart part, `Kernel.lean`, and three headers: `X-Conjectures-Hotkey`, `X-Conjectures-Timestamp` (unix seconds) and `X-Conjectures-Signature`, an sr25519 signature over the five lines `conjectures-competition-submit-v1` / `competition: <slug>` / `digest: <sha256(Kernel.lean)>` / `hotkey: <ss58>` / `timestamp: <seconds>` (`miner/sig.py`) |
| `GET …/submissions/{id}` | its slot, state (`queued`, `verifying`, `accepted`, `rejected`, or `error` when the gate crashed on it three times: uncharged, and worth reporting), the stage reached, the gate's reason, the ratio and per-class timings, and the verifier fingerprint it was judged by (`verification_current` is false once the validator's gate has changed; the verdict still counts) |
| `GET …/submissions/{id}/report` | the gate's full verdict JSON: every stage it reached and why it stopped |
| `GET …/leaderboard` | the slot's board: the champion first (its ratio is against the champion it took over from), then every other hotkey's best accepted ratio against the current champion (`champion_sha256`), fastest first; `qualifies` marks ratios at or below the promotion bar, `admission` each row's outcome (`leader` for the champion, `qualifies`, `not-significant`, `above-bar`, `no-evidence`), `leader` the champion's submission id (null while the shipped champion holds), and `pays` whether this board pays its author (only the full entry's does) |
| `GET /health` | liveness, queue length, the promotion bar |
| `GET /ready` | the same plus `database`; 503 while the database is unreachable |

What they return, from a live run before the champion was raised (an entry at 0.555 of the baseline it then had; hashes shortened):

```text
POST /v1/competitions/floatlib/submissions                  200
  {"competition": "floatlib", "submission": 2, "slot": "full", "state": "queued",
   "digest": "d4606f82…", "created": true, "slots_remaining": 3}
  (the same file again: the same submission, "created": false)
GET  /v1/competitions/floatlib/submissions/2                200
  {"id": 2, "hotkey": "5Grw…", "slot": "full", "digest": "d4606f82…",
   "submitted_at": "2026-10-08T10:38:28Z", "state": "accepted", "exit_code": 0,
   "stage": "score", "reason": null, "ratio": 0.5552,
   "classes": {"add:near-one": {"incumbent_ns": 44.70, "submission_ns": 29.89, "ratio": 0.669,
               "spread_inc": 1.119, "spread_sub": 1.216}, … twelve types …},
   "seed": 3863216527, "incumbent_sha256": "119f1106…",
   "verifier_fingerprint": "8f2a164a…", "verification_current": true}
GET  /v1/competitions/floatlib/submissions/2/report         200
  {"report": "{\"submission\": \"2\", \"slot\": \"full\", \"mode\": \"gate\", \"stage\": \"score\",
   \"passed\": true, \"reason\": \"\", \"seed\": …, \"ratio\": …, \"classes\": {…},
   \"rounds\": [[incumbent_ns, submission_ns], … 25 rounds …], \"incumbent_sha256\": …,
   \"verifier_fingerprint\": …, \"seconds\": 174.6}"}
  (a rejection: "stage": "statement", "passed": false, "reason": "the obligation does not
   typecheck: …", no ratio, classes or rounds)
GET  /v1/competitions/floatlib/leaderboard                  200
  {"slot": "full", "incumbent_sha256": "119f1106…", "verifier_fingerprint": "8f2a164a…",
   "promotion_bar": 0.97, "leader": 2, "pays": true,
   "ranking": [{"rank": 1, "submission": 2, "hotkey": "5Grw…", "ratio": 0.5552,
                "qualifies": true, "admission": "leader", "submitted_at": "…"}]}
  (/v1/competitions/floatlib-add/leaderboard: the same shape, "slot": "add", "pays": false)
GET  /health    200  {"ok": true, "queued": 0, "promotion_bar": 0.97}
GET  /ready     200  {"ok": true, "queued": 0, "promotion_bar": 0.97, "database": true}
```

A refusal is a 4xx with one reason, `{"detail": {"reason": "…"}}`: 400 for a malformed hotkey,
signature or file; 401 for a stale timestamp or a signature that does not verify; 402 for an
unregistered hotkey or no unspent registration; 404 for an unknown slug or submission; 413 for a
file over 256,000 bytes; 429 past the rate limit.

**What the service enforces:**
- The signature must match the hotkey and this exact file, and its timestamp must be within 5 minutes of the validator's clock, so a captured upload cannot be replayed.
- **One registration buys one accepted submission.** A rejection costs nothing: fix it and resubmit. An acceptance spends one registration; to submit again, register again.
- **Except an entry that holds the gate.** Each entry has a time budget for its own builds, kernel replays and timing runs, 600 seconds in all (a full entry uses about two minutes): one that runs out, or goes over a memory cap (4096 MB to build, 512 MB to time), is rejected *and* spends a registration, so a slow file cannot be resubmitted for free. Check yours with `gate.py` first.
- At most 10 signed writes a minute per hotkey, and at most as many queued submissions as you have unspent registrations.
- The same file from the same hotkey in the same slot is the same submission: resubmitting it returns the same id.
- A signature is bound to its slot's slug: one made for `floatlib` (the full entry) does not verify for `floatlib-add`, and the other way round, nor for any other competition on the platform.
- Registrations are shared across slots: one registration buys one accepted submission in any slot. (Two meanings of "slot" meet here: a *registration slot* is what you spend, a *competition slot* is `full` or one of the practice boards.)
- **One board pays: the full entry.** Its champion's author receives the competition's share; while the shipped champion holds, the share goes to the treasury. The practice boards are ranked and shown, but unpaid.
- Each submission runs this exact gate on the validator's pinned machine and CPU, on a fresh seed.
- **A verdict keeps counting after a gate update.** Every verdict records the verifier fingerprint and incumbent it was judged by, as provenance; when the validator updates its gate, contract or bench, the change is logged and older accepted submissions stay on the board. The operator may choose to re-verify them, at no cost to you: a re-acceptance reuses the registration already spent.
- **A promotion starts a fresh board.** Every verdict also records the champion it was measured against (`champion_sha256`, the hash of the champion's `Kernel.lean`). After a promotion, the board counts only ratios against the new champion, since older ones were measured against a slower one, and every later entry is timed against the new champion.

**Round rules:**
- **Every entry is timed against the current champion,** the best submission so far in its slot; for the full entry, `kernels/full/incumbent` until a submission beats it.
- **Promotion is automatic: beat the champion by 3%, with evidence.** An accepted entry becomes the new champion when its ratio is 0.97 or lower *and* its 5th-percentile bootstrapped gain over the champion is positive. The gate records every round's paired champion and submission timings, and the validator bootstraps them (2000 draws, a seed derived from the timings themselves, so every validator agrees). So a copy of the champion, or the same kernel with one lucky round, is never promoted; a genuinely faster kernel is, and the next entry must beat it by 3%. The one-hour timing study shows noise under 1% per run, so 3% leaves margin. This is the admission test the DEFLATE competition uses.
- **Emission:** as in the DEFLATE competition, the validator's treasury takes 80% and the competition 20%; the full entry's champion's author receives the competition's share, and while the shipped champion holds, it goes to the treasury.

## 13. The other slots: multiplication, fused multiply-add, division and square root

Everything above applies, with these names swapped in. Pass `--slot` to the gate; the default is `add`.

| slot | kernel you define | theorem you prove | gate statement | incumbent |
|---|---|---|---|---|
| `add` | `Submission.add x y` | `add_spec : ∀ x y, add x y = ExecFloat.Spec.add x y` | `Obligation` | `ExecFloat.add` |
| `mul` | `Submission.mul x y` | `mul_spec : ∀ x y, mul x y = ExecFloat.Spec.mul x y` | `MulObligation` | `ExecFloat.mul` |
| `fma` | `Submission.fma x y z` | `fma_spec : ∀ x y z, fma x y z = ExecFloat.Spec.fma x y z` | `FmaObligation` | `ExecFloat.fma` |
| `div` | `Submission.div x y` | `div_spec : ∀ x y, div x y = ExecFloat.Spec.div x y` | `DivObligation` | `ExecFloat.div` |
| `sqrt` | `Submission.sqrt x` | `sqrt_spec : ∀ x, sqrt x = ExecFloat.Spec.sqrt x` | `SqrtObligation` | `ExecFloat.sqrt` |

```bash
.venv/bin/python gate.py --slot mul --bench my-mul          # time an unproved multiplication
.venv/bin/python gate.py --slot fma kernels/fma/word         # the full gate on the fma slot
.venv/bin/python gate.py --slot sqrt kernels/sqrt/incumbent  # the square-root slot's incumbent
```

`ExecFloat.Spec.fma` rounds once: `x * y + z` computed exactly, then rounded. Multiplying,
rounding and adding is a different function, and `kernels/fma/wrong` shows it fails to prove.
FloatLib's word kernels `Configured.Backend.wordMul`, `wordFma`, `wordDiv` and `wordSqrt`, with
their `_eq_spec` theorems, play the role `wordAdd` plays in section 7. `kernels/div/wrong`
(multiplying instead) and `kernels/sqrt/wrong` (returning the input) show the statement stage
refusing a kernel that is not the operation.

Each slot times its own operand classes, 500,000 calls each, summed:

| slot | class | operands |
|---|---|---|
| `mul` | `near-one` | both factors within 3 binades of one |
| `mul` | `spread` | exponents over the whole normal range: overflow, underflow and normal products |
| `mul` | `tiny` | products in the subnormal range |
| `fma` | `near-one` | all three near one, a dot product's accumulate step |
| `fma` | `cancel` | a negative addend the size of the product: cancellation |
| `fma` | `wide` | an addend 20 to 40 binades from the product, either side |
| `div` | `near-one` | both operands within 3 binades of one |
| `div` | `spread` | exponents over the whole normal range: overflow, underflow and normal quotients |
| `div` | `extremes` | tiny over huge and huge over tiny, alternately: subnormal quotients and just past overflow |
| `sqrt` | `near-one` | positive values within 3 binades of one |
| `sqrt` | `spread` | positive values over the whole normal range, odd and even exponents |
| `sqrt` | `subnormal` | positive subnormal inputs, which need normalising first |

**`mul/fast` (0.55x, accepted):** FloatLib's `mulFiniteImpl?` on bare words with `tuned`'s
`roundP`. All three slots round through the same `roundProduct`, and its `UInt64.log2` bit loop runs
up to 47 times on a 48-bit product, so the fast `log2` pays even more here than in `add`. The
special test is simpler: multiplication falls back only on NaN and infinity (`a <<< 1 ≥ 0xff000000`,
lemma `sm1_eq`), because the rounding step already signs a zero product. Its proof is `tuned`'s
`log2` and rounding lemmas unchanged plus the `mul` versions of `word32`, `dec_word` and `impl_eq`:
328 lines, no floating-point reasoning. The same `roundP` swap applies to `fma`'s word path.

Where to look first in `fma`: FloatLib's fma takes about 250 ns on the `wide` class against about 65 ns on
the other two, so the far-addend path is the obvious target. The summed score weights classes by
the incumbent's time, so `wide` is about two thirds of the fma score.

`div` and `sqrt` are new: their incumbents are FloatLib's certified kernels, about 50 ns a call on
every class on the reference machine, and no faster proven kernel exists. Both carry the
`Option`/`Model` wrappers and the carrier codec `direct` and `mirror` removed for `add`. `sqrt`'s
word kernel also normalises with the bit-at-a-time `UInt64.log2` that `fastlog` replaced, so that
lemma is the first thing to try there; `div` takes its own quotient path, where nobody has
measured where the time goes.

## 14. The TorchLean kernels: `sum` and `bmm`

The heart of the full entry (section 15), each with its own practice board. Everything above applies; what changes is the shape of the kernel, the module you
import, and the rule you must meet.

| slot | kernel you define | theorem you prove | gate statement | incumbent |
|---|---|---|---|---|
| `sum` | `Submission.sum {n} (x : Vector Binary32 n) : Binary32` | `sum_spec : ∀ n (x : Vector Binary32 n), sum x = sumSpec x.get` | `SumObligation` | left fold of `ExecFloat.add` |
| `bmm` | `Submission.bmm (batch m n p : Nat) (A : Vector Binary32 (batch*m*n)) (B : Vector Binary32 (batch*n*p)) : Vector Binary32 (batch*m*p)` | `bmm_spec : ∀ batch m n p A B, (bmm batch m n p A B).get = bmmSpec batch m n p A.get B.get` | `BmmObligation` | `ExecFloat.add`/`mul` in `bmmSpec`'s loop |

```bash
.venv/bin/python gate.py --slot bmm --bench my-bmm     # time an unproved matmul
.venv/bin/python gate.py --slot bmm kernels/bmm/fast   # the full gate on the bmm slot
python miner/submit.py submit my-bmm --hotkey <file> --url <validator> --slot bmm
```

**The rules are TorchLean's.** A `sum` or `bmm` submission imports `ContractTensor` and nothing
else (it brings `Contract` with it). Its `sumSpec` is TorchLean's `reduceSumLeftSpec`: add the
elements left to right from +0 with exact IEEE addition. Its `bmmSpec` is TorchLean's `bmmSpec`:
row-major batched matrix multiply, every output `C[b,i,j]` a left fold over `k` from +0 of
`add acc (mul A[b,i,k] B[b,k,j])`, reading out-of-range indices as +0 (`getD`).
`lean/stage2/Stage2/Link.lean` proves both are TorchLean's own definitions, so a kernel the gate
accepts is proved against TorchLean itself. The order is part of the rule: every addition
rounds, so another order gives other bits, and `kernels/sum/wrong` and `kernels/bmm/wrong` (the same
values summed backwards) cannot be proved.

**Timing.** Each sweep is 500,000 scalar steps, as for every slot:

| slot | shape | classes |
|---|---|---|
| `sum` | 500 vectors of 1,000 elements | `dense` (near one, random signs), `relu` (about half +0, rest positive), `wide` (random signs over 50 binades) |
| `bmm` | 2 batches of 50×100 by 100×50 matrices (500,000 multiply-adds) | `dense` (both near one), `relu` (`A` half +0, as activations after a ReLU), `wide` (both over 50 binades) |

**The ladder so far.**

| kernel | ratio | how |
|---|---|---|
| `sum/incumbent`, `bmm/incumbent` | 1.00x | FloatLib's `add`/`mul` in TorchLean's loop |
| `sum/tuned` | **0.52x** | stage 1's `tuned` addition copied in with its proof, a ten-line proof on top |
| `bmm/fast` | **0.57x** | `tuned` and `mul/fast` copied in, each in its own namespace; proof by composition |

To build on a stage-1 winner, copy it into your file, in a namespace of its own if you use two,
as `kernels/bmm/fast` does: you may import only `ContractTensor`.

**Where the room is.** The stage-1 kernels are weakest on zeros: `bmm/fast` reaches 0.52x on
`dense` but only 0.68x on `relu`, `sum/tuned` 0.47x against 0.71x. Real network data is full of
zeros, and an unproved matmul that skips `acc + (±0 · b)` measured about 0.48x of the proved one
on ReLU data. Such a shortcut is allowed if you prove it exact: careful, `-0 + +0 = +0` in IEEE, so
a skip is only safe when `b` is finite and `acc` is finite and nonzero. Faster scalar kernels from
stage 1 carry over directly, too.

**Rewards.** `sum` and `bmm` are practice boards; they pay as part of a full entry (section 15).

## 15. The full entry: the one paid board

Everything comes together here. One `Kernel.lean`, `import ContractTensor`, defining all four
kernels with their proofs, under the names the earlier sections use:

| kernel | theorem | rule |
|---|---|---|
| `Submission.add` | `add_spec` | FloatLib's `ExecFloat.Spec.add` (section 2) |
| `Submission.mul` | `mul_spec` | FloatLib's `ExecFloat.Spec.mul` (section 13) |
| `Submission.sum` | `sum_spec` | TorchLean's `reduceSumLeftSpec` (section 14) |
| `Submission.bmm` | `bmm_spec` | TorchLean's `bmmSpec` (section 14) |

The gate proves them in one statement,
`Obligation Submission.add ∧ MulObligation Submission.mul ∧ SumObligation Submission.sum ∧
BmmObligation Submission.bmm`, so one wrong kernel refuses the whole entry
(`kernels/full/wrong` sums backwards and is refused at the statement stage).

```bash
cp -r kernels/full/incumbent my-entry                  # the champion: start here, beat it
.venv/bin/python gate.py --bench my-entry              # time it (full is the default slot)
.venv/bin/python gate.py my-entry                      # the real gate, about 3 minutes
python miner/submit.py submit my-entry --hotkey <file> --url <validator>
```

**The champion.** `kernels/full/incumbent` is the best proven work so far, not FloatLib's own
code: `tuned` addition and `mul/fast` multiplication with their proofs, and TorchLean's `sum` and
`bmm` built from them. It runs at 0.56x of `kernels/full/baseline` (FloatLib's own `add` and `mul`
in TorchLean's loops), so a copy of the published examples scores 1.00 and wins nothing.

**Scoring.** Twelve timed input types, three per kernel and the same as the practice boards':
`add:near-one`, `add:wide-gap`, `add:cancel`, `mul:near-one`, `mul:spread`, `mul:tiny`,
`sum:dense`, `sum:relu`, `sum:wide`, `bmm:dense`, `bmm:relu`, `bmm:wide`, each 500,000 steps,
plus an untimed raw set per kernel. The ratio is your total time over the champion's total, so
each kernel counts by the time it takes, and the 3% bar and statistical admission apply to the
total as on every board. Each kernel's share of the champion's time, from a full gate run on the
reference machine, and how much faster that kernel alone must get to clear the 0.97 bar:

| kernel | champion ns per step, three types summed | share of the total | alone, needs |
|---|---|---|---|
| `add` | about 76 | 23% | 0.87x of the champion's `add` |
| `mul` | about 58 | 18% | 0.83x |
| `sum` | about 68 | 21% | 0.86x |
| `bmm` | about 125 | 38% | 0.92x |

**How to win.** An entry becomes the champion, and its author is paid, when:
1. it is correct: all four proofs pass the gate, and every output matches the champion's bit for
   bit on fresh random inputs;
2. its total ratio against the current champion is 0.97 or lower;
3. the bootstrap over its 25 paired rounds shows it beats the champion even at the 5th percentile:
   a copy of the champion, or one lucky run, is never promoted.

It stays champion, and paid, until an entry beats it by 3% in turn.

**You need not optimise all four.** Keep the champion's code and proofs for the kernels you leave
alone and replace one: your ratio is then about `1 - share × (1 - that kernel's ratio)`, which is
where the last column of the table comes from (estimates from the shares, not gate runs).

**Build `sum` and `bmm` from your own `add` and `mul`.** Nothing forces it, but it is how a better
`add` or `mul` pays twice: once on its own input types, and again inside `sum` and `bmm`, which
spend most of their time in it. The proofs carry over: once `add = ExecFloat.add` and
`mul = ExecFloat.mul` (from `add_spec` and `mul_spec`), the `sum` and `bmm` proofs are the
champion's unchanged; `kernels/full/incumbent` shows the pattern, with `tuned` and `mul/fast` each in
a namespace of its own.

**The ladder so far.**

| entry | ratio against the shipped champion |
|---|---|
| `kernels/full/baseline`: FloatLib's `add`/`mul`, TorchLean's loops | about 1.8x |
| `kernels/full/incumbent`: `tuned` and `mul/fast`, the same loops on top | **1.00x**: 0.97 or lower takes over |

**Where to look.** Everything in sections 6, 7, 13 and 14: a faster `add` or `mul` (below 0.50x
needs a new rounding argument), and exact shortcuts in `sum` and `bmm` on zero-heavy data
(proved, as section 14 warns about signed zeros). Try each idea on its practice board first; a
full entry takes about three minutes to check against one minute for a single kernel.

