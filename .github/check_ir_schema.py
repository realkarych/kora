import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import cast

from check_jsonschema.formats import FormatOptions
from check_jsonschema.regex_variants import RegexImplementation, RegexVariantName
from check_jsonschema.schema_loader import SchemaLoader


def check(directory: Path) -> dict[str, object]:
    if version("check-jsonschema") != "0.38.2":
        raise ValueError("the checker version differs")
    corpus = cast(
        "dict[str, object]", json.loads((directory / "corpus.json").read_text())
    )
    cases = cast("list[dict[str, object]]", corpus["cases"])
    regex = RegexImplementation(RegexVariantName.default)
    results: dict[str, object] = {}
    for mode in ("committed", "validation", "serialization"):
        path = directory / f"{mode}.schema.json"
        loader = SchemaLoader(str(path))
        outcomes: list[dict[str, object]] = []
        for case in cases:
            document = cast("dict[str, object]", case["document"])
            validator = loader.get_validator(
                directory / "input.json",
                document,
                FormatOptions(regex_impl=regex),
                regex,
                False,
            )
            actual = validator.is_valid(document)
            if actual is not case["valid"]:
                raise ValueError("the independent schema corpus differs")
            outcomes.append(
                {"name": case["name"], "expected": case["valid"], "actual": actual}
            )
        checker = validator.format_checker
        if checker is None or "date-time" not in checker.checkers:
            raise ValueError("the timestamp format checker is missing")
        results[mode] = {
            "schema_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "metaschema_valid": True,
            "uri_format_registered": "uri" in checker.checkers,
            "date_time_format_registered": "date-time" in checker.checkers,
            "cases": outcomes,
        }
    return {
        "checker": version("check-jsonschema"),
        "python": sys.version.split()[0],
        "schemas": results,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("expected checker input directory")
    sys.stdout.write(json.dumps(check(Path(sys.argv[1])), indent=2) + "\n")
