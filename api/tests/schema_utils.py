import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, RefResolver


def load_schema_store() -> dict:
    schema_path = Path("/home/stephenfeather/binbrain-api-schemas.json")
    content = schema_path.read_text(encoding="utf-8")
    content = re.sub(r",\s*([}\]])", r"\1", content)
    raw = json.loads(content)
    store = {s["$id"]: s for s in raw}
    return store


def validate_schema(schema_id: str, data: dict) -> None:
    store = load_schema_store()
    schema = store[schema_id]
    resolver = RefResolver.from_schema(schema, store=store)
    Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(data)
