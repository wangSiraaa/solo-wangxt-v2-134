"""
Non-negative unmixing.

The hard consistency rule implemented here:

* Parameters that describe the WHOLE image (background, noise scale, display
  normalization, matrix conditioning) are estimated ONCE from a coarse
  pyramid level and frozen into a content-hashed blob before any tile is
  dispatched.
* Every tile receives that frozen blob, verifies the hash, and only does the
  per-pixel solve. Tiles cannot estimate their own background/scale and pass
  it off as a single global model (which would create seams and a Frankenstein
  coefficient field).
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from scipy.optimize import nnls as scipy_nnls

from .config import get_settings

SOLVER_VERSION = "fista-nnls-1"


class UnmixingError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Matrix validation
# ---------------------------------------------------------------------------


def validate_matrix(M: np.ndarray, n_channels: int) -> list[dict]:
    """Return a list of issues (empty == healthy)."""
    issues: list[dict] = []
    s = get_settings()
    if M.ndim != 2:
        raise UnmixingError("matrix_bad_shape", f"matrix must be 2-D, got {M.shape}")
    if M.shape[0] != n_channels:
        raise UnmixingError(
            "matrix_channel_mismatch",
            f"image has {n_channels} channels but matrix maps {M.shape[0]}",
        )
    if M.shape[1] < 1:
        raise UnmixingError("matrix_no_components", "matrix has zero components")
    if not np.isfinite(M).all():
        raise UnmixingError("matrix_nonfinite", "matrix contains NaN/Inf")
    if (M < 0).any():
        raise UnmixingError("matrix_negative", "bleed matrix must be non-negative")

    # "Missing channel": a detection channel that records essentially nothing.
    row_norm = np.linalg.norm(M, axis=1)
    for c, rn in enumerate(row_norm):
        if rn < 1e-9:
            issues.append(
                {"code": "missing_channel", "severity": "error", "channel": int(c),
                 "message": f"channel {c} has zero response for every component"}
            )
    col_norm = np.linalg.norm(M, axis=0)
    for k, cn in enumerate(col_norm):
        if cn < 1e-9:
            issues.append(
                {"code": "dead_component", "severity": "error", "component": int(k),
                 "message": f"component {k} has zero spectrum"}
            )

    rank = int(np.linalg.matrix_rank(M, tol=1e-8))
    if rank < min(M.shape):
        issues.append(
            {"code": "matrix_rank_deficient", "severity": "error", "rank": rank,
             "message": f"matrix rank {rank} < min(M.shape)={min(M.shape)}"}
        )
    try:
        cond = float(np.linalg.cond(M))
    except np.linalg.LinAlgError:
        cond = math.inf
    if not math.isfinite(cond) or cond > s.matrix_cond_warn:
        issues.append(
            {"code": "matrix_ill_conditioned", "severity": "warning",
             "condition": None if not math.isfinite(cond) else cond,
             "message": f"matrix condition number {cond:.2g} exceeds {s.matrix_cond_warn}"}
        )
    return issues


# ---------------------------------------------------------------------------
# Solver: fast vectorized non-negative least squares (FISTA)
# ---------------------------------------------------------------------------


def nnls_pixels(Y: np.ndarray, M: np.ndarray, max_iter: int = 120,
                tol: float = 1e-7) -> tuple[np.ndarray, int]:
    """
    Solve min_X ||X M^T - Y||_F^2  s.t. X >= 0, vectorized over rows of Y.

    Y: (P, C), M: (C, K) -> X: (P, K)
    """
    Y = np.asarray(Y, dtype=np.float64)
    M = np.asarray(M, dtype=np.float64)
    P = Y.shape[0]
    K = M.shape[1]
    MtM = M.T @ M
    # Lipschitz constant of the gradient of (1/2)||XM^T-Y||^2 w.r.t. X
    lip = 2.0 * float(np.linalg.eigvalsh(MtM)[-1])
    step = 1.0 / max(lip, 1e-12)

    X = np.zeros((P, K), dtype=np.float64)
    Z = X.copy()
    t = 1.0
    scale = max(float(np.linalg.norm(Y)), 1.0)
    used_iter = max_iter
    for it in range(1, max_iter + 1):
        grad = Z @ MtM - Y @ M
        X_next = Z - step * grad
        np.maximum(X_next, 0.0, out=X_next)
        t_next = (1.0 + math.sqrt(1.0 + 4.0 * t * t)) / 2.0
        mom = (t - 1.0) / t_next
        Z = X_next + mom * (X_next - X)
        delta = float(np.abs(X_next - X).sum()) / (X_next.size * max(scale, 1.0))
        X = X_next
        t = t_next
        if delta < tol:
            used_iter = it
            break
    return X.astype(np.float32), used_iter


def nnls_single(y: np.ndarray, M: np.ndarray) -> np.ndarray:
    x, _ = scipy_nnls(M, y)
    return x


# ---------------------------------------------------------------------------
# Whole-image parameters — frozen before fan-out
# ---------------------------------------------------------------------------


def _coarsen(image: np.ndarray, cap: int = 512) -> np.ndarray:
    """Block-average the full image down to at most cap x cap (every pixel
    contributes, so this is a genuine whole-image estimate)."""
    h, w = image.shape[:2]
    step = max(1, math.ceil(max(h, w) / cap))
    h2, w2 = math.ceil(h / step), math.ceil(w / step)
    acc = np.zeros((h2, w2) + image.shape[2:], dtype=np.float64)
    cnt = np.zeros((h2, w2) + (1,) * (image.ndim - 2), dtype=np.float64)
    for dy in range(step):
        for dx in range(step):
            block = image[dy::step, dx::step].astype(np.float64)
            acc[: block.shape[0], : block.shape[1]] += block
            cnt[: block.shape[0], : block.shape[1]] += 1
    return acc / cnt


def estimate_frozen_params(image: np.ndarray, M: np.ndarray, algorithm: str) -> dict:
    """
    Single place where whole-image statistics are estimated. Tiles never call
    this. Returns a JSON-serializable, hashable dict.
    """
    if image.ndim != 3:
        raise UnmixingError("image_bad_shape", f"expected HWC array, got {image.shape}")
    H, W, C = image.shape
    K = M.shape[1]
    issues = validate_matrix(M, C)
    hard = [i for i in issues if i["severity"] == "error"]
    if hard:
        raise UnmixingError(hard[0]["code"], hard[0]["message"])

    if not np.isfinite(image).all():
        raise UnmixingError("image_nonfinite", "source image contains NaN/Inf")

    dtype_max = float(np.iinfo(image.dtype).max if np.issubdtype(image.dtype, np.integer) else 1.0)

    # Whole-image saturation audit (per channel)
    sat_frac = [float(np.mean(image[..., c] >= dtype_max)) for c in range(C)]

    coarse = _coarsen(image)
    flat = coarse.reshape(-1, C)

    # Background: darkest robust fraction of pixels, per channel (whole image)
    bg = [float(np.quantile(flat[:, c], 0.005)) for c in range(C)]
    bg_sub = np.clip(flat - np.asarray(bg), 0.0, None)

    # One global solve on the coarse grid to learn noise scale & display scales
    Xc, iters = nnls_pixels(bg_sub, M)
    recon = Xc @ M.T
    resid = bg_sub - recon
    sigma = [float(1.4826 * np.median(np.abs(resid[:, c] - np.median(resid[:, c]))) + 1e-9)
             for c in range(C)]

    raw_scale = [float(max(np.quantile(bg_sub[:, c], 0.999), 1.0)) for c in range(C)]
    comp_scale = [float(max(np.quantile(Xc[:, k], 0.999), 1.0)) for k in range(K)]
    resid_abs_max = float(np.quantile(np.abs(resid), 0.999))
    resid_scale = max(resid_abs_max, 1.0)

    params = {
        "solver": SOLVER_VERSION,
        "algorithm": algorithm,
        "shape": [int(H), int(W), int(C), int(K)],
        "dtype_max": dtype_max,
        "background": bg,
        "noise_sigma": sigma,
        "display": {
            "raw_scale": raw_scale,
            "comp_scale": comp_scale,
            "resid_scale": resid_scale,
        },
        "saturation_fraction": sat_frac,
        "coarse_solver_iters": int(iters),
    }
    params["hash"] = params_hash(params)
    return params


def params_hash(params: dict) -> str:
    body = {k: v for k, v in params.items() if k != "hash"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def verify_frozen(params: dict) -> None:
    expect = params.get("hash")
    if not expect or params_hash(params) != expect:
        raise UnmixingError(
            "frozen_params_tampered",
            "whole-image parameter blob fails its hash check; refusing tile work",
        )


# ---------------------------------------------------------------------------
# Per-tile work (uses ONLY frozen parameters)
# ---------------------------------------------------------------------------


def unmix_tile(tile: np.ndarray, M: np.ndarray, frozen: dict) -> dict:
    """
    tile: uint16 (h, w, C). Returns components float32 (h,w,K) and signed
    residual float32 (h,w,C) in background-subtracted observed coordinates.
    """
    verify_frozen(frozen)
    if tile.ndim != 3 or tile.shape[2] != M.shape[0]:
        raise UnmixingError(
            "tile_corrupt",
            f"tile shape {tile.shape} inconsistent with matrix {M.shape}",
        )
    if not np.isfinite(tile).all():
        raise UnmixingError("tile_corrupt", "tile contains NaN/Inf")

    h, w, C = tile.shape
    bg = np.asarray(frozen["background"], dtype=np.float32)
    Y = tile.reshape(-1, C).astype(np.float32) - bg
    np.maximum(Y, 0.0, out=Y)
    X, iters = nnls_pixels(Y, M.astype(np.float32))
    recon = X @ M.T.astype(np.float32)
    residual = Y - recon  # signed

    comp = X.reshape(h, w, -1)
    resid = residual.reshape(h, w, C)

    stats = {
        "solver_iters": int(iters),
        "comp_max": [float(np.max(comp[..., k])) for k in range(comp.shape[2])],
        "resid_rms": float(np.sqrt(np.mean(resid**2))),
        "resid_max_abs": float(np.max(np.abs(resid))),
        "pixels": int(h * w),
    }
    return {"components": comp, "residual": resid, "stats": stats}


# ---------------------------------------------------------------------------
# Generation-level end-to-end metrics (known-composition validation)
# ---------------------------------------------------------------------------


def aggregate_generation_metrics(tile_payloads: list[dict], shape_hint=None) -> dict:
    """Weighted-mean residual RMS over finished tiles."""
    total_pixels = 0
    acc_sq = 0.0
    max_abs = 0.0
    for p in tile_payloads:
        n = p["pixels"]
        acc_sq += float(p["resid_rms"]) ** 2 * n
        max_abs = max(max_abs, float(p["resid_max_abs"]))
        total_pixels += n
    rms = math.sqrt(acc_sq / total_pixels) if total_pixels else None
    return {"reconstruction_rmse": rms, "resid_max_abs": max_abs,
            "tiles_aggregated": len(tile_payloads)}


def recovery_error_vs_truth(components: np.ndarray, truth: np.ndarray) -> dict:
    """
    Compare recovered abundance map against the known-composition truth.
    Component ordering may be permuted: match by cosine similarity of the
    total-intensity vectors.
    """
    K = components.shape[2]
    x = components.reshape(-1, K).astype(np.float64)
    t = truth.reshape(-1, K).astype(np.float64)
    # align scales: each recovered component is matched to a truth component
    # then rescaled by least squares with free scalar
    xn = x / (np.linalg.norm(x, axis=0, keepdims=True) + 1e-12)
    tn = t / (np.linalg.norm(t, axis=0, keepdims=True) + 1e-12)
    sim = tn.T @ xn
    perm = np.zeros(K, dtype=int)
    used = set()
    for k in range(K):
        order = np.argsort(-sim[:, k])
        for cand in order:
            if int(cand) not in used:
                perm[k] = int(cand)
                used.add(int(cand))
                break
    x_matched = x[:, perm]
    scales = np.sum(t * x_matched, axis=0) / (np.sum(x_matched**2, axis=0) + 1e-12)
    x_scaled = x_matched * scales
    rmse = float(np.sqrt(np.mean((x_scaled - t) ** 2)))
    rel = rmse / float(np.sqrt(np.mean(t**2)) + 1e-12)
    return {"recovery_rmse": rmse, "recovery_relative_rmse": rel,
            "component_permutation": perm.tolist(),
            "component_scales": scales.tolist()}
