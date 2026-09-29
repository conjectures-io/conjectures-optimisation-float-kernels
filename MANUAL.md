# Mining the FloatLib binary32 addition slot - the manual

## Quick Summary

- You submit one Lean file, `Kernel.lean`: a binary32 addition `Submission.add` and a proof `Submission.add_spec` that it equals FloatLib's reference `ExecFloat.Spec.add` on every pair of inputs, NaNs, infinities and signed zeros included. The gate checks the proof, re-checks it in the kernel, audits axioms, then times your kernel against the incumbent. Lower time wins.
- The incumbent is FloatLib's own certified kernel (`ExecFloat.add`, about 30 to 50 ns per addition depending on the machine). The best proven kernel so far, `submissions/tuned`, runs at **0.50x** of its time (`fastlog`, 0.53x); an unproved textbook adder reaches **0.40x**. The gap between those two is what is left to win.
- Setup is one command, `./setup.sh` (about 15 minutes the first time), and `selftest.py` checks the whole environment against 26 expected verdicts in about 9 minutes. Measure before you prove: `gate.py --bench` times an unproved kernel in about 30 seconds. `gate.py` then runs the real six-stage gate.
- The proof is smaller than it looks. Fall back to `ExecFloat.add` for every case you do not handle, and reuse FloatLib's own theorems: `direct`, `mirror`, `fastlog` and `tuned` are four accepted kernels whose proofs add no floating-point reasoning at all, only the plumbing in section 7.
- Two more slots, binary32 `mul` and `fma`, run on the same gate with `--slot mul|fma` (section 13). `submissions/mul-fast` is a proven multiplication at **0.55x**, built almost entirely from `tuned`'s lemmas; nothing faster is proven for `fma` yet.
- Status: the gate, the bench and the submission service are real and tested; the rewards and the exact promotion bar are proposed, not final (section 12).

```bash
./setup.sh                                                  # toolchain, Mathlib cache, build, .venv
export PATH=$HOME/.elan/bin:$PATH
.venv/bin/python selftest.py                                # 26/26 cases as expected = ready
cp -r submissions/textbook-unproved my-kernel               # the fast adder, one sorry left
.venv/bin/python gate.py --bench my-kernel                  # speed only, proof not checked
.venv/bin/python gate.py my-kernel                          # the real gate: all six stages
```

## 1. Set up and test the environment

### Requirements

| | |
|---|---|
| OS | Linux x86_64; tested on Ubuntu 26.04 (12 cores, 60 GB RAM) |
| disk | about 14 GB: 11 GB under `lean/.lake` once built (Mathlib 8 GB, FloatLib build 2.6 GB), 2.9 GB for the Lean toolchain in `~/.elan` |
| packages | `sudo apt install python3 python3-venv curl git bubblewrap util-linux` |
| network | only during setup: elan, the toolchain, FloatLib and Mathlib from GitHub, and the Mathlib cache; the gate itself runs offline |
| time | about 15 minutes for a first setup (3 for the Mathlib cache, 12 for the build); seconds afterwards |

The repository holds only the competition: `lean/` is a small Lean workspace whose `lakefile.lean`
pins FloatLib at commit `0d91825` as a dependency. `setup.sh` fetches FloatLib and Mathlib into
`lean/.lake/packages`; nothing in FloatLib is edited.

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
.venv/bin/python selftest.py     # about 9 minutes
```

The self-test gates every fixture in `submissions/` and compares each verdict with
`submissions/expected.tsv`: the stage it must stop at, and for accepted kernels a ratio range.
It also reruns `exploit-forge` and `exploit-tamper` with policy skipped, to show the stages behind
policy hold on their own. A healthy machine ends with:

```
[selftest] ok   exploit-forge          add  nopolicy
[selftest] ok   exploit-tamper         add  nopolicy
[selftest] ok   mul-incumbent          mul  gate
...
[selftest] ok   fma-wrong              fma  gate
[selftest] 26/26 cases as expected
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

One directory with exactly one file, UTF-8 text:

```
my-kernel/
  Kernel.lean    def Submission.add, theorem Submission.add_spec
```

The minimum passing file is the incumbent itself (`submissions/incumbent/Kernel.lean`):

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
.venv/bin/python gate.py --bench my-kernel      # intake, build, score; no policy, no proof check
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
and the plumbing is written, in `submissions/direct`, `mirror` and `fastlog`:

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
how FloatLib proves its own kernel and what the textbook adder needs. Nobody has done it yet.

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
| no `#` commands (`#eval`, `#print`, `#exit`...), `run_cmd`, `run_elab`, `run_meta`, `macro`, `elab`, `syntax`, `attribute`, `IO` | run your code while the gate elaborates the file |
| `set_option` only `maxHeartbeats`, `maxRecDepth`, `synthInstance.maxHeartbeats` | `debug.skipKernelTC` would switch off the kernel |
| `@[...]` only `inline`, `noinline`, `always_inline`, `specialize`, `simp`, `reducible`, `irreducible` | `@[command_elab]` and friends register your own elaborator |

`submissions/exploit-*` has one file per rule, each rejected; the two that matter most
(`exploit-forge`, `exploit-tamper`) are also rejected by the later stages when policy is skipped.

## 9. A worked example

```bash
# output abridged to the key lines
cp -r submissions/textbook-unproved my-kernel
.venv/bin/python gate.py --bench my-kernel
#   [bench] my-kernel passed=True stage=score ratio=0.397 (UNPROVED - bench only, not a verdict)
.venv/bin/python gate.py my-kernel
#   [policy] REJECTED my-kernel: banned name 'sorry' at line 63
# ... prove the fast path, delete the sorry ...
.venv/bin/python gate.py my-kernel
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

1. `lean/contract/Contract.lean` - the obligation, 23 lines.
2. `submissions/incumbent/Kernel.lean`, then `submissions/fastlog/Kernel.lean` and `submissions/tuned/Kernel.lean` - the start and the best proven kernels, with every proof pattern of section 7.
3. `submissions/textbook-unproved/Kernel.lean` - the unproved target at 0.40x.
4. `lean/.lake/packages/floatlib/FloatLib/Floats/ExecFloat/Backends/Word/Narrow/Addition/Runtime.lean` - the incumbent's kernel, 80 lines.
5. `.../Word/Narrow/Addition/Proof.lean` - its proof, 563 lines: the argument you will adapt.
6. `.../Dispatch/Add/Runtime.lean` and `Proof.lean` - how specials reach `generic`, and `word_eq_spec`.
7. `lean/bench/Bench.lean` - exactly what is timed.

## 12. Submitting, and the round rules

**Submitting is live.** A validator runs the service in `validator/`, and `miner/submit.py` talks
to it. You need a hotkey registered on the subnet, and the service's URL from the round
announcement.

```bash
.venv/bin/pip install -r requirements-service.txt      # once: the client's signing and HTTP libraries
python miner/submit.py submit my-kernel --hotkey ~/.bittensor/wallets/<w>/hotkeys/<h> --url http://<validator>:9300
#   (add --slot mul or --slot fma for the other slots; the default is add)
#   submission 12 queued  digest f5e2f885...
python miner/submit.py status 12 --url http://<validator>:9300
#   submission 12  state accepted
#   stage score
#   time vs incumbent 0.5347x (lower is faster)
python miner/submit.py leaderboard --url http://<validator>:9300    # --slot mul|fma for the others
#   rank    ratio qualifies       admission  hotkey
#      1   0.5347       yes          leader  5Grw...  <- paid
```

| endpoint | what it does |
|---|---|
| `POST /submit` | multipart `Kernel.lean` plus `hotkey`, `signature`, `timestamp` and an optional `slot` (`add`, `mul`, `fma`; default `add`); the client signs `submit:<sha256(Kernel.lean)>:<hotkey>:<timestamp>` for `add` and `submit:<slot>:<sha256>:<hotkey>:<timestamp>` for the others, with sr25519 |
| `GET /submissions/{id}` | its slot, state (`queued`, `verifying`, `accepted`, `rejected`, or `error` when the gate crashed on it three times: uncharged, and worth reporting), the stage reached, the gate's reason, the ratio and per-class timings, and the verifier fingerprint it was judged by (`verification_current` is false once the validator's gate has changed) |
| `GET /leaderboard?slot=add` | one slot's board: every hotkey's best accepted ratio against that slot's current incumbent, fastest first; `qualifies` marks ratios at or below the promotion bar, `admission` each row's statistical admission outcome (`leader`, `not-significant`, `above-bar`, `no-evidence`), and `leader` the one submission paid |
| `GET /health` | liveness, queue length, the promotion bar |

**What the service enforces:**
- The signature must match the hotkey and this exact file, and its timestamp must be within 5 minutes of the validator's clock, so a captured upload cannot be replayed.
- **One registration buys one accepted submission.** A rejection costs nothing: fix it and resubmit. An acceptance spends one registration; to submit again, register again.
- At most 10 signed writes a minute per hotkey, and at most as many queued submissions as you have unspent registrations.
- The same file from the same hotkey in the same slot is the same submission: resubmitting it returns the same id.
- A signature is bound to its slot: one made for `add` does not verify for `mul`, and the other way round.
- Registrations are shared across slots: one registration buys one accepted submission in any slot. (Two meanings of "slot" meet here: a *registration slot* is what you spend, a *competition slot* is `add`, `mul` or `fma`.)
- **Only the `add` board pays for now.** `mul` and `fma` are gated, ranked and shown, but the weight setter scores `add` until the rewards say how emission splits across slots.
- Each submission runs this exact gate on the validator's pinned machine and CPU, on a fresh seed.
- **A verdict counts only under the verifier that gave it.** When the validator updates its gate, contract or bench, older accepted submissions leave the board until the operator re-verifies them, at no cost to you: a re-acceptance reuses the registration already spent.

**Round rules (proposed, not final):**
- **Promotion:** an accepted kernel at least 3% faster than the current incumbent over all timed classes becomes the new incumbent (the operator repins it). The one-hour timing study shows noise under 1% per run, so 3% leaves margin.
- **Who leads: statistical admission.** The headline ratio ranks the board; the lead needs evidence as well. The gate records every round's paired incumbent and submission timings, and the validator bootstraps them (2000 draws, a seed derived from the timings themselves, so every validator agrees). A submission qualifies when its ratio is at or below the bar *and* its 5th-percentile gain over the incumbent is positive. Taken in submission order, a qualifying submission takes the lead only if its 5th-percentile gain over the current leader is positive too. So a later copy of the leader, or the same kernel with one lucky round, cannot take the lead; a genuinely faster kernel can. This is the rule the DEFLATE competition uses.
- **Emission:** a placeholder until rewards are announced. The admitted leader's hotkey receives the winner share and the rest burns.
- **Later slots** use the same shape, one obligation per operation and format: `Spec.mul`, `Spec.div`, `Spec.sqrt`, `Spec.fma`, and binary64.

## 13. The multiplication and fused multiply-add slots

Everything above applies, with these names swapped in. Pass `--slot` to the gate; the default is `add`.

| slot | kernel you define | theorem you prove | gate statement | incumbent |
|---|---|---|---|---|
| `add` | `Submission.add x y` | `add_spec : ∀ x y, add x y = ExecFloat.Spec.add x y` | `Obligation` | `ExecFloat.add` |
| `mul` | `Submission.mul x y` | `mul_spec : ∀ x y, mul x y = ExecFloat.Spec.mul x y` | `MulObligation` | `ExecFloat.mul` |
| `fma` | `Submission.fma x y z` | `fma_spec : ∀ x y z, fma x y z = ExecFloat.Spec.fma x y z` | `FmaObligation` | `ExecFloat.fma` |

```bash
.venv/bin/python gate.py --slot mul --bench my-mul          # time an unproved multiplication
.venv/bin/python gate.py --slot fma submissions/fma-word     # the full gate on the fma slot
```

`ExecFloat.Spec.fma` rounds once: `x * y + z` computed exactly, then rounded. Multiplying,
rounding and adding is a different function, and `submissions/fma-wrong` shows it fails to prove.
FloatLib's word kernels `Configured.Backend.wordMul` and `wordFma`, with `wordMul_eq_spec` and
`wordFma_eq_spec`, play the role `wordAdd` plays in section 7.

Each slot times its own operand classes, 500,000 calls each, summed:

| slot | class | operands |
|---|---|---|
| `mul` | `near-one` | both factors within 3 binades of one |
| `mul` | `spread` | exponents over the whole normal range: overflow, underflow and normal products |
| `mul` | `tiny` | products in the subnormal range |
| `fma` | `near-one` | all three near one, a dot product's accumulate step |
| `fma` | `cancel` | a negative addend the size of the product: cancellation |
| `fma` | `wide` | an addend 20 to 40 binades from the product, either side |

**`mul-fast` (0.55x, accepted):** FloatLib's `mulFiniteImpl?` on bare words with `tuned`'s
`roundP`. All three slots round through the same `roundProduct`, and its `UInt64.log2` bit loop runs
up to 47 times on a 48-bit product, so the fast `log2` pays even more here than in `add`. The
special test is simpler: multiplication falls back only on NaN and infinity (`a <<< 1 ≥ 0xff000000`,
lemma `sm1_eq`), because the rounding step already signs a zero product. Its proof is `tuned`'s
`log2` and rounding lemmas unchanged plus the `mul` versions of `word32`, `dec_word` and `impl_eq`:
328 lines, no floating-point reasoning. The same `roundP` swap applies to `fma`'s word path.

Where to look first in `fma`: FloatLib's fma takes about 250 ns on the `wide` class against about 65 ns on
the other two, so the far-addend path is the obvious target. The summed score weights classes by
the incumbent's time, so `wide` is about two thirds of the fma score.

