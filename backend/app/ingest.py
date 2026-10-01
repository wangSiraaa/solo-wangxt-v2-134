from __future__ import annotations

from typing import Any

import numpy as np

from . import imaging
from .storage import ObjectStore


def _downsample_half(tile: np.ndarray) -> np.ndarray:
    c, h, w = tile.shape
    h2, w2 = h // 2, w // 2
    cropped = tile[:, : h2 * 2, : w2 * 2]
    return cropped.reshape(c, h2, 2, w2, 2).mean(axis=(2, 4))


def _slice_level(image: np.ndarray, level: int) -> np.ndarray:
    out = image
    for _ in range(level):
        out = _downsample_half(out)
    return out


def create_pyramid_manifest(
    store: ObjectStore,
    image: np.ndarray,
    *,
    image_id: str,
    tile_size: int,
) -> dict[str, Any]:
    if image.ndim != 3:
        raise imaging.UnmixValidationError("invalid_image", "image must have shape C,H,W")
    channels, height, width = image.shape
    levels = imaging.build_levels(width, height, tile_size)
    tiles = []
    source_digest_inputs = []
    for info in levels:
        level = info["level"]
        pyramid = _slice_level(image, level)
        for yi in range(info["y_tiles"]):
            for xi in range(info["x_tiles"]):
                y0, x0 = yi * tile_size, xi * tile_size
                piece = pyramid[:, y0 : min(y0 + tile_size, info["height"]), x0 : min(x0 + tile_size, info["width"])]
                data = imaging.encode_source_tile(piece)
                key, digest = store.put_content_addressed(
                    f"sources/{image_id}/level/{level}", data, ".npz"
                )
                source_digest_inputs.append(digest)
                tiles.append({
                    "level": level,
                    "x": xi,
                    "y": yi,
                    "width": piece.shape[2],
                    "height": piece.shape[1],
                    "object_key": key,
                    "digest": digest,
                })
    manifest = {
        "image_id": image_id,
        "width": width,
        "height": height,
        "channel_count": channels,
        "tile_size": tile_size,
        "levels": levels,
        "tiles": tiles,
    }
    # Digest every tile, not just the submitted archive. A missing or altered
    # pyramid slice therefore cannot share an image digest with the original.
    manifest["source_digest"] = imaging.sha256_bytes(
        imaging.canonical_json([t["digest"] for t in tiles])
    )
    return manifest


def synthetic_demo_image(width: int = 2048, height: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    channels = ["DAPI", "FITC", "TRITC", "Cy5"]
    components_names = ["nuclei", "membrane", "beads"]
    a = np.asarray([
        [0.95, 0.08, 0.02],
        [0.08, 0.88, 0.05],
        [0.03, 0.10, 0.90],
        [0.02, 0.04, 0.18],
    ], dtype=np.float64)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    rng = np.random.default_rng(42)

    def gaussian_blob(cx: float, cy: float, sx: float, sy: float, peak: float) -> np.ndarray:
        return peak * np.exp(-0.5 * (((xx - cx) / sx) ** 2 + ((yy - cy) / sy) ** 2))

    nuclei = np.zeros((height, width), dtype=np.float64)
    membrane = np.zeros((height, width), dtype=np.float64)
    spacing_x = max(120.0, width / 8.0)
    spacing_y = max(120.0, height / 8.0)
    i = 0
    for cy in np.arange(spacing_y * 0.55, height, spacing_y):
        for cx in np.arange(spacing_x * 0.55, width, spacing_x):
            jitter_x = rng.normal(0, spacing_x * 0.12)
            jitter_y = rng.normal(0, spacing_y * 0.12)
            sx = rng.uniform(spacing_x * 0.10, spacing_x * 0.14)
            sy = rng.uniform(spacing_y * 0.10, spacing_y * 0.14)
            nuclei += gaussian_blob(cx + jitter_x, cy + jitter_y, sx, sy, rng.uniform(0.34, 0.48))
            if i % 3 == 0:
                membrane += gaussian_blob(
                    cx + spacing_x * 0.30 + rng.normal(0, 12),
                    cy + spacing_y * 0.25 + rng.normal(0, 12),
                    sx * 1.45,
                    sy * 1.45,
                    rng.uniform(0.20, 0.30),
                )
            i += 1

    beads = np.zeros((height, width), dtype=np.float64)
    bead_count = max(24, (width * height) // 5_500)
    bx = rng.integers(8, max(9, width - 8), bead_count)
    by = rng.integers(8, max(9, height - 8), bead_count)
    for cx, cy in zip(bx, by):
        beads += gaussian_blob(float(cx), float(cy), 1.4, 1.4, rng.uniform(0.35, 0.52))

    components = np.stack([nuclei, membrane, beads], axis=0)
    # Components are bounded before mixing so the image has a small but
    # meaningful saturation population without destroying recoverability.
    components = np.clip(components, 0.0, 0.72)
    source, _ = imaging.make_synthetic_image(components, a, noise_sigma=0.003, offset=0.015)
    return source.astype(np.float64), components.astype(np.float64)
