import json
import os
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

# Schema files live at the repo root (two levels above binbrain/api/).
# Override with BINBRAIN_SCHEMA_DIR env var for non-standard layouts (e.g. Pi).
_DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent
_SCHEMA_DIR = Path(os.environ.get("BINBRAIN_SCHEMA_DIR", str(_DEFAULT_SCHEMA_DIR)))


def load_schema_store() -> dict:
    store = {}

    schema_path = _SCHEMA_DIR / "binbrain-api-schemas.json"
    content = schema_path.read_text(encoding="utf-8")
    content = re.sub(r",\s*([}\]])", r"\1", content)
    raw = json.loads(content)
    store.update({s["$id"]: s for s in raw})

    error_path = _SCHEMA_DIR / "binbrain-error-schemas.json"
    error_raw = json.loads(error_path.read_text(encoding="utf-8"))
    store[error_raw["$id"]] = error_raw

    return store


def validate_schema(schema_id: str, data: dict) -> None:
    store = load_schema_store()
    schema = store[schema_id]
    resources = {
        schema_item["$id"]: Resource.from_contents(schema_item, default_specification=DRAFT202012)
        for schema_item in store.values()
    }
    registry = Registry().with_resources(resources.items())
    Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(data)
