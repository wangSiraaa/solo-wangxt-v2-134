"""Imaging primitives: multi-channel arrays, pyramid geometry, tile PNG I/O."""

from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

TILE = 512  # default, overridden via settings.tile_size where plumbed


def save_npy(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    return buf.getvalue()


def load_npy(data: bytes) -> np.ndarray:
    return np.load(io.BytesIO(data), allow_pickle=False)


def save_npz(**arrays: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def load_npz(data: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def num_levels(h: int, w: int, tile: int = TILE) -> int:
    """Deepest pyramid level whose dimensions still exceed one tile."""
    return max(1, math.ceil(math.log2(max(h, w) / tile)) + 1)


def level_dimensions(base_h: int, base_w: int, level: int) -> tuple[int, int]:
    """level 0 = full resolution; each deeper level halves."""
    scale = 2**level
    return max(1, math.ceil(base_h / scale)), max(1, math.ceil(base_w / scale))


def grid_for_level(h: int, w: int, tile: int = TILE) -> tuple[int, int]:
    return math.ceil(w / tile), math.ceil(h / tile)


def downsample2(arr: np.ndarray) -> np.ndarray:
    """Mean-pool 2x with edge handling, keeping the source dtype range."""
    h, w = arr.shape[:2]
    h2, w2 = math.ceil(h / 2), math.ceil(w / 2)
    acc = np.zeros((h2, w2) + arr.shape[2:], dtype=np.float64)
    cnt = np.zeros((h2, w2) + (1,) * (arr.ndim - 2), dtype=np.float64)
    for ih in range(2):
        for iw in range(2):
            block = arr[ih::2, iw::2]
            acc[: block.shape[0], : block.shape[1]] += block.astype(np.float64)
            cnt[: block.shape[0], : block.shape[1]] += 1
    mean = acc / np.maximum(cnt, 1)
    if np.issubdtype(arr.dtype, np.integer):
        mean = np.clip(np.round(mean), 0, np.iinfo(arr.dtype).max)
    return mean.astype(arr.dtype)


def crop_tile(arr: np.ndarray, level_arr: np.ndarray, tx: int, ty: int, tile: int) -> np.ndarray:
    h, w = level_arr.shape[:2]
    y0, x0 = ty * tile, tx * tile
    y1, x1 = min(h, y0 + tile), min(w, x0 + tile)
    return np.ascontiguousarray(level_arr[y0:y1, x0:x1])


def encode_gray_png(tile: np.ndarray) -> bytes:
    """Encode a 2-D uint16/uint8 tile as grayscale PNG; 16-bit preserved."""
    if tile.ndim != 2:
        raise ValueError("gray tile must be 2-D")
    mode = "I;16" if tile.dtype == np.uint16 else "L"
    img = Image.fromarray(tile, mode=mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def encode_rgb_png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def normalize_for_view(tile: np.ndarray, scale: float) -> np.ndarray:
    return np.clip(tile.astype(np.float32) * scale, 0, 255).astype(np.uint8)


def make_synthetic_image(
    h: int = 4096,
    w: int = 4096,
    channels: int = 4,
    components: int = 3,
    seed: int = 7,
    dtype_max: int = 65535,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Build a known-composition test image.

    Returns (observed[H,W,C uint16], truth_abundance[H,W,K float64],
             matrix[C,K float64], component_masks[K,H,W bool]).
    The observation is A @ M.T plus mild Poisson-like noise, then clipped
    (clipping creates genuine saturation cases at component hotspots).
    """
    rng = np.random.default_rng(seed)
    # well-conditioned mixing matrix: dominant diagonal + bleed
    m = np.eye(channels, components, dtype=np.float64)
    if channels >= components:
        bleed = rng.uniform(0.05, 0.2, size=(channels, components))
        m = m * rng.uniform(0.7, 1.0, size=(1, components)) + bleed
        m = m / np.linalg.norm(m, axis=0, keepdims=True)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    truth = np.zeros((h, w, components), dtype=np.float64)
    masks = np.zeros((components, h, w), dtype=bool)
    centers = [(0.32, 0.30), (0.68, 0.35), (0.5, 0.72)][:components]
    radii = [0.16, 0.14, 0.18][:components]
    for k, (cy, cx) in enumerate(centers):
        r2 = ((yy / h - cy) ** 2 + (xx / w - cx) ** 2) / (radii[k] ** 2)
        blob = np.clip(1.0 - r2, 0, 1) ** 1.7
        # deliberately hot core (1.04x detector ceiling) to produce genuine
        # camera clipping / saturation at the component hotspot
        truth[..., k] = blob * (dtype_max * 1.04)
        masks[k] = blob > 0.05

    observed = truth @ m.T
    noise = rng.normal(0.0, dtype_max * 0.0008, size=observed.shape)
    observed = np.clip(observed + noise, 0, dtype_max).round().astype(np.uint16)
    return observed, truth, m, masks
