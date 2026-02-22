import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


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
    resources = {
        schema_item["$id"]: Resource.from_contents(schema_item, default_specification=DRAFT202012)
        for schema_item in store.values()
    }
    registry = Registry().with_resources(resources)
    Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(data)
