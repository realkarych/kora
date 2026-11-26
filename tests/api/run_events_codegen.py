import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from kora.api.routes import RouteName
from kora.ir.json import JsonObject, JsonValue, parse_json_object

WEB_ROOT: Path = Path(__file__).resolve().parents[2] / "web"
SCHEMA_INPUT: Path = WEB_ROOT / "openapi/run-events.openapi.json"
GENERATED_OUTPUT: Path = WEB_ROOT / "src/api/generated/run-events"
SCHEMA_REFERENCE_PREFIX = "#/components/schemas/"


def schema_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def schema_references(value: JsonValue) -> Iterator[str]:
    if isinstance(value, dict):
        if "$ref" in value:
            reference = value["$ref"]
            assert isinstance(reference, str)
            assert reference.startswith(SCHEMA_REFERENCE_PREFIX)
            yield reference.removeprefix(SCHEMA_REFERENCE_PREFIX)
        for child in value.values():
            yield from schema_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from schema_references(child)


def project_run_events_schema(installed_schema: bytes) -> bytes:
    document = parse_json_object(json.loads(installed_schema))
    operation_path = RouteName.RUN_EVENTS.value
    paths = schema_object(document["paths"])
    path_item = schema_object(paths[operation_path])
    operation = schema_object(path_item["get"])
    assert operation["operationId"] == "list_run_events"
    path_metadata = {"parameters", "servers", "summary", "description"}
    selected_path = {key: path_item[key] for key in path_metadata & path_item.keys()}
    selected_path["get"] = operation
    schemas = schema_object(schema_object(document["components"])["schemas"])
    referenced: JsonObject = {}
    pending = set(schema_references(selected_path))
    while pending:
        name = pending.pop()
        schema = schemas[name]
        referenced[name] = schema
        pending.update(set(schema_references(schema)) - referenced.keys())
    selected_document = {
        key: value
        for key, value in document.items()
        if key not in {"paths", "components"}
    }
    selected_document["paths"] = {operation_path: selected_path}
    selected_document["components"] = {"schemas": referenced}
    return (json.dumps(selected_document, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def generated_files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def regenerate_run_events(output: Path) -> dict[str, bytes]:
    pnpm = shutil.which("pnpm")
    assert pnpm is not None
    result = subprocess.run(
        [
            pnpm,
            "exec",
            "openapi-ts",
            "--no-log-file",
            "-f",
            "openapi-ts.config.mjs",
            "-o",
            str(output),
        ],
        cwd=WEB_ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return generated_files(output)


def verify_run_events_codegen(installed_schema: bytes, tmp_path: Path) -> None:
    assert project_run_events_schema(installed_schema) == SCHEMA_INPUT.read_bytes()
    first = regenerate_run_events(tmp_path / "generated-first")
    second = regenerate_run_events(tmp_path / "generated-second")
    committed = generated_files(GENERATED_OUTPUT)
    assert first == second == committed
