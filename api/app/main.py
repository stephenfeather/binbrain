import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Query
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from fastembed import TextEmbedding

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


def get_db():
    db = SessionLocal()
    try:
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
def health():
    return {"ok": True, "embed_model": EMBED_MODEL_NAME}


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
    db.execute(
        text("INSERT INTO bins (bin_id) VALUES (:bin_id) ON CONFLICT (bin_id) DO NOTHING"),
        {"bin_id": bin_id},
    )
    db.commit()

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

        db.execute(
            text("INSERT INTO photos (bin_id, path) VALUES (:bin_id, :path)"),
            {"bin_id": bin_id, "path": str(fpath)},
        )
        db.commit()

        saved.append({"path": str(fpath)})

    return {"bin_id": bin_id, "count": len(saved), "saved": saved}


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
        res = db.execute(
            text("INSERT INTO items (name, category, notes) VALUES (:name, :category, :notes) RETURNING item_id"),
            {"name": name, "category": category, "notes": notes},
        )
        item_id = int(res.scalar_one())

        # 2) Create embedding
        vec = embed_text(canonical_item_text(name, category, notes))
        dims = len(vec)
        if dims != 384:
            raise HTTPException(status_code=500, detail=f"unexpected embedding dims {dims}, expected 384")
        vec_str = vec_to_pgvector(vec)

        # 3) Upsert embedding row
        db.execute(
            text("""
                INSERT INTO item_embeddings (item_id, model, dims, embedding)
                VALUES (:item_id, :model, :dims, CAST(:embedding AS vector))
                ON CONFLICT (item_id) DO UPDATE
                SET model = EXCLUDED.model,
                    dims = EXCLUDED.dims,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
            """),
            {"item_id": item_id, "model": EMBED_MODEL_NAME, "dims": dims, "embedding": vec_str},
        )

        # 4) Optional association to a bin
        if bin_id:
            bin_id = bin_id.strip()
            db.execute(
                text("INSERT INTO bins (bin_id) VALUES (:bin_id) ON CONFLICT (bin_id) DO NOTHING"),
                {"bin_id": bin_id},
            )
            db.execute(
                text("""
                    INSERT INTO bin_items (bin_id, item_id, confidence, quantity)
                    VALUES (:bin_id, :item_id, :confidence, :quantity)
                """),
                {"bin_id": bin_id, "item_id": item_id, "confidence": confidence, "quantity": quantity},
            )

        db.commit()
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

    db.execute(
        text("INSERT INTO bins (bin_id) VALUES (:bin_id) ON CONFLICT (bin_id) DO NOTHING"),
        {"bin_id": bin_id},
    )
    db.execute(
        text("""
            INSERT INTO bin_items (bin_id, item_id, confidence, quantity)
            VALUES (:bin_id, :item_id, :confidence, :quantity)
        """),
        {"bin_id": bin_id, "item_id": item_id, "confidence": confidence, "quantity": quantity},
    )
    db.commit()

    return {"ok": True, "bin_id": bin_id, "item_id": item_id}


@app.get("/bins/{bin_id}")
def get_bin(
    bin_id: str,
    db: Session = Depends(get_db),
):
    bin_id = (bin_id or "").strip()
    if not bin_id:
        raise HTTPException(status_code=400, detail="bin_id is required")

    exists = db.execute(
        text("SELECT 1 FROM bins WHERE bin_id = :bin_id"),
        {"bin_id": bin_id},
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="bin not found")

    items = db.execute(
        text("""
            SELECT
              i.item_id,
              i.name,
              i.category,
              bi.quantity,
              bi.confidence
            FROM bin_items bi
            JOIN items i ON i.item_id = bi.item_id
            WHERE bi.bin_id = :bin_id
            ORDER BY i.item_id
        """),
        {"bin_id": bin_id},
    ).mappings().all()

    photos = db.execute(
        text("""
            SELECT
              photo_id,
              path
            FROM photos
            WHERE bin_id = :bin_id
            ORDER BY photo_id
        """),
        {"bin_id": bin_id},
    ).mappings().all()

    return {
        "bin_id": bin_id,
        "items": [dict(row) for row in items],
        "photos": [dict(row) for row in photos],
    }


@app.get("/bins")
def list_bins(
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            WITH item_agg AS (
              SELECT
                bin_id,
                COUNT(*)::int AS item_count,
                MAX(created_at) AS last_item_at
              FROM bin_items
              GROUP BY bin_id
            ),
            photo_agg AS (
              SELECT
                bin_id,
                COUNT(*)::int AS photo_count,
                MAX(created_at) AS last_photo_at
              FROM photos
              GROUP BY bin_id
            )
            SELECT
              b.bin_id,
              COALESCE(ia.item_count, 0) AS item_count,
              COALESCE(pa.photo_count, 0) AS photo_count,
              GREATEST(
                b.created_at,
                COALESCE(ia.last_item_at, b.created_at),
                COALESCE(pa.last_photo_at, b.created_at)
              ) AS last_updated
            FROM bins b
            LEFT JOIN item_agg ia ON ia.bin_id = b.bin_id
            LEFT JOIN photo_agg pa ON pa.bin_id = b.bin_id
            ORDER BY last_updated DESC
        """),
    ).mappings().all()

    return [dict(row) for row in rows]


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    try:
        qvec = embed_text(q)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"query embedding failed: {e}")

    if len(qvec) != 384:
        raise HTTPException(status_code=500, detail=f"unexpected query embedding dims {len(qvec)}, expected 384")

    qvec_str = vec_to_pgvector(qvec)

    rows = db.execute(
        text("""
            SELECT
              i.item_id,
              i.name,
              i.category,
              (e.embedding <=> CAST(:qvec AS vector)) AS distance,
              array_remove(array_agg(bi.bin_id), NULL) AS bins
            FROM item_embeddings e
            JOIN items i ON i.item_id = e.item_id
            LEFT JOIN bin_items bi ON bi.item_id = i.item_id
            GROUP BY i.item_id, i.name, i.category, e.embedding
            ORDER BY e.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
        """),
        {"qvec": qvec_str, "limit": limit},
    ).mappings().all()

    return {"q": q, "limit": limit, "results": [dict(r) for r in rows]}
