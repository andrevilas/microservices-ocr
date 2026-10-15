"""Export the implemented HTTP v1 contract without creating runtime state."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from app.http_contract import is_v1


def v1_schema(application):
    schema = deepcopy(application.openapi())
    schema["paths"] = {path: value for path, value in schema["paths"].items() if is_v1(path)}
    definitions = schema["components"].get("schemas", {})
    used = set()

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in used:
                    used.add(name)
                    visit(definitions[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema["paths"])
    schema["components"]["schemas"] = {name: definitions[name] for name in sorted(used)}
    schema["components"]["securitySchemes"] = {"PrincipalBearer": schema["components"]["securitySchemes"]["PrincipalBearer"]}
    return schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true", help="Fail if the existing export differs; write nothing.")
    args = parser.parse_args()
    from app.main import create_app
    content = json.dumps(v1_schema(create_app()), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != content:
            parser.exit(1, "OpenAPI export is missing or stale. Regenerate with python -m app.export_openapi.\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
