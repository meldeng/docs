#!/usr/bin/env python3
"""Normalize Meld OpenAPI files for Mintlify versioned reference."""

import argparse
from html import unescape
from html.parser import HTMLParser
import json
import re
from pathlib import Path

SERVICE_ROUTE_SEGMENTS = {
    "crypto": "crypto",
    "banklinking": "bank-linking",
    "payments": "payments",
    "customer": "customer",
    "serviceproviders": "service-providers",
    "webhooks": "webhooks",
    "beta": "beta",
    "networkpartner": "network-partners",
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

PATH_PRIORITY_BY_SERVICE = {
    "crypto": [
        "/payments/crypto/quote",
        "/crypto/session/widget",
        "/payments/transactions/{id}",
        "/payments/transactions",
        "/payments/transactions/sessions/{sessionId}",
    ],
}

PLACEHOLDER_EXAMPLE_VALUES = {
    "<string>",
    "<unknown>",
    "string",
    "unknown",
}


def slugify(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def version_key_from_spec_path(spec_path: Path) -> str | None:
    _, version_key = spec_path.stem.rsplit("-", 1)
    if len(version_key) != 8 or not version_key.isdigit():
        return None
    return version_key


def version_display(version_key: str) -> str:
    return f"{version_key[:4]}-{version_key[4:6]}-{version_key[6:8]}"


def sync_openapi_info_version(payload: dict, version: str) -> int:
    """Backend exports info.version as 'unversioned'; align with the dated API version."""
    updated = 0
    info = payload.setdefault("info", {})
    if info.get("version") != version:
        info["version"] = version
        updated += 1

    readme = payload.setdefault("x-readme", {})
    headers = readme.setdefault("headers", [])
    for header in headers:
        if isinstance(header, dict) and header.get("key") == "Meld-Version":
            if header.get("value") != version:
                header["value"] = version
                updated += 1
            return updated

    headers.append({"key": "Meld-Version", "value": version})
    return updated + 1


def fallback_operation_slug(method: str, path: str) -> str:
    path_slug = slugify(path.replace("{", "").replace("}", ""))
    return f"{method}-{path_slug}"


class MarkdownHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.link_stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n\n")
        elif tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag == "a":
            self.link_stack.append({"href": attrs.get("href", ""), "text": []})

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n\n")
        elif tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag == "a" and self.link_stack:
            link = self.link_stack.pop()
            text = "".join(link["text"]).strip()
            href = link["href"]
            if text and href:
                self.parts.append(f"[{text}]({href})")
            elif text:
                self.parts.append(text)

    def handle_data(self, data):
        if self.link_stack:
            self.link_stack[-1]["text"].append(data)
        else:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(unescape(f"&{name};"))

    def handle_charref(self, name):
        self.handle_data(unescape(f"&#{name};"))

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


def html_to_markdown(text: str) -> str:
    if not isinstance(text, str) or "<" not in text:
        return text
    parser = MarkdownHTMLParser()
    parser.feed(text)
    parser.close()
    return parser.markdown()


def normalize_descriptions(value) -> int:
    updated = 0
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"description", "summary"} and isinstance(child, str):
                normalized = html_to_markdown(child)
                if normalized != child:
                    value[key] = normalized
                    updated += 1
            else:
                updated += normalize_descriptions(child)
    elif isinstance(value, list):
        for child in value:
            updated += normalize_descriptions(child)
    return updated


def is_placeholder_example(value) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if normalized in PLACEHOLDER_EXAMPLE_VALUES:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    return False


def should_drop_example(value) -> bool:
    if is_placeholder_example(value):
        return True
    if isinstance(value, str) and " or " in value.lower():
        return True
    return False


def resolve_ref(ref: str, schemas: dict) -> dict | None:
    if not ref.startswith("#/components/schemas/"):
        return None
    name = ref.removeprefix("#/components/schemas/")
    target = schemas.get(name)
    return target if isinstance(target, dict) else None


def sanitize_object_schema(schema: dict) -> int:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return 0

    updated = 0
    required = set(schema.get("required") or [])

    # SpringDoc occasionally emits a spurious `example` property on request objects.
    if "example" in properties and "example" not in required:
        del properties["example"]
        updated += 1

    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue

        if name not in required:
            if "example" in prop:
                del prop["example"]
                updated += 1
            if "default" in prop:
                del prop["default"]
                updated += 1
            continue

        default = prop.get("default")
        example = prop.get("example")
        if default is not None:
            if example != default:
                prop["example"] = default
                updated += 1
        elif "example" in prop and should_drop_example(example):
            del prop["example"]
            updated += 1

    return updated


def sanitize_schema_node(schema: dict, schemas: dict, visited: set[str] | None = None) -> int:
    if not isinstance(schema, dict):
        return 0

    visited = visited or set()
    updated = 0

    ref = schema.get("$ref")
    if isinstance(ref, str):
        ref_name = ref.removeprefix("#/components/schemas/")
        if ref_name in visited:
            return 0
        target = resolve_ref(ref, schemas)
        if target is not None:
            visited.add(ref_name)
            updated += sanitize_schema_node(target, schemas, visited)
        return updated

    if schema.get("type") == "object" or isinstance(schema.get("properties"), dict):
        updated += sanitize_object_schema(schema)

    items = schema.get("items")
    if isinstance(items, dict):
        updated += sanitize_schema_node(items, schemas, visited)

    return updated


def sanitize_component_schemas(payload: dict) -> int:
    schemas = payload.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        return 0

    updated = 0
    for schema in schemas.values():
        updated += sanitize_schema_node(schema, schemas)
    return updated


def build_property_example_value(prop: dict, schemas: dict, visited: set[str]):
    if "default" in prop:
        return prop["default"]
    example = prop.get("example")
    if example is not None and not should_drop_example(example):
        return example
    enum_values = prop.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    if "$ref" in prop:
        nested = build_required_example({"$ref": prop["$ref"]}, schemas, visited)
        return nested

    prop_type = prop.get("type")
    if prop_type == "object" or isinstance(prop.get("properties"), dict):
        return build_required_example(prop, schemas, visited)
    if prop_type == "array":
        return []
    if prop_type == "boolean":
        return False
    if prop_type in {"number", "integer"}:
        return 0
    return None


def build_required_example(schema: dict, schemas: dict, visited: set[str] | None = None) -> dict | None:
    if not isinstance(schema, dict):
        return None

    visited = set(visited or ())
    working_schema = schema

    ref = schema.get("$ref")
    if isinstance(ref, str):
        ref_name = ref.removeprefix("#/components/schemas/")
        if ref_name in visited:
            return None
        target = resolve_ref(ref, schemas)
        if target is None:
            return None
        visited.add(ref_name)
        working_schema = target

    if working_schema.get("type") != "object" and not isinstance(working_schema.get("properties"), dict):
        return None

    properties = working_schema.get("properties") or {}
    required = working_schema.get("required") or []
    if not required:
        return None

    example = {}
    for name in required:
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        value = build_property_example_value(prop, schemas, visited)
        if value is not None:
            example[name] = value
    return example or None


def inject_required_only_request_examples(payload: dict) -> int:
    """Mintlify prefill uses requestBody examples; without one it fabricates <string> for optional fields."""
    schemas = payload.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        return 0

    updated = 0
    for operations in payload.get("paths", {}).values():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            request_body = operation.get("requestBody")
            if not isinstance(request_body, dict):
                continue
            for media_type, content in (request_body.get("content") or {}).items():
                if not isinstance(content, dict) or "json" not in media_type:
                    continue
                schema = content.get("schema")
                if not isinstance(schema, dict):
                    continue

                example = build_required_example(schema, schemas)
                if example is None:
                    if "example" in content:
                        del content["example"]
                        updated += 1
                    if "examples" in content:
                        del content["examples"]
                        updated += 1
                    continue

                if content.get("example") != example:
                    content["example"] = example
                    updated += 1
                if "examples" in content:
                    del content["examples"]
                    updated += 1
    return updated


def reorder_paths(payload: dict, service: str) -> int:
    path_priority = PATH_PRIORITY_BY_SERVICE.get(service)
    paths = payload.get("paths")
    if not path_priority or not isinstance(paths, dict):
        return 0

    priority = {path: index for index, path in enumerate(path_priority)}
    reordered_items = sorted(
        paths.items(),
        key=lambda item: (priority.get(item[0], len(priority)), list(paths).index(item[0])),
    )
    reordered_paths = dict(reordered_items)
    if list(reordered_paths) == list(paths):
        return 0

    payload["paths"] = reordered_paths
    return 1


def normalize_spec(spec_path: Path) -> int:
    service = spec_path.stem.rsplit("-", 1)[0]
    service_route = SERVICE_ROUTE_SEGMENTS.get(service)
    if not service_route:
        return 0

    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    updated = 0
    version_key = version_key_from_spec_path(spec_path)
    if version_key:
        updated += sync_openapi_info_version(payload, version_display(version_key))
    updated += normalize_descriptions(payload)
    updated += sanitize_component_schemas(payload)
    updated += inject_required_only_request_examples(payload)
    updated += reorder_paths(payload, service)

    for path_name, operations in payload.get("paths", {}).items():
        for method, operation in operations.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            tag = (operation.get("tags") or [service_route])[0]
            tag_slug = slugify(tag)
            operation_slug = (operation.get("operationId") or "").strip("/")
            if not operation_slug:
                operation_slug = fallback_operation_slug(method.lower(), path_name)
            href = f"/api-reference/{service_route}/{tag_slug}/{operation_slug}"
            mint_config = operation.setdefault("x-mint", {})
            if mint_config.get("href") != href:
                mint_config["href"] = href
                updated += 1

    spec_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return updated


def main():
    parser = argparse.ArgumentParser(description="Normalize Mintlify hrefs inside OpenAPI specs")
    parser.add_argument("directory", nargs="?", default="openapi", help="Directory containing OpenAPI JSON files")
    args = parser.parse_args()

    root = Path(args.directory)
    total = 0
    for spec_path in sorted(root.glob("*.json")):
        total += normalize_spec(spec_path)
    print(f"Updated {total} OpenAPI operations in {root}")


if __name__ == "__main__":
    main()
