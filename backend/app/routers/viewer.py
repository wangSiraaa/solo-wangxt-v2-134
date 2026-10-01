"""
Tile serving for OpenSeadragon (three coordinated viewers: raw / components /
residual).

Cache safety is structural: the tile URL namespace is

    /viewer/jobs/{job}/generations/{gen}/{view}/.../L{level}/{x}_{y}.png

and the bytes live at object keys that embed image digest, matrix digest,
algorithm and generation. Responses are marked immutable: a matrix revision
changes every URL, so no browser can keep showing an old-coefficient tile
inside a new report.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import imaging
from ..config import get_settings
from ..database import get_db
from ..models import Generation, Image, Job, MatrixVersion, TileRecord
from ..object_store import get_store, tile_cache_key
from ..security import Principal, can_view_image, resolve_principal

router = APIRouter(prefix="/viewer", tags=["viewer"])

_VIEW_PREFIX = {"raw": "raw", "components": "components", "residual": "residual"}
_IMMUTABLE = "public, max-age=31536000, immutable"


def _load_authorized(db: Session, p: Principal, job_id: int,
                     gen_id: int) -> tuple[Job, Generation, Image]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    image = db.get(Image, job.image_id)
    if not can_view_image(db, p, image):
        raise HTTPException(403, "no view permission on this image")
    gen = db.get(Generation, gen_id)
    if gen is None or gen.job_id != job_id:
        raise HTTPException(404, "generation not found")
    return job, gen, image


def _dzi_xml(width: int, height: int, tile_size: int, layers: int,
             job_id: int, gen_id: int, view: str) -> str:
    # DZI's level 0 is the single-tile top; our L0 is full resolution.
    max_level = imaging.num_levels(height, width, tile_size) - 1
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Image TileSize="{tile_size}" Overlap="0" Format="png"
       xmlns="http://schemas.microsoft.com/deepzoom/2008"
       JobId="{job_id}" GenerationId="{gen_id}" View="{view}" Layers="{layers}"
       InternalBaseLevel="{max_level}">
  <Size Width="{width}" Height="{height}"/>
</Image>"""


@router.get("/jobs/{job_id}/generations/{gen_id}/{view}.dzi")
def dzi(job_id: int, gen_id: int, view: str, layer: int = 0,
        db: Session = Depends(get_db),
        p: Principal = Depends(resolve_principal)):
    if view not in _VIEW_PREFIX:
        raise HTTPException(404, "unknown view")
    job, _gen, image = _load_authorized(db, p, job_id, gen_id)
    if view == "raw":
        layers = image.channels
    elif view == "components":
        layers = len(db.get(MatrixVersion, job.matrix_version_id).matrix[0])
    else:
        layers = 1 + image.channels  # rms + signed per-channel
    if not (0 <= layer < layers):
        raise HTTPException(400, f"layer out of range (0..{layers - 1})")
    ts = get_settings().tile_size
    xml = _dzi_xml(image.width, image.height, ts, layers, job_id, gen_id, view)
    return Response(content=xml, media_type="application/xml")


@router.get("/jobs/{job_id}/generations/{gen_id}/{view}_files/{dzi_level}/{coord}.png")
def tile(job_id: int, gen_id: int, view: str, dzi_level: int, coord: str,
         layer: int = Query(0),
         db: Session = Depends(get_db),
         p: Principal = Depends(resolve_principal)):
    if view not in _VIEW_PREFIX:
        raise HTTPException(404, "unknown view")
    if "_" not in coord:
        raise HTTPException(404, "tile coordinate must be x_y")
    xs, ys = coord.split("_", 1)
    x, y = int(xs), int(ys)
    job, _gen, image = _load_authorized(db, p, job_id, gen_id)
    ts = get_settings().tile_size
    n_internal = imaging.num_levels(image.height, image.width, ts)
    internal_level = (n_internal - 1) - dzi_level
    if not (0 <= internal_level < n_internal):
        raise HTTPException(404, "level out of range")

    tr = db.scalar(
        select(TileRecord).where(
            TileRecord.generation_id == gen_id,
            TileRecord.level == internal_level,
            TileRecord.tile_x == x, TileRecord.tile_y == y,
        )
    )
    if tr is None:
        raise HTTPException(404, "tile not found")

    field = {"raw": tr.raw_key, "components": tr.comp_key,
             "residual": tr.resid_key}[view]
    if view == "raw":
        n_layers = image.channels
    elif view == "components":
        n_layers = len(db.get(MatrixVersion, job.matrix_version_id).matrix[0])
    else:
        n_layers = 1 + image.channels
    if not (0 <= layer < n_layers):
        raise HTTPException(400, "layer out of range")

    if tr.state != "done" or field is None:
        # Explicit partial-result marker: the viewer renders this as a hatched
        # placeholder rather than silently showing nothing or stale data.
        return Response(
            content=_placeholder_png(
                f"{tr.state}\nL{internal_level} {x},{y}"),
            media_type="image/png",
            headers={"X-Unmix-Tile-State": tr.state,
                     "Cache-Control": "no-store"},
        )

    keys = json.loads(field)
    data = get_store().get(keys[layer])

    # Defense-in-depth: verify bytes actually come from the pinned namespace.
    expected = tile_cache_key(
        job.image_digest, job.matrix_digest, job.algorithm, gen_id,
        internal_level, x, y, f"{view}{layer}",
    )
    same_namespace = (
        f"/img-{job.image_digest[:16]}/mat-{job.matrix_digest[:16]}"
        f"/{job.algorithm}/gen{gen_id:06d}/L{internal_level}/"
    )
    if same_namespace not in f"/{keys[layer]}":
        raise HTTPException(409, "tile key outside pinned generation namespace")

    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": _IMMUTABLE,
                             "ETag": f'"{job.image_digest[:16]}-{job.matrix_digest[:16]}-gen{gen_id}-L{internal_level}-{x}-{y}-{view}{layer}"',
                             "X-Unmix-Tile-State": "done",
                             "X-Unmix-Expected-Key": expected})


def _placeholder_png(label: str) -> bytes:
    from PIL import Image, ImageDraw

    ts = get_settings().tile_size
    img = Image.new("L", (min(ts, 256), min(ts, 256)), color=245)
    draw = ImageDraw.Draw(img)
    # hatch border + state text => unmistakable "partial result" marker
    for i in range(0, img.size[0], 16):
        draw.line([(i, 0), (i - img.size[0], img.size[1])], fill=200)
    draw.rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], outline=(120,))
    draw.text((8, 8), f"PARTIAL / {label}", fill=80)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
