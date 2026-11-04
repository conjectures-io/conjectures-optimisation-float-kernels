import ExactSuite
import ExactToolkit.ModelProofs
import Lean

/-! Structural audit of the actual generated arrays. No inference or timing.

Each layer's `dims`, in order (booleans as 0 or 1):
* `linear`: rows, inner, outputs; `gemm`: m, k, n; `scaleShift`: channels, hw;
  `rmsnorm`/`layernorm`: rows, n;
* `conv2d`/`depthwise`: cin, h, w, cout, kh, kw, strideH, strideW, padT, padL, padB, padR,
  dilH, dilW, groups (the shape as given; depthwise ignores its cout and groups);
* `convTranspose`: the convolution's fifteen, then outPadH, outPadW;
* `maxpool`/`avgpool`: c, h, w, kh, kw, strideH, strideW, padT, padL, padB, padR, ceilMode,
  countIncludePad. -/
open ExactRuntime ExactSuite Lean

/-- A convolution shape's fields, in the order above. -/
def convDims (s : ConvShape) : Array Nat :=
  #[s.cin, s.h, s.w, s.cout, s.kh, s.kw, s.strideH, s.strideW, s.padT, s.padL, s.padB, s.padR,
    s.dilH, s.dilW, s.groups]

/-- A pooling shape's fields, in the order above. -/
def poolDims (s : PoolShape) : Array Nat :=
  #[s.c, s.h, s.w, s.kh, s.kw, s.strideH, s.strideW, s.padT, s.padL, s.padB, s.padR,
    if s.ceilMode then 1 else 0, if s.countIncludePad then 1 else 0]

def shapeRecord (l : Layer) (shape : Nat × Nat) : Json := Id.run do
  let (op, dims, weights, bias, finite, epsilon) := match l with
    | .linear r i o _ w b => ("linear", #[r,i,o], w.size, b.size,
        w.all finiteWord && b.all finiteWord, 0)
    | .gemm m k n a => ("gemm", #[m,k,n], a.size, 0, a.all finiteWord, 0)
    | .conv s _ w b => ("conv2d", convDims s, w.size, b.size,
        w.all finiteWord && b.all finiteWord, 0)
    | .depthwise s _ w b => ("depthwise", convDims s, w.size, b.size,
        w.all finiteWord && b.all finiteWord, 0)
    | .convTranspose s _ w b => ("convTranspose",
        convDims s.toConvShape ++ #[s.outPadH, s.outPadW], w.size, b.size,
        w.all finiteWord && b.all finiteWord, 0)
    | .bn _ c hw s t => ("scaleShift", #[c,hw], s.size, t.size,
        s.all finiteWord && t.all finiteWord, 0)
    | .maxpool s => ("maxpool", poolDims s, 0, 0, true, 0)
    | .avgpool s => ("avgpool", poolDims s, 0, 0, true, 0)
    | .rmsnorm r n eps g => ("rmsnorm", #[r,n], g.size, 0,
        g.all finiteWord && finiteWord eps && rationalWord eps > 0, eps.toNat)
    | .layernorm r n eps g b => ("layernorm", #[r,n], g.size, b.size,
        g.all finiteWord && b.all finiteWord && finiteWord eps && rationalWord eps > 0, eps.toNat)
    | .save => ("save", #[], 0, 0, true, 0)
    | .addSaved _ => ("addSaved", #[], 0, 0, true, 0)
  return Json.mkObj [("op", toJson op), ("dims", toJson dims),
    ("weights", toJson weights), ("bias", toJson bias), ("parameters_finite", toJson finite),
    ("eps", toJson epsilon), ("input", toJson shape.1), ("saved", toJson shape.2),
    ("output", toJson (l.shapeStep shape).1), ("saved_after", toJson (l.shapeStep shape).2),
    ("valid", toJson (decide (l.acceptsShape shape)))]

def main (args : List String) : IO Unit := do
  for arg in args do
    let seed := arg.toNat!.toUInt32
    for c in cells do
      let p := prepareCell c seed
      if p.samples == 0 || p.size == 0 || p.input.size != p.samples * p.size ||
          !p.input.all finiteWord then
        throw (IO.userError s!"invalid generated model input: {seed}:{c.name}")
      let mut shape := (p.size, 0)
      let mut trace : Array Json := #[]
      for l in p.layers do
        trace := trace.push (shapeRecord l shape)
        if !(decide (l.acceptsShape shape)) then
          throw (IO.userError s!"invalid generated layer shape: {seed}:{c.name}")
        shape := l.shapeStep shape
      IO.println <| (Json.mkObj [("seed", toJson seed.toNat), ("cell", toJson c.name),
        ("samples", toJson p.samples), ("input_size", toJson p.size),
        ("input_length", toJson p.input.size), ("output_size", toJson shape.1),
        ("trace", toJson trace)]).compress
