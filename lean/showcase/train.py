"""Trains the showcase models on scikit-learn's 8×8 digits and writes `digits.bin`.

Not part of the gate. Needs numpy and scikit-learn (`uv pip install numpy scikit-learn`):

    python lean/showcase/train.py

`digits.bin` is a list of float32 tensors, each a little-endian u32 count followed by that many
float32 words, then the test labels as a u32 count and u32 words. Tensor order and layouts are
those of `ExactRuntime.Layer` (linear weights `out × in`, conv weights `out × in × 3 × 3`):

    mlp:  w1 (64 → 64), b1, w2 (64 → 32), b2, w3 (32 → 10), b3
    cnn:  conv1 (1 → 8), b, conv2 (8 → 16), b, fc (16 → 10), b
    test: images (360 × 64, values in [0, 1])
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sklearn.datasets import load_digits

OUT = Path(__file__).resolve().parent / "digits.bin"
TEST = 360
rng = np.random.default_rng(0)


def data():
    # The first 1437 images train, the last 360 test; pixels scaled from 0..16 to 0..1.
    d = load_digits()
    x = (d.data / 16.0).astype(np.float32)
    return x[:-TEST], d.target[:-TEST], x[-TEST:], d.target[-TEST:]


def init(*shape):
    fan_in = int(np.prod(shape[1:]))
    return (rng.standard_normal(shape) * np.sqrt(2.0 / fan_in)).astype(np.float32)


def softmax_grad(logits, y):
    # d(cross-entropy)/d(logits), averaged over the batch.
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    p[np.arange(len(y)), y] -= 1
    return p / len(y)


# MLP


def mlp_train(x, y, epochs=60, lr=0.1, batch=32):
    ws = [init(64, 64), init(32, 64), init(10, 32)]
    bs = [np.zeros(n, np.float32) for n in (64, 32, 10)]
    for _ in range(epochs):
        order = rng.permutation(len(x))
        for i in range(0, len(x), batch):
            xb, yb = x[order[i : i + batch]], y[order[i : i + batch]]
            acts = [xb]
            for k, (w, b) in enumerate(zip(ws, bs)):
                z = acts[-1] @ w.T + b
                acts.append(np.maximum(z, 0) if k < 2 else z)
            g = softmax_grad(acts[-1], yb)
            for k in (2, 1, 0):
                gw, gb = g.T @ acts[k], g.sum(axis=0)
                g = (g @ ws[k]) * (acts[k] > 0)
                ws[k] -= lr * gw
                bs[k] -= lr * gb
    return ws, bs


def mlp_predict(ws, bs, x):
    for k, (w, b) in enumerate(zip(ws, bs)):
        x = x @ w.T + b
        x = np.maximum(x, 0) if k < 2 else x
    return x.argmax(axis=1)


# CNN: conv 3×3 1 → 8, ReLU, max pool 2, conv 3×3 8 → 16, ReLU, global average, linear 16 → 10


def im2col(x):
    # (n, c, h, w) → (n, h, w, c·9), windows in (c, dy, dx) order with zero padding 1.
    n, c, h, w = x.shape
    p = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    cols = [p[:, :, dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3)]
    return np.stack(cols, axis=2).reshape(n, c * 9, h, w).transpose(0, 2, 3, 1)


def col2im(g, c):
    # The adjoint of im2col: (n, h, w, c·9) → (n, c, h, w).
    n, h, w, _ = g.shape
    g = g.transpose(0, 3, 1, 2).reshape(n, c, 9, h, w)
    out = np.zeros((n, c, h + 2, w + 2), np.float32)
    for t in range(9):
        dy, dx = divmod(t, 3)
        out[:, :, dy : dy + h, dx : dx + w] += g[:, :, t]
    return out[:, :, 1:-1, 1:-1]


def cnn_forward(p, x):
    c1w, c1b, c2w, c2b, fw, fb = p
    x = x.reshape(-1, 1, 8, 8)
    col1 = im2col(x)
    z1 = (col1 @ c1w.reshape(8, -1).T + c1b).transpose(0, 3, 1, 2)
    a1 = np.maximum(z1, 0)
    win = a1.reshape(-1, 8, 4, 2, 4, 2)
    pool = win.max(axis=(3, 5))
    col2 = im2col(pool)
    z2 = (col2 @ c2w.reshape(16, -1).T + c2b).transpose(0, 3, 1, 2)
    a2 = np.maximum(z2, 0)
    feat = a2.mean(axis=(2, 3))
    return feat @ fw.T + fb, (col1, z1, a1, win, pool, col2, z2, feat)


def cnn_train(x, y, epochs=60, lr=0.1, batch=32):
    p = [init(8, 1, 3, 3), np.zeros(8, np.float32), init(16, 8, 3, 3), np.zeros(16, np.float32)]
    p += [init(10, 16), np.zeros(10, np.float32)]
    for _ in range(epochs):
        order = rng.permutation(len(x))
        for i in range(0, len(x), batch):
            xb, yb = x[order[i : i + batch]], y[order[i : i + batch]]
            logits, (col1, z1, a1, win, pool, col2, z2, feat) = cnn_forward(p, xb)
            g = softmax_grad(logits, yb)
            g_fw, g_fb = g.T @ feat, g.sum(axis=0)
            g = (g @ p[4])[:, :, None, None] / 16 * np.ones_like(z2) * (z2 > 0)
            gz2 = g.transpose(0, 2, 3, 1)
            g_c2w = (gz2.reshape(-1, 16).T @ col2.reshape(-1, 72)).reshape(16, 8, 3, 3)
            g_c2b = gz2.sum(axis=(0, 1, 2))
            g_pool = col2im(gz2 @ p[2].reshape(16, -1), 8)
            mask = win == pool[:, :, :, None, :, None]
            g_a1 = (mask * g_pool[:, :, :, None, :, None]).reshape(a1.shape)
            gz1 = (g_a1 * (z1 > 0)).transpose(0, 2, 3, 1)
            g_c1w = (gz1.reshape(-1, 8).T @ col1.reshape(-1, 9)).reshape(8, 1, 3, 3)
            g_c1b = gz1.sum(axis=(0, 1, 2))
            for k, gk in enumerate((g_c1w, g_c1b, g_c2w, g_c2b, g_fw, g_fb)):
                p[k] -= lr * gk
    return p


def main():
    xtr, ytr, xte, yte = data()
    ws, bs = mlp_train(xtr, ytr)
    print(f"mlp test accuracy {(mlp_predict(ws, bs, xte) == yte).mean():.3f}")
    cnn = cnn_train(xtr, ytr)
    print(f"cnn test accuracy {(cnn_forward(cnn, xte)[0].argmax(axis=1) == yte).mean():.3f}")
    tensors = [ws[0], bs[0], ws[1], bs[1], ws[2], bs[2], *cnn, xte]
    with OUT.open("wb") as f:
        for t in tensors:
            t = np.ascontiguousarray(t, dtype="<f4").ravel()
            f.write(np.uint32(t.size).astype("<u4").tobytes() + t.tobytes())
        f.write(np.uint32(TEST).astype("<u4").tobytes() + yte.astype("<u4").tobytes())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
