import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Query, Body
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from fastembed import TextEmbedding

from app.db import repository
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
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": exc.detail,
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
        return {"ok": True, "db_ok": True, "embed_model": EMBED_MODEL_NAME}
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

    return {"bin_id": bin_id, "photos": saved}


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
        return {"item_id": item_id, "name": name, "category": category, "notes": notes, "bin_id": bin_id}

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
            getattr(db, "info", {}).get("request_id"),
            bin_id,
            item_id,
            len(saved_photos),
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
    return {
        "bin_id": bin_id,
        "items": items,
        "photos": photos,
    }


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
    return bins


@app.get("/photos/{photo_id}/suggest")
def suggest_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    return {"photo_id": photo_id, "suggestions": []}


@app.post("/photos/{photo_id}/detect")
def detect_for_photo(
    photo_id: int,
    db: Session = Depends(get_db),
):
    if not repository.photo_exists(db, photo_id):
        raise HTTPException(status_code=404, detail="photo not found")

    detections = []
    model = "stub"
    repository.insert_photo_detections(db, photo_id, model, detections)
    repository.clear_detection_groups(db, photo_id, model)
    db.commit()

    return {
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
    return {"photo_id": photo_id, "groups": groups}


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

    selected_groups = payload.get("selected_groups") or []
    if not isinstance(selected_groups, list):
        raise HTTPException(status_code=400, detail="selected_groups must be a list")

    try:
        repository.ensure_bin_active_or_create(db, bin_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="bin not found")

    model = "stub"
    items_out = []
    try:
        for g in selected_groups:
            label = (g.get("label") or "").strip()
            category = g.get("category")
            if not label:
                raise HTTPException(status_code=400, detail="label is required")
            quantity = g.get("quantity")

            item_id = repository.insert_item(db, label, category, None)
            repository.insert_bin_item(db, bin_id, item_id, None, quantity)
            repository.insert_photo_group_item(db, photo_id, model, label, category, item_id)

            items_out.append(
                {
                    "item_id": item_id,
                    "label": label,
                    "category": category,
                    "quantity": quantity,
                }
            )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"confirm failed: {e}")

    return {"photo_id": photo_id, "bin_id": bin_id, "items": items_out}


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
