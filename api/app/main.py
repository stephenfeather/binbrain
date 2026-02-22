import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Query, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from fastembed import TextEmbedding

from app.db import repository
from app.services.detection import detect
DATABASE_URL = os.environ["DATABASE_URL"]
PHOTO_DIR = os.environ.get("PHOTO_DIR", "/data/photos")
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

app = FastAPI(title="BinBrain API")

photo_root = Path(PHOTO_DIR)
photo_root.mkdir(parents=True, exist_ok=True)

# Local CPU text embeddings (bge-small-en-v1.5 => 384 dims)
embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("binbrain")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    status_code = exc.status_code
    code_map = {
        400: "bad_request",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
        500: "internal_error",
        503: "service_unavailable",
    }
    error_code = code_map.get(status_code, "bad_request" if status_code == 422 else "internal_error")
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=status_code,
        content={
            "version": "1",
            "error": {
                "code": error_code,
                "message": message,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "version": "1",
            "error": {
                "code": "bad_request",
                "message": "validation error",
                "details": exc.errors(),
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("event=unhandled_error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(
        status_code=500,
        content={
            "version": "1",
            "error": {
                "code": "internal_error",
                "message": "internal server error",
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        headers={"x-request-id": getattr(request.state, "request_id", "")},
    )


def get_db(request: Request):
    db = SessionLocal()
    try:
        db.info["request_id"] = getattr(request.state, "request_id", None)
        yield db
    finally:
        db.close()


def canonical_item_text(name: str, category: Optional[str], notes: Optional[str]) -> str:
    parts = [f"name: {name}"]
    if category:
        parts.append(f"category: {category}")
    if notes:
        parts.append(f"notes: {notes}")
    return "\n".join(parts)


def fingerprint_for(name: str, category: Optional[str]) -> str:
    name_part = (name or "").strip().lower()
    cat_part = (category or "").strip().lower()
    return f"{name_part}|{cat_part}"


def embed_text(s: str) -> list[float]:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty text")
    vec = next(embedder.embed([s]))
    return vec.tolist()


def vec_to_pgvector(vec: list[float]) -> str:
    # pgvector accepts a string like: [0.1,0.2,...]
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"version": "1", "ok": True, "db_ok": True, "embed_model": EMBED_MODEL_NAME, "expected_dims": 384}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")


@app.post("/ingest")
async def ingest(
    bin_id: str = Form(...),
    photos: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    bin_id = bin_id.strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    # Ensure bin exists
    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found")
    db.commit()

    logger.info(
        "event=ingest_start request_id=%s bin_id=%s count=%s",
        db.info.get("request_id"),
        bin_id,
        len(photos),
    )

    saved = []
    bin_dir = photo_root / bin_id
    bin_dir.mkdir(parents=True, exist_ok=True)

    for up in photos:
        ext = os.path.splitext(up.filename or "")[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
            ext = ext if ext else ".bin"

        fname = f"{uuid.uuid4().hex}{ext}"
        fpath = bin_dir / fname
        fpath.write_bytes(await up.read())

        photo_id = repository.insert_photo(db, bin_id, str(fpath))
        db.commit()

        saved.append({"photo_id": photo_id, "path": str(fpath)})

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


@app.post("/items")
def create_item(
    name: str = Form(...),
    category: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    bin_id: Optional[str] = Form(None),
    confidence: Optional[float] = Form(None),
    quantity: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    try:
        # 1) Insert item (no commit yet)
        item_id = repository.insert_item(db, name, category, notes)

        # 2) Create embedding
        vec = embed_text(canonical_item_text(name, category, notes))
        dims = len(vec)
        if dims != 384:
            raise HTTPException(status_code=500, detail=f"unexpected embedding dims {dims}, expected 384")
        vec_str = vec_to_pgvector(vec)

        # 3) Upsert embedding row
        repository.upsert_item_embedding(db, item_id, EMBED_MODEL_NAME, dims, vec_str)

        # 4) Optional association to a bin
        if bin_id:
            bin_id = bin_id.strip()
            try:
                repository.ensure_bin_active_or_create(db, bin_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="bin not found")
            repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)

        db.commit()
        logger.info(
            "event=item_create request_id=%s item_id=%s bin_id=%s",
            db.info.get("request_id"),
            item_id,
            bin_id,
        )
        return {
            "version": "1",
            "item_id": item_id,
            "fingerprint": fingerprint_for(name, category),
            "name": name,
            "category": category,
            "notes": notes,
            "bin_id": bin_id,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"create_item failed: {e}")


@app.post("/associate")
def associate_item(
    bin_id: str = Form(...),
    item_id: int = Form(...),
    confidence: Optional[float] = Form(None),
    quantity: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found")
    repository.insert_bin_item(db, bin_id, item_id, confidence, quantity)
    db.commit()

    logger.info(
        "event=item_associate request_id=%s bin_id=%s item_id=%s",
        db.info.get("request_id"),
        bin_id,
        item_id,
    )
    return {"ok": True, "bin_id": bin_id, "item_id": item_id}


@app.post("/bins/{bin_id}/add")
async def add_to_bin(
    bin_id: str,
    name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    confidence: Optional[float] = Form(None),
    quantity: Optional[float] = Form(None),
    photos: Optional[list[UploadFile]] = File(None),
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    name = (name or "").strip() if name is not None else None
    if name == "":
        name = None

    try:
        try:
            repository.ensure_bin_active_or_create(db, bin_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="bin not found")

        item_id = None
        if name:
            item_id = repository.insert_item(db, name, category, notes)

            vec = embed_text(canonical_item_text(name, category, notes))
            dims = len(vec)
            if dims != 384:
                raise HTTPException(status_code=500, detail=f"unexpected embedding dims {dims}, expected 384")
            vec_str = vec_to_pgvector(vec)

            repository.upsert_item_embedding(db, item_id, EMBED_MODEL_NAME, dims, vec_str)

        saved_photos = []
        if photos:
            bin_dir = photo_root / bin_id
            bin_dir.mkdir(parents=True, exist_ok=True)

            for up in photos:
                ext = os.path.splitext(up.filename or "")[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
                    ext = ext if ext else ".bin"

                fname = f"{uuid.uuid4().hex}{ext}"
                fpath = bin_dir / fname
                fpath.write_bytes(await up.read())

                photo_id = repository.insert_photo(db, bin_id, str(fpath))
                saved_photos.append({"photo_id": photo_id, "path": str(fpath)})

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
            "photos": saved_photos,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"add_to_bin failed: {e}")


@app.get("/bins/{bin_id}")
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

    logger.info(
        "event=bin_get request_id=%s bin_id=%s items=%s photos=%s",
        db.info.get("request_id"),
        bin_id,
        len(items),
        len(photos),
    )
    return {"version": "1", "bin_id": bin_id, "items": items, "photos": photos}


@app.get("/bins")
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


@app.get("/photos/{photo_id}/suggest")
def suggest_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    logger.info(
        "event=photo_suggest request_id=%s photo_id=%s",
        db.info.get("request_id"),
        photo_id,
    )
    suggestions = []
    suggestions.sort(key=lambda s: (-s.get("confidence", 0.0), s.get("name", "")))
    return {"version": "1", "photo_id": photo_id, "suggestions": suggestions}


@app.post("/photos/{photo_id}/detect")
def detect_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    photo_path = repository.fetch_photo_path(db, photo_id)
    if not photo_path:
        raise HTTPException(status_code=404, detail="photo not found")

    detections = detect(photo_path)
    model = "stub"
    repository.insert_photo_detections(db, photo_id, model, detections)
    repository.clear_detection_groups(db, photo_id, model)
    db.commit()

    logger.info(
        "event=photo_detect request_id=%s photo_id=%s model=%s detections=%s",
        db.info.get("request_id"),
        photo_id,
        model,
        len(detections),
    )
    return {
        "version": "1",
        "photo_id": photo_id,
        "model": model,
        "detections": detections,
    }


@app.get("/photos/{photo_id}/groups")
def groups_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    model = "stub"
    groups = repository.fetch_cached_groups(db, photo_id, model)
    if not groups:
        groups = repository.compute_groups_from_detections(db, photo_id, model)
        repository.clear_detection_groups(db, photo_id, model)
        repository.insert_detection_groups(db, photo_id, model, groups)
        db.commit()
    logger.info(
        "event=photo_groups request_id=%s photo_id=%s model=%s groups=%s",
        db.info.get("request_id"),
        photo_id,
        model,
        len(groups),
    )
    return {"version": "1", "photo_id": photo_id, "model": model, "groups": groups}


@app.post("/photos/{photo_id}/confirm")
def confirm_photo_groups(
    photo_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    bin_id = (payload.get("bin_id") or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    version = payload.get("version")
    if version != "1":
        raise HTTPException(status_code=400, detail="version must be '1'")

    selected_groups = payload.get("selected_groups") or []
    if not isinstance(selected_groups, list):
        raise HTTPException(status_code=400, detail="selected_groups must be a list")

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found")

    model = "stub"
    results = []
    try:
        for g in selected_groups:
            group_key = (g.get("group_key") or "").strip()
            label = (g.get("label") or "").strip()
            category = (g.get("category") or "").strip()
            if not group_key:
                raise HTTPException(status_code=400, detail="group_key is required")
            if not label:
                raise HTTPException(status_code=400, detail="label is required")
            if not category:
                raise HTTPException(status_code=400, detail="category is required")
            quantity = g.get("quantity")

            item_id, inserted = repository.insert_item_with_status(db, label, category, None)
            linked = repository.insert_bin_item(db, bin_id, item_id, None, quantity)
            repository.insert_photo_group_item(db, photo_id, model, label, category, item_id)

            status = "created" if inserted else "updated"
            if not linked:
                status = "linked"

            results.append(
                {
                    "group_key": group_key or fingerprint_for(label, category),
                    "item_id": item_id,
                    "fingerprint": fingerprint_for(label, category),
                    "status": status,
                }
            )

        db.commit()
        logger.info(
            "event=photo_confirm request_id=%s photo_id=%s bin_id=%s item_ids=%s",
            db.info.get("request_id"),
            photo_id,
            bin_id,
            [i["item_id"] for i in results],
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"confirm failed: {e}")

    return {"version": "1", "photo_id": photo_id, "bin_id": bin_id, "results": results}


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    try:
        qvec = embed_text(q)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"query embedding failed: {e}")

    if len(qvec) != 384:
        raise HTTPException(status_code=500, detail=f"unexpected query embedding dims {len(qvec)}, expected 384")

    qvec_str = vec_to_pgvector(qvec)

    rows = repository.search_items(db, qvec_str, limit, offset, min_score)

    logger.info(
        "event=search request_id=%s limit=%s offset=%s min_score=%s results=%s",
        db.info.get("request_id"),
        limit,
        offset,
        min_score,
        len(rows),
    )
    return {
        "q": q,
        "limit": limit,
        "offset": offset,
        "min_score": min_score,
        "results": rows,
    }
