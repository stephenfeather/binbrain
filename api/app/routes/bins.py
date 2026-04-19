import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from app.db import repository
from app.deps import (
    EMBED_MODEL_NAME,
    MAX_FILE_BYTES,
    MAX_FILES_PER_REQUEST,
    canonical_item_text,
    embed_text,
    get_db,
    logger,
    photo_root,
    vec_to_pgvector,
)
from app.security import validate_bin_id
from app.services.image_validation import validate_image_file
from app.services.metadata_schema import validate_device_metadata
from app.services.upc_lookup import validate_upc
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Dev2_016: session_id is client-supplied, opaque, length-capped. Reject
# non-printable ASCII and anything longer than this so a misbehaving client
# cannot smuggle arbitrary bytes through a grouping key.
_SESSION_ID_MAX_LEN = 128


class BinLocationUpdate(BaseModel):
    location_id: int | None = None


class PhotoRecord(BaseModel):
    """Public photo shape. `url` points at authenticated /photos/{id}/file.

    F-10: the internal filesystem `path` and FF-04 `device_metadata` are
    never exposed on this record.
    """

    photo_id: int
    url: str


class BinItemRecord(BaseModel):
    item_id: int
    name: str
    category: Optional[str] = None
    upc: Optional[str] = None
    quantity: Optional[float] = None
    confidence: Optional[float] = None
    source_photo_id: Optional[int] = None
    source_bbox: Optional[list[float]] = None


class GetBinResponse(BaseModel):
    version: str = "1"
    bin_id: str
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    items: list[BinItemRecord]
    photos: list[PhotoRecord]


router = APIRouter()

_PHOTO_ROOT_RESOLVED: Path | None = None

# FF-04: allowlist of photo fields safe to expose in user-plane responses.
# The photos row also carries `path` (F-10: internal FS path) and
# `device_metadata` (FF-04: OCR/classification/telemetry) — neither may leak.
# Admin endpoints needing those fields must project them explicitly.
# `url` is synthesized server-side in `_public_photo` from `photo_id`.
_PUBLIC_PHOTO_FIELDS = frozenset({"photo_id", "url"})


def _public_photo(photo_id: int) -> dict:
    """Build the user-plane photo record. F-10: never includes internal path."""
    return {"photo_id": photo_id, "url": f"/photos/{photo_id}/file"}


def _get_photo_root_resolved() -> Path:
    """Return the resolved photo_root, cached after first call."""
    global _PHOTO_ROOT_RESOLVED
    if _PHOTO_ROOT_RESOLVED is None:
        _PHOTO_ROOT_RESOLVED = photo_root.resolve()
    return _PHOTO_ROOT_RESOLVED


def _assert_within_photo_root(path: Path) -> None:
    """Raise HTTPException(400) if *path* resolves outside photo_root."""
    root = _get_photo_root_resolved()
    resolved = path.resolve()
    if not str(resolved).startswith(str(root) + os.sep) and resolved != root:
        raise HTTPException(status_code=400, detail="invalid bin_id")


def _check_bin_id(raw: str) -> str:
    """Validate bin_id; raise HTTPException(400) on failure."""
    try:
        return validate_bin_id(raw.strip() if raw else "")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid bin_id: must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ "
                "(no path separators, dots, or special characters)"
            ),
        ) from None


async def _stream_upload(up: UploadFile, dest: Path) -> None:
    """Stream *up* to *dest* in 64 KiB chunks, enforcing MAX_FILE_BYTES.

    Raises HTTPException(413) if the file exceeds the per-file size limit.
    On error, *dest* is unlinked if it exists.
    """
    written = 0
    try:
        async with aiofiles.open(dest, "wb") as f:
            while True:
                chunk = await up.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {MAX_FILE_BYTES}-byte per-file limit",
                    )
                await f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="upload failed") from exc


def _validate_and_place(tmp: Path, final: Path) -> None:
    """Validate image type, then move tmp → final.  Unlinks tmp on rejection.

    Creates final.parent lazily (only on success) so a rejected upload leaves
    no empty directories under photo_root.  Uses shutil.move() to handle
    cross-filesystem moves when tmp lives in the system temp dir.

    Raises HTTPException(415) if the file is not an allowed image type.
    """
    try:
        validate_image_file(tmp)
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=415,
            detail="unsupported file type",
        ) from None
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(final))


@router.post("/ingest")
async def ingest(
    request: Request,
    bin_id: str = Form(...),
    photos: list[UploadFile] = File(...),
    device_metadata: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    bin_id = _check_bin_id(bin_id)

    # F-04: enforce file count before touching any file data.
    if len(photos) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files: max {MAX_FILES_PER_REQUEST} per request",
        )

    # Parse and validate device_metadata (F-11: size cap + top-level key allowlist).
    parsed_metadata = None
    if device_metadata:
        try:
            parsed_metadata = validate_device_metadata(device_metadata)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="device_metadata must be valid JSON"
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Dev2_016: session_id is opaque but must be short and printable ASCII so
    # a misbehaving client can't smuggle bytes through a grouping key. Reject
    # before the file loop so no photo rows are written on a bad request.
    if session_id is not None and session_id != "":
        if len(session_id) > _SESSION_ID_MAX_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"session_id exceeds {_SESSION_ID_MAX_LEN}-char limit",
            )
        if not all(0x20 <= ord(c) <= 0x7E for c in session_id):
            raise HTTPException(
                status_code=400,
                detail="session_id must be printable ASCII",
            )

        # ApiDev_008: validate that the session exists, is owned by this api
        # key, and is still open. Every failure mode collapses to the same
        # `invalid_session` error code so callers cannot enumerate sessions.
        api_key_id = getattr(request.state, "api_key_id", None)
        if api_key_id is None or not repository.validate_session_for_ingest(
            db, session_id, int(api_key_id)
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_session",
                    "message": ("Session not found, not yours, or already closed"),
                },
            )
    else:
        session_id = None

    # Ensure bin exists
    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found") from None
    db.commit()

    logger.info(
        "event=ingest_start request_id=%s bin_id=%s count=%s",
        db.info.get("request_id"),
        bin_id,
        len(photos),
    )

    bin_dir = photo_root / bin_id

    # F-01: belt-and-suspenders path confinement (checked before any I/O).
    _assert_within_photo_root(bin_dir)

    saved = []
    for up in photos:
        ext = os.path.splitext(up.filename or "")[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
            ext = ".tmp"

        fname = f"{uuid.uuid4().hex}{ext}"
        # F-05: write to system temp dir (outside photo_root) so a rejected
        # upload leaves no residue under photo_root.  bin_dir is created
        # lazily inside _validate_and_place only on success.
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".upload_tmp")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        final_path = bin_dir / fname

        # F-04: stream to temp; F-05: validate and rename.
        await _stream_upload(up, tmp_path)
        _validate_and_place(tmp_path, final_path)

        # Dev2_016: capture pixel dimensions (after EXIF rotation) so downstream
        # training can back-map normalized bboxes without re-reading files.
        # Best-effort: if PIL can't decode the placed file, insert NULL dims and
        # log — ingest must not fail because of a metadata-only read.
        pw: int | None
        ph: int | None
        try:
            with Image.open(final_path) as img:
                pw, ph = ImageOps.exif_transpose(img).size
        except (UnidentifiedImageError, OSError) as exc:
            logger.warning(
                "event=ingest_dims_failed request_id=%s photo_path=%s err=%s",
                db.info.get("request_id"),
                final_path,
                exc,
            )
            pw = ph = None

        photo_id = repository.insert_photo(
            db,
            bin_id,
            str(final_path),
            device_metadata=parsed_metadata,
            width=pw,
            height=ph,
            session_id=session_id,
        )
        db.commit()

        saved.append(_public_photo(photo_id))

    logger.info(
        "event=ingest_complete request_id=%s bin_id=%s saved=%s",
        db.info.get("request_id"),
        bin_id,
        len(saved),
    )

    if saved:
        logger.info(
            "event=ingest_photo_ids request_id=%s bin_id=%s photo_ids=%s",
            db.info.get("request_id"),
            bin_id,
            [p["photo_id"] for p in saved],
        )

    return {"version": "1", "bin_id": bin_id, "photos": saved}


@router.post("/bins/{bin_id:path}/add")
async def add_to_bin(
    bin_id: str,
    name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    upc: Optional[str] = Form(None),
    confidence: Optional[float] = Form(None),
    quantity: Optional[float] = Form(None),
    photos: Optional[list[UploadFile]] = File(None),
    db: Session = Depends(get_db),
):
    bin_id = _check_bin_id(bin_id)

    # F-04: enforce file count before touching any file data.
    if photos and len(photos) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files: max {MAX_FILES_PER_REQUEST} per request",
        )

    name = (name or "").strip() if name is not None else None
    if name == "":
        name = None

    upc = (upc or "").strip() or None
    if upc and not validate_upc(upc):
        raise HTTPException(status_code=400, detail="invalid UPC format (expected 12 or 13 digits)")

    try:
        try:
            repository.ensure_bin_active_or_create(db, bin_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="bin not found") from None

        item_id = None
        if upc and not name:
            # UPC-only path: look up existing item, skip insert
            existing = repository.find_item_by_upc(db, upc)
            if existing:
                item_id = existing["item_id"]
        if name:
            # If UPC already exists on a different item, use that item
            if upc:
                existing = repository.find_item_by_upc(db, upc)
                if existing:
                    item_id = existing["item_id"]
            if item_id is None:
                item_id = repository.insert_item(db, name, category, notes, upc=upc)

            vec = embed_text(canonical_item_text(name, category, notes))
            dims = len(vec)
            if dims != 384:
                raise HTTPException(
                    status_code=500, detail=f"unexpected embedding dims {dims}, expected 384"
                )
            vec_str = vec_to_pgvector(vec)

            repository.upsert_item_embedding(db, item_id, EMBED_MODEL_NAME, dims, vec_str)

        saved_photos = []
        if photos:
            bin_dir = photo_root / bin_id

            # F-01: belt-and-suspenders path confinement (checked before any I/O).
            _assert_within_photo_root(bin_dir)

            for up in photos:
                ext = os.path.splitext(up.filename or "")[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
                    ext = ".tmp"

                fname = f"{uuid.uuid4().hex}{ext}"
                # F-05: write to system temp dir so rejected uploads leave no
                # residue under photo_root.  bin_dir created lazily on success.
                tmp_fd, tmp_name = tempfile.mkstemp(suffix=".upload_tmp")
                os.close(tmp_fd)
                tmp_path = Path(tmp_name)
                final_path = bin_dir / fname

                await _stream_upload(up, tmp_path)
                _validate_and_place(tmp_path, final_path)

                photo_id = repository.insert_photo(db, bin_id, str(final_path))
                saved_photos.append(_public_photo(photo_id))

        if item_id is not None:
            repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)

        db.commit()

        logger.info(
            "event=bin_add request_id=%s bin_id=%s item_id=%s photos=%s",
            db.info.get("request_id"),
            bin_id,
            item_id,
            len(saved_photos),
        )
        if saved_photos:
            logger.info(
                "event=bin_add_photo_ids request_id=%s bin_id=%s photo_ids=%s",
                db.info.get("request_id"),
                bin_id,
                [p["photo_id"] for p in saved_photos],
            )
        return {
            "bin_id": bin_id,
            "item_id": item_id,
            "name": name,
            "category": category,
            "notes": notes,
            "upc": upc,
            "photos": saved_photos,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="internal error") from None


@router.get("/bins/{bin_id}", response_model=GetBinResponse)
def get_bin(
    bin_id: str,
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    exists = repository.bin_exists(db, bin_id)
    if not exists:
        raise HTTPException(status_code=404, detail="bin not found")

    items = repository.fetch_bin_items(db, bin_id)
    photos = repository.fetch_bin_photos(db, bin_id)

    loc_info = repository.fetch_bin_location(db, bin_id)
    location_id = loc_info["location_id"]
    location_name = loc_info["location_name"]

    logger.info(
        "event=bin_get request_id=%s bin_id=%s items=%s photos=%s",
        db.info.get("request_id"),
        bin_id,
        len(items),
        len(photos),
    )
    # F-10 / FF-04: build the user-plane photo record from `photo_id` only.
    # Strips internal filesystem `path` (F-10) and operational `device_metadata`
    # (FF-04 — OCR/classification/telemetry payloads must not leak to the
    # user-plane response). The `url` field is synthesized server-side so
    # the allowlist stays honest even if the DB row gains new columns.
    safe_photos = [_public_photo(p["photo_id"]) for p in photos]
    return {
        "version": "1",
        "bin_id": bin_id,
        "location_id": location_id,
        "location_name": location_name,
        "items": items,
        "photos": safe_photos,
    }


@router.get("/bins")
def list_bins(
    db: Session = Depends(get_db),
):
    bins = repository.list_bins(db)
    logger.info(
        "event=bins_list request_id=%s count=%s",
        db.info.get("request_id"),
        len(bins),
    )
    return {"version": "1", "bins": bins}


@router.delete("/bins/{bin_id}/items/{item_id}")
def remove_item_from_bin(
    bin_id: str,
    item_id: int,
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    if not repository.bin_exists(db, bin_id):
        raise HTTPException(status_code=404, detail="bin not found")

    removed = repository.delete_bin_item(db, bin_id, item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="item not in bin")

    db.commit()
    logger.info(
        "event=bin_item_delete request_id=%s bin_id=%s item_id=%s",
        db.info.get("request_id"),
        bin_id,
        item_id,
    )
    return {"version": "1", "bin_id": bin_id, "item_id": item_id, "removed": True}


@router.patch("/bins/{bin_id}/items/{item_id}")
def update_item_in_bin(
    bin_id: str,
    item_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    if not repository.bin_exists(db, bin_id):
        raise HTTPException(status_code=404, detail="bin not found")

    quantity = payload.get("quantity")
    confidence = payload.get("confidence")

    if quantity is None and confidence is None:
        raise HTTPException(
            status_code=400, detail="at least one of quantity or confidence is required"
        )

    updated = repository.update_bin_item(
        db, bin_id, item_id, quantity=quantity, confidence=confidence
    )
    if not updated:
        raise HTTPException(status_code=404, detail="item not in bin")

    db.commit()
    logger.info(
        "event=bin_item_update request_id=%s bin_id=%s item_id=%s quantity=%s confidence=%s",
        db.info.get("request_id"),
        bin_id,
        item_id,
        quantity,
        confidence,
    )
    return {
        "version": "1",
        "bin_id": bin_id,
        "item_id": item_id,
        "quantity": quantity,
        "confidence": confidence,
    }


@router.delete("/bins/{bin_id}")
def delete_bin(
    bin_id: str,
    db: Session = Depends(get_db),
):
    """Soft-delete a bin and reattribute its items to UNASSIGNED.

    FEAT-4-route. Calls ``reattribute_bin_items_to_unassigned`` (PR #28),
    then stamps ``deleted_at = now()`` on the bin row in the same
    transaction. The sentinel ``UNASSIGNED`` is preempted with a clean
    400 + custom error code so the protective trigger never fires.
    """
    # SEC-31-1: defer to the shared validator so DELETE matches the same
    # path-safety regex as POST/PATCH on bins (rejects path separators,
    # dots, anything outside ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$).
    bin_id = _check_bin_id(bin_id)

    if bin_id == repository.UNASSIGNED_BIN_ID:
        # Preempt the protect_unassigned_bin trigger with a stable,
        # client-facing error code. The global HTTPException handler maps
        # 400 → "bad_request" by default; this branch needs a more specific
        # code so we render the envelope inline.
        return JSONResponse(
            status_code=400,
            content={
                "version": "1",
                "error": {
                    "code": "cannot_delete_sentinel",
                    "message": "The UNASSIGNED bin cannot be deleted.",
                    "request_id": db.info.get("request_id"),
                },
            },
            headers={"x-request-id": db.info.get("request_id") or ""},
        )

    moved_item_count = repository.reattribute_bin_items_to_unassigned(db, bin_id)
    deleted_at = repository.soft_delete_bin(db, bin_id)
    if deleted_at is None:
        # Bin was missing or already soft-deleted. Roll back the (no-op or
        # already-applied) reattribute so the partial work doesn't commit.
        db.rollback()
        raise HTTPException(status_code=404, detail="bin not found")

    db.commit()
    logger.info(
        "event=bin_delete request_id=%s bin_id=%s moved_item_count=%s",
        db.info.get("request_id"),
        bin_id,
        moved_item_count,
    )
    return {
        "status": "deleted",
        "bin_id": bin_id,
        "moved_item_count": moved_item_count,
        "deleted_at": deleted_at.isoformat(),
    }


@router.patch("/bins/{bin_id}/location")
def update_bin_location(
    bin_id: str,
    payload: BinLocationUpdate,
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    if not repository.bin_exists(db, bin_id):
        raise HTTPException(status_code=404, detail="bin not found")

    loc = None
    if payload.location_id is not None:
        loc = repository.get_location(db, payload.location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="location not found")

    updated = repository.update_bin_location(db, bin_id, payload.location_id)
    if not updated:
        raise HTTPException(status_code=404, detail="bin not found")

    db.commit()

    loc_name = loc["name"] if loc else None

    logger.info(
        "event=bin_location_update request_id=%s bin_id=%s location_id=%s",
        db.info.get("request_id"),
        bin_id,
        payload.location_id,
    )
    return {
        "version": "1",
        "bin_id": bin_id,
        "location_id": payload.location_id,
        "location_name": loc_name,
    }
