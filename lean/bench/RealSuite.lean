import ExactSuite

/-! Trusted adapters for local sample-addressable real-model fixtures.

Weights use the little-endian length-prefixed tensor stream. Inputs are supplied per process as
one length-prefixed tensor containing only selected fixed-width sample records. The dataset file
is staged by the host, so this process never reads the full sample corpus or labels.
-/

namespace RealSuite

open ExactRuntime ExactSuite

def readTensors (bytes : ByteArray) : Except String (Array (Array Word)) := do
  let mut tensors := #[]
  let mut offset := 0
  while offset < bytes.size do
    if offset + 4 > bytes.size then throw "truncated tensor length"
    let size := bytes[offset]!.toNat ||| (bytes[offset + 1]!.toNat <<< 8) |||
      (bytes[offset + 2]!.toNat <<< 16) ||| (bytes[offset + 3]!.toNat <<< 24)
    offset := offset + 4
    if size > (bytes.size - offset) / 4 then throw "truncated tensor data"
    let tensor := Array.ofFn (n := size) fun i =>
      bytes[offset + i.val * 4]!.toUInt32 |||
      (bytes[offset + i.val * 4 + 1]!.toUInt32 <<< 8) |||
      (bytes[offset + i.val * 4 + 2]!.toUInt32 <<< 16) |||
      (bytes[offset + i.val * 4 + 3]!.toUInt32 <<< 24)
    tensors := tensors.push tensor
    offset := offset + size * 4
  if tensors.isEmpty then throw "fixture needs at least one tensor"
  return tensors

def eps : Word := 0x3727C5AC

def imageLayers (family : String) (side : Nat) (t : Array (Array Word)) : Except String (List Layer) := do
  let get (i : Nat) := t[i]!
  if family == "mlp" then
    return [.linear 1 (side * side) 32 true (get 0) (get 1),
      .linear 1 32 16 true (get 2) (get 3), .linear 1 16 10 false (get 4) (get 5)]
  if family == "normalized_mlp" then
    return [.linear 1 (side * side) 32 true (get 0) (get 1),
      .rmsnorm 1 32 eps (get 2), .linear 1 32 16 true (get 3) (get 4),
      .layernorm 1 16 eps (get 5) (get 6), .linear 1 16 10 false (get 7) (get 8)]
  if family == "resnet" then
    let pooled := side / 2
    return [.conv (.simple 1 side side 8 3 3 1 1) false (get 0) (Array.replicate 8 0),
      .bn true 8 (side * side) (get 1) (get 2), .save,
      .conv (.simple 8 side side 8 3 3 1 1) false (get 3) (Array.replicate 8 0),
      .bn true 8 (side * side) (get 4) (get 5),
      .conv (.simple 8 side side 8 3 3 1 1) false (get 6) (Array.replicate 8 0),
      .bn false 8 (side * side) (get 7) (get 8), .addSaved true,
      .maxpool (.square 8 side side 2),
      .conv (.simple 8 pooled pooled 16 3 3 1 1) false (get 9) (Array.replicate 16 0),
      .bn true 16 (pooled * pooled) (get 10) (get 11),
      .avgpool (.square 16 pooled pooled pooled),
      .linear 1 16 10 false (get 12) (get 13)]
  if family == "mobilenet" then
    let pooled := side / 2
    return [.conv (.simple 1 side side 8 3 3 1 1) false (get 0) (Array.replicate 8 0),
      .bn true 8 (side * side) (get 1) (get 2),
      .depthwise (.simple 8 side side 8 3 3 1 1) false (get 3) (Array.replicate 8 0),
      .bn true 8 (side * side) (get 4) (get 5),
      .conv (.simple 8 side side 16 1 1 1 0) false (get 6) (Array.replicate 16 0),
      .bn true 16 (side * side) (get 7) (get 8),
      .depthwise (.simple 16 side side 16 3 3 1 1) false (get 9) (Array.replicate 16 0),
      .bn true 16 (side * side) (get 10) (get 11),
      .conv (.simple 16 side side 16 1 1 1 0) false (get 12) (Array.replicate 16 0),
      .bn true 16 (side * side) (get 13) (get 14),
      .maxpool (.square 16 side side 2),
      .avgpool (.square 16 pooled pooled pooled), .linear 1 16 10 false (get 15) (get 16)]
  if family == "mixer" then
    let channels := (side / 4) * (side / 4)
    return [.save, .layernorm 16 channels eps (get 1) (get 2),
      .gemm 16 16 channels (get 0), .addSaved false,
      .save, .layernorm 16 channels eps (get 3) (get 4),
      .linear 16 channels (2 * channels) true (get 5) (get 6),
      .linear 16 (2 * channels) channels false (get 7) (get 8), .addSaved false,
      .linear 1 (16 * channels) 10 false (get 9) (get 10)]
  throw s!"unsupported real-model adapter {family}"

def sensorLayers (family : String) (size : Nat) (t : Array (Array Word)) : Except String (List Layer) := do
  if size % 128 != 0 then throw "UCI-HAR input must contain 128 samples per channel"
  let channels := size / 128
  if family == "mlp" then
    return [.linear 1 size 128 true t[0]! t[1]!, .linear 1 128 64 true t[2]! t[3]!,
      .linear 1 64 6 false t[4]! t[5]!]
  if family == "normalized_mlp" then
    return [.linear 1 size 64 true t[0]! t[1]!, .rmsnorm 1 64 eps t[2]!,
      .linear 1 64 32 true t[3]! t[4]!, .layernorm 1 32 eps t[5]! t[6]!,
      .linear 1 32 6 false t[7]! t[8]!]
  if family == "resnet" then
    return [.conv (.simple channels 128 1 8 3 1 1 0) false t[0]! (Array.replicate 8 0),
      .bn true 8 126 t[1]! t[2]!, .save,
      .conv (.simple 8 126 1 8 1 1 1 0) false t[3]! (Array.replicate 8 0),
      .bn true 8 126 t[4]! t[5]!,
      .conv (.simple 8 126 1 8 1 1 1 0) false t[6]! (Array.replicate 8 0),
      .bn false 8 126 t[7]! t[8]!, .addSaved true,
      .linear 1 1008 6 false t[9]! t[10]!]
  if family == "mobilenet" then
    return [.conv (.simple channels 128 1 8 3 1 1 0) false t[0]! (Array.replicate 8 0),
      .bn true 8 126 t[1]! t[2]!,
      .depthwise (.simple 8 126 1 8 3 1 1 0) false t[3]! (Array.replicate 8 0),
      .bn true 8 124 t[4]! t[5]!,
      .conv (.simple 8 124 1 16 1 1 1 0) false t[6]! (Array.replicate 16 0),
      .bn true 16 124 t[7]! t[8]!,
      .depthwise (.simple 16 124 1 16 3 1 1 0) false t[9]! (Array.replicate 16 0),
      .bn true 16 122 t[10]! t[11]!,
      .conv (.simple 16 122 1 16 1 1 1 0) false t[12]! (Array.replicate 16 0),
      .bn true 16 122 t[13]! t[14]!, .linear 1 1952 6 false t[15]! t[16]!]
  if family == "mixer" then
    if size % 16 != 0 then throw "UCI-HAR Mixer input must divide into 16 tokens"
    let width := size / 16
    return [.save, .layernorm 16 width eps t[1]! t[2]!, .gemm 16 16 width t[0]!,
      .addSaved false, .save, .layernorm 16 width eps t[3]! t[4]!,
      .linear 16 width (2 * width) true t[5]! t[6]!,
      .linear 16 (2 * width) width false t[7]! t[8]!, .addSaved false,
      .linear 1 size 6 false t[9]! t[10]!]
  throw s!"unsupported UCI-HAR model family {family}"

def applianceMixerLayers (t : Array (Array Word)) : Except String (List Layer) := do
  if t.size < 20 then throw "Appliance Mixer fixture has too few weight tensors"
  let block (offset : Nat) : List Layer :=
    [.save, .layernorm 24 8 eps t[offset + 1]! t[offset + 2]!,
      .gemm 24 24 8 t[offset]!, .addSaved false,
      .save, .layernorm 24 8 eps t[offset + 3]! t[offset + 4]!,
      .linear 24 8 16 true t[offset + 5]! t[offset + 6]!,
      .linear 24 16 8 false t[offset + 7]! t[offset + 8]!, .addSaved false]
  return block 0 ++ block 9 ++ [.linear 1 192 6 false t[18]! t[19]!]

def prepare (adapter family : String) (size : Nat) (tensors : Array (Array Word)) : Except String (List Layer) := do
  if tensors.isEmpty || size == 0 then throw "fixture requires weights and a positive input size"
  let layers ← if adapter == "image-zoo-v1" then
      let side := Nat.sqrt size
      if side * side != size then throw "image adapter requires square flattened inputs"
      imageLayers family side tensors
    else if adapter == "uci-har-v1" then sensorLayers family size tensors
    else if adapter == "appliance-mixer-v1" && family == "mixer" then
      applianceMixerLayers tensors
    else throw s!"unsupported adapter/family pair {adapter}/{family}"
  return layers

end RealSuite
