"""External UPC barcode lookup with graceful degradation.

Primary:  upcitemdb.com free tier (100 req/day, 6 req/min, no API key required)
Fallback: go-upc.com (stub — requires API key, not yet integrated)

All public functions return rather than raise. Callers receive a UPCResult
with source="unknown" when all services fail or the UPC is not found.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("binbrain")

_UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
_TIMEOUT = 5  # seconds
_MAX_BODY_BYTES = 256 * 1024  # 256 KB cap on upcitemdb response body (SEC-24-1)


def _project_upcitemdb_response(body: dict) -> dict:
    """Project upstream response to an allow-listed subset (SEC-24-2).

    Preserves audit value (what upstream said about THIS UPC) without
    hoarding unbounded ancillary fields (image URLs, descriptions,
    offers[], arbitrary keys) that have no business use.
    """
    items = body.get("items") or []
    first_item = items[0] if items else {}
    item_allowlist = {"title", "category", "brand", "upc", "ean"}
    projected_item = {k: first_item.get(k) for k in item_allowlist if k in first_item}
    return {
        "code": body.get("code"),
        "total": body.get("total"),
        "items": [projected_item] if projected_item else [],
    }


@dataclass
class UPCResult:
    name: Optional[str]
    category: Optional[str]
    brand: Optional[str]
    source: str  # "upcitemdb" | "go-upc" | "unknown"
    # ApiDev2_002 (Gap #7): provenance fields. ``raw_response`` carries the
    # full upstream body for non-local successful hits so the route can
    # persist it to ``item_upc_lookups.raw_response``; ``None`` for the
    # ``unknown`` fallback and for lookup failures. ``elapsed_ms`` records
    # the external call latency (``None`` for the ``unknown`` fallback that
    # made no network call). Additive and optional to preserve existing
    # call sites.
    raw_response: Optional[dict] = None
    elapsed_ms: Optional[int] = None


def validate_upc(upc: str) -> bool:
    """Return True for a valid UPC-A (12 digits) or EAN-13 (13 digits) string."""
    if not upc or not upc.isdigit():
        return False
    return len(upc) in (12, 13)


def extract_upc_from_device_metadata(metadata: dict | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    dp = metadata.get("device_processing")
    if not isinstance(dp, dict):
        return None
    barcodes = dp.get("barcodes")
    if not isinstance(barcodes, list):
        return None
    for bc in barcodes:
        if not isinstance(bc, dict):
            continue
        payload = bc.get("payload")
        if isinstance(payload, str) and validate_upc(payload):
            return payload
    return None


def _simplify_category(raw: str | None) -> str | None:
    """Extract the first segment from a Google taxonomy string.

    upcitemdb returns categories like "Electronics > Computers > Laptops".
    BinBrain only wants the top-level segment.
    """
    if not raw:
        return None
    return raw.split(" > ")[0].strip() or None


def _lookup_upcitemdb(upc: str) -> UPCResult | None:
    """Query upcitemdb.com free tier. Returns UPCResult or None on any failure."""
    if not upc.isdigit():
        return None
    url = f"{_UPCITEMDB_URL}?upc={upc}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_MAX_BODY_BYTES + 1)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if len(raw) > _MAX_BODY_BYTES:
            logger.warning(
                "event=upc_lookup_external source=upcitemdb upc=%s elapsed_ms=%s status=oversize size=%s",
                upc,
                elapsed_ms,
                len(raw),
            )
            return None
        body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            "event=upc_lookup_external source=upcitemdb upc=%s elapsed_ms=%s status=http_error code=%s",
            upc,
            elapsed_ms,
            exc.code,
        )
        return None
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            "event=upc_lookup_external source=upcitemdb upc=%s elapsed_ms=%s status=error error=%s",
            upc,
            elapsed_ms,
            str(exc)[:200],
        )
        return None

    if body.get("code") != "OK" or not body.get("items"):
        logger.info(
            "event=upc_lookup_external source=upcitemdb upc=%s elapsed_ms=%s status=not_found",
            upc,
            elapsed_ms,
        )
        return None

    item = body["items"][0]
    result = UPCResult(
        name=item.get("title") or None,
        category=_simplify_category(item.get("category")),
        brand=item.get("brand") or None,
        source="upcitemdb",
        # ApiDev2_002 (Gap #7): carry upstream body + latency to the route
        # so ``item_upc_lookups`` records provenance.
        # ApiDev2_002b (SEC-24-2): project to an allow-listed subset before
        # persist — attacker-influenced upstream fields are not trusted to
        # sit unbounded in prod jsonb.
        raw_response=_project_upcitemdb_response(body),
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "event=upc_lookup_external source=upcitemdb upc=%s elapsed_ms=%s status=ok name=%s",
        upc,
        elapsed_ms,
        result.name,
    )
    return result


def _lookup_goupc(upc: str) -> UPCResult | None:
    """Go-UPC fallback. Stub — requires API key, not yet integrated."""
    return None


def lookup_upc(upc: str) -> UPCResult:
    """Try all UPC lookup sources in priority order.

    Always returns a UPCResult. If all sources fail or the UPC is unknown,
    returns UPCResult(source="unknown") with null name/category/brand.
    """
    result = _lookup_upcitemdb(upc)
    if result and result.name:
        return result

    result = _lookup_goupc(upc)
    if result and result.name:
        return result

    return UPCResult(name=None, category=None, brand=None, source="unknown")
