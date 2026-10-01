from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image
from scipy import linalg, optimize

from .storage import canonical_json, sha256_bytes


class UnmixValidationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def as_matrix(coefficients: list[list[float]], image_channels: list[str], matrix_channels: list[str]):
    a = np.asarray(coefficients, dtype=np.float64)
    missing = sorted(set(image_channels) - set(matrix_channels))
    if missing:
        raise UnmixValidationError("missing_channel", f"matrix lacks channels: {', '.join(missing)}")
    order = [matrix_channels.index(c) for c in image_channels]
    a = a[order, :]
    if a.ndim != 2 or a.size == 0:
        raise UnmixValidationError("invalid_matrix", "coefficient matrix must be 2-D and non-empty")
    if np.any(~np.isfinite(a)) or np.any(a < 0):
        raise UnmixValidationError("invalid_matrix", "coefficients must be finite and non-negative")
    if np.all(a == 0, axis=0).any() or np.all(a == 0, axis=1).any():
        raise UnmixValidationError("invalid_matrix", "matrix contains an all-zero row or column")
    return a


def matrix_condition(a: np.ndarray) -> float:
    rank = int(np.linalg.matrix_rank(a, tol=1e-10))
    if rank < min(a.shape):
        return float("inf")
    return float(np.linalg.cond(a))


def validate_for_image(matrix: dict[str, Any], image: dict[str, Any], condition_limit: float = 1e8):
    a = as_matrix(matrix["coefficients"], image["channel_names"], matrix["channel_names"])
    condition = matrix_condition(a)
    if not math.isfinite(condition) or condition > condition_limit:
        raise UnmixValidationError(
            "ill_conditioned_matrix",
            f"matrix condition number {condition:.3g} exceeds limit {condition_limit:.3g}",
        )
    return a, condition


def algorithm_digest(algorithm: str, params: dict[str, Any], frozen_digest: str | None) -> str:
    return sha256_bytes(canonical_json({
        "algorithm": algorithm,
        "params": params,
        "frozen": frozen_digest,
    }))


def tile_cache_key(
    *,
    image_digest: str,
    matrix_digest: str,
    algorithm: str,
    algorithm_params: dict[str, Any],
    frozen_digest: str,
    level: int,
    x: int,
    y: int,
    kind: str,
) -> str:
    material = {
        "v": 1,
        "image": image_digest,
        "matrix": matrix_digest,
        "algorithm": algorithm,
        "algorithm_params": algorithm_params,
        "frozen": frozen_digest,
        "level": level,
        "x": x,
        "y": y,
        "kind": kind,
    }
    digest = sha256_bytes(canonical_json(material))
    # Human-readable prefix is useful when debugging cache poisoning. The hash
    # is the actual authority and changes whenever any coefficient changes.
    return (
        f"img={image_digest[:12]}/mat={matrix_digest[:12]}/"
        f"alg={algorithm}/lvl={level}/x={x}/y={y}/{kind}/{digest}"
    )


def freeze_global_statistics(
    tiles: list[np.ndarray],
    *,
    saturation_quantile: float = 0.999,
    saturation_fraction: float = 0.001,
) -> dict[str, Any]:
    """Compute parameters from the complete level-zero image exactly once.

    Workers receive these values in their frozen payload. Tile-local medians
    are intentionally not used as global parameters; that would make seams
    depend on scheduling and on which neighbours completed first.
    """
    if not tiles:
        raise UnmixValidationError("empty_image", "cannot freeze statistics without source tiles")
    channels = tiles[0].shape[0]
    if any(t.shape[0] != channels for t in tiles):
        raise UnmixValidationError("missing_channel", "source tiles have inconsistent channel counts")

    # A robust low percentile approximates detector dark level. Values from
    # all level-zero tiles are concatenated before the estimate is made.
    samples = [t.reshape(channels, -1) for t in tiles]
    flat = np.concatenate(samples, axis=1)
    offsets = np.nanquantile(flat, 0.01, axis=1)
    # Scales are recorded for visualization/QC, but numerical unmixing uses
    # physical units. Dividing each observed channel by its own maximum would
    # silently rescale the calibrated matrix and prevent recovery of known
    # components.
    observed_scales = np.maximum(np.nanmax(flat, axis=1) - offsets, 1e-12)
    return {
        "channel_offsets": offsets.astype(float).tolist(),
        "channel_scales": np.ones(channels, dtype=float).tolist(),
        "observed_channel_scales": observed_scales.astype(float).tolist(),
        "saturation_quantile": saturation_quantile,
        "saturation_fraction": saturation_fraction,
        "source_tile_count": len(tiles),
        "pixel_count": int(flat.shape[1]),
    }


@dataclass
class UnmixResult:
    components: np.ndarray
    reconstruction: np.ndarray
    residual: np.ndarray
    stats: dict[str, Any]
    quality_flags: dict[str, Any]


def _nnls_column(a: np.ndarray, values: np.ndarray) -> np.ndarray:
    x, _ = optimize.nnls(a, values)
    return x


def unmix_tile(
    tile: np.ndarray,
    a: np.ndarray,
    frozen_params: dict[str, Any],
    *,
    algorithm: str = "nnls",
    ridge: float = 0.0,
) -> UnmixResult:
    if tile.ndim != 3:
        raise UnmixValidationError("corrupt_tile", "source tile must have shape C,H,W")
    c, h, w = tile.shape
    if a.shape[0] != c:
        raise UnmixValidationError(
            "missing_channel", f"tile has {c} channels but matrix has {a.shape[0]} rows"
        )
    if not np.all(np.isfinite(tile)):
        raise UnmixValidationError("corrupt_tile", "source tile contains NaN or infinite samples")

    offsets = np.asarray(frozen_params["channel_offsets"], dtype=np.float64).reshape(c, 1, 1)
    scales = np.asarray(frozen_params["channel_scales"], dtype=np.float64).reshape(c, 1, 1)
    normalized = np.clip((tile - offsets) / scales, 0.0, 1.0)
    k = a.shape[1]

    if algorithm not in {"nnls", "fista_nnls"}:
        raise UnmixValidationError("unknown_algorithm", f"unsupported algorithm {algorithm}")

    flat = normalized.reshape(c, h * w)
    x = np.zeros((k, h * w), dtype=np.float64)

    if algorithm == "nnls":
        # Correct reference path. Large production tiles can switch to the
        # vectorized fista_nnls algorithm; both are recorded in the cache key.
        for i in range(flat.shape[1]):
            x[:, i] = _nnls_column(a, flat[:, i])
    else:
        # Accelerated projected gradient NNLS. Deterministic and NumPy-only.
        lip = float(linalg.eigvalsh(a.T @ a, subset_by_index=[k - 1, k - 1])[0])
        step = 1.0 / max(lip + ridge, 1e-12)
        y = x.copy()
        t = 1.0
        for _ in range(80):
            grad = a.T @ (a @ y - flat) + ridge * y
            nxt = np.maximum(y - step * grad, 0.0)
            t_next = (1.0 + math.sqrt(1.0 + 4.0 * t * t)) / 2.0
            y = nxt + ((t - 1.0) / t_next) * (nxt - x)
            x, t = nxt, t_next

    components = x.reshape(k, h, w)
    reconstruction_norm = np.tensordot(a, components, axes=([1], [0]))
    reconstruction = reconstruction_norm * scales + offsets
    residual = tile - reconstruction
    diff = normalized.reshape(c, -1) - reconstruction_norm.reshape(c, -1)
    rmse = float(np.sqrt(np.mean(diff * diff)))
    max_abs = float(np.max(np.abs(diff)))

    threshold = float(frozen_params.get("saturation_quantile", 0.999))
    saturated_mask = normalized >= threshold
    saturated_fraction = float(np.mean(saturated_mask))
    saturation_warn = saturated_fraction >= float(
        frozen_params.get("saturation_fraction", 0.001)
    )
    quality = {
        "saturated": saturation_warn,
        "saturated_fraction": saturated_fraction,
        "saturated_channels": np.where(np.any(saturated_mask, axis=(1, 2)))[0].astype(int).tolist(),
    }
    stats = {
        "rmse": rmse,
        "max_abs_residual": max_abs,
        "component_mass": np.sum(components, axis=(1, 2)).astype(float).tolist(),
    }
    return UnmixResult(components, reconstruction, residual, stats, quality)


def encode_result_tile(result: UnmixResult) -> bytes:
    return _npz_bytes(
        components=result.components.astype(np.float32),
        reconstruction=result.reconstruction.astype(np.float32),
        residual=result.residual.astype(np.float32),
        stats=canonical_json(result.stats),
        quality_flags=canonical_json(result.quality_flags),
    )


def _npz_bytes(**arrays: Any) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def decode_result_tile(data: bytes) -> dict[str, Any]:
    loaded = np.load(io.BytesIO(data), allow_pickle=False)
    import json

    return {
        "components": loaded["components"],
        "reconstruction": loaded["reconstruction"],
        "residual": loaded["residual"],
        "stats": json.loads(str(loaded["stats"])),
        "quality_flags": json.loads(str(loaded["quality_flags"])),
    }


def encode_source_tile(tile: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, tile=tile.astype(np.float32))
    return buf.getvalue()


def decode_source_tile(data: bytes) -> np.ndarray:
    loaded = np.load(io.BytesIO(data), allow_pickle=False)
    tile = np.asarray(loaded["tile"], dtype=np.float64)
    if tile.ndim != 3 or not np.all(np.isfinite(tile)):
        raise UnmixValidationError("corrupt_tile", "source tile failed shape or finite-value checks")
    return tile


def normalize_channel(values: np.ndarray, *, vmin: float | None = None, vmax: float | None = None) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros(values.shape + (3,), dtype=np.uint8)
    if vmin is None:
        vmin = float(np.quantile(finite, 0.01))
    if vmax is None:
        vmax = float(np.quantile(finite, 0.99))
    if vmax <= vmin:
        vmax = vmin + 1.0
    scaled = np.clip((values - vmin) / (vmax - vmin), 0, 1)
    out = (scaled * 255).astype(np.uint8)
    return np.stack([out, out, out], axis=-1)


def encode_png_channel(values: np.ndarray, color: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    rgb = normalize_channel(values)
    tint = (rgb.astype(np.float32) / 255.0) * np.asarray(color, dtype=np.float32)
    image = Image.fromarray(np.clip(tint, 0, 255).astype(np.uint8), mode="RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def build_levels(width: int, height: int, tile_size: int) -> list[dict[str, int]]:
    levels = []
    w, h, level = width, height, 0
    while True:
        levels.append({
            "level": level,
            "width": w,
            "height": h,
            "x_tiles": int(math.ceil(w / tile_size)),
            "y_tiles": int(math.ceil(h / tile_size)),
        })
        if w <= tile_size and h <= tile_size:
            break
        w, h, level = max(1, (w + 1) // 2), max(1, (h + 1) // 2), level + 1
    return levels


def osd_to_storage_level(osd_level: int, max_level: int) -> int:
    """Map Deep Zoom level zero (smallest) to our level zero (full res)."""
    if osd_level < 0 or osd_level > max_level:
        raise UnmixValidationError("invalid_level", f"level {osd_level} is outside pyramid")
    return max_level - osd_level


def make_synthetic_image(
    components: np.ndarray,
    a: np.ndarray,
    *,
    noise_sigma: float = 0.002,
    offset: float = 0.02,
    seed: int = 7,
) -> tuple[np.ndarray, np.ndarray]:
    """Return mixed source and exact components for acceptance checks."""
    rng = np.random.default_rng(seed)
    source = np.tensordot(a, components, axes=([1], [0]))
    source += offset + rng.normal(0, noise_sigma, source.shape)
    return np.clip(source, 0, 1), components.astype(np.float64)


def recovery_metrics(estimated: np.ndarray, truth: np.ndarray) -> dict[str, float]:
    estimated = np.asarray(estimated, dtype=np.float64)
    truth = np.asarray(truth, dtype=np.float64)
    denom = max(float(np.linalg.norm(truth)), 1e-12)
    return {
        "component_rmse": float(np.sqrt(np.mean((estimated - truth) ** 2))),
        "relative_component_error": float(np.linalg.norm(estimated - truth) / denom),
    }
