"""Helpers for extracting OCR + barcode data from stored device_metadata (S-DM-01).

FF-04 allowlist: only device_processing.ocr[{text,confidence}] and
device_processing.barcodes[{payload,symbology}] are extracted. All other
device_processing fields (device_model, ios_version, quality_scores,
classifications, crop_applied, device_id, imei, mac, serial) are ignored.
"""

from __future__ import annotations

_OCR_CAP = 10
_BARCODE_CAP = 10
_OCR_MIN_CONFIDENCE = 0.5


def extract_ocr_and_barcodes(device_metadata: dict | None) -> tuple[list[dict], list[dict]]:
    """Extract OCR results and barcodes from stored device_metadata.

    Returns (ocr_results, barcodes). Both are empty lists when metadata is
    absent or malformed. OCR entries below _OCR_MIN_CONFIDENCE are dropped.
    OCR entries whose text matches a barcode payload are de-duplicated (the
    barcode array is the authoritative source for that string).
    """
    if not device_metadata or not isinstance(device_metadata, dict):
        return [], []

    dp = device_metadata.get("device_processing")
    if not isinstance(dp, dict):
        return [], []

    # Barcodes first — used for OCR de-dup.
    raw_barcodes = dp.get("barcodes") or []
    barcodes: list[dict] = []
    barcode_payloads: set[str] = set()
    for b in raw_barcodes[:_BARCODE_CAP]:
        if not isinstance(b, dict):
            continue
        payload = b.get("payload")
        if not isinstance(payload, str) or not payload:
            continue
        barcodes.append({"payload": payload, "symbology": b.get("symbology") or ""})
        barcode_payloads.add(payload)

    # OCR — filter confidence, cap, de-dup against barcode payloads.
    raw_ocr = dp.get("ocr") or []
    ocr_results: list[dict] = []
    for entry in raw_ocr:
        if len(ocr_results) >= _OCR_CAP:
            break
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        conf = entry.get("confidence")
        if not isinstance(text, str) or not text:
            continue
        if not isinstance(conf, (int, float)) or conf < _OCR_MIN_CONFIDENCE:
            continue
        if text in barcode_payloads:
            continue
        ocr_results.append({"text": text, "confidence": float(conf)})

    return ocr_results, barcodes
