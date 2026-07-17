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


# Only these tags trigger HTML->markdown conversion. Meld descriptions use angle-bracket
# placeholders like `<id>`, `<payment method>`, `<numeric amount>` that HTMLParser would
# otherwise parse as tags and silently strip. Convert only when a real HTML tag is present.
HTML_TAG_RE = re.compile(
    r"</?(?:br|p|div|li|ul|ol|b|strong|i|em|a|span|code|pre|h[1-6]|table|thead|tbody|tr|td|th|blockquote)\b[^>]*>",
    re.IGNORECASE,
)


def html_to_markdown(text: str) -> str:
    if not isinstance(text, str) or "<" not in text:
        return text
    if not HTML_TAG_RE.search(text):
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


def is_empty_placeholder(value) -> bool:
    """Only null/empty-string defaults and empty-string examples are generator noise.

    Keep every other non-empty annotation example exactly as the backend emitted it
    (including imperfect values like "USD or BTC" or "CHECKING, SAVINGS").
    """
    return value is None or value == ""


def has_nonempty_example_value(value) -> bool:
    """True when a requestBody example contains at least one non-empty leaf value."""
    if isinstance(value, dict):
        return any(has_nonempty_example_value(child) for child in value.values())
    if isinstance(value, list):
        return any(has_nonempty_example_value(child) for child in value)
    return value is not None and value != ""


def resolve_ref(ref: str, schemas: dict) -> dict | None:
    if not ref.startswith("#/components/schemas/"):
        return None
    name = ref.removeprefix("#/components/schemas/")
    target = schemas.get(name)
    return target if isinstance(target, dict) else None


def strip_empty_placeholders(node) -> int:
    """Remove only default:null, default:"", and example:"" from schema trees."""
    updated = 0
    if isinstance(node, dict):
        if "default" in node and is_empty_placeholder(node["default"]):
            del node["default"]
            updated += 1
        if "example" in node and node["example"] == "":
            del node["example"]
            updated += 1

        properties = node.get("properties")
        if isinstance(properties, dict):
            required = set(node.get("required") or [])
            # SpringDoc occasionally emits a spurious `example` property on request objects.
            if "example" in properties and "example" not in required:
                del properties["example"]
                updated += 1

        for value in node.values():
            updated += strip_empty_placeholders(value)
    elif isinstance(node, list):
        for value in node:
            updated += strip_empty_placeholders(value)
    return updated


def sanitize_component_schemas(payload: dict) -> int:
    schemas = payload.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        return 0

    return strip_empty_placeholders(schemas)


def build_property_example_value(prop: dict, schemas: dict, visited: set[str]):
    default = prop.get("default")
    if default is not None and not is_empty_placeholder(default):
        return default
    example = prop.get("example")
    if example is not None and example != "":
        return example
    enum_values = prop.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    if "$ref" in prop:
        nested = build_required_example({"$ref": prop["$ref"]}, schemas, visited)
        return nested

    # allOf-wrapped refs (common for polymorphic nested objects)
    for part in prop.get("allOf") or []:
        if isinstance(part, dict) and "$ref" in part:
            nested = build_required_example({"$ref": part["$ref"]}, schemas, visited)
            if nested is not None:
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
    # A required scalar with no example/default/enum still has to appear in the
    # sample, otherwise the generated request body silently omits a required
    # field. Fall back to a type placeholder (matching Mintlify's own prefill)
    # so the field is always present for the caller to fill in.
    if prop_type == "string":
        return "<string>"
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


def first_named_example_value(examples) -> dict | None:
    """Extract the first non-empty OpenAPI `examples.*.value` object."""
    if not isinstance(examples, dict):
        return None
    for example in examples.values():
        if not isinstance(example, dict):
            continue
        value = example.get("value")
        if isinstance(value, dict) and has_nonempty_example_value(value):
            return value
    return None


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

                # Preserve author/backend-provided examples. Springdoc emits named
                # `examples` from `@ExampleObject`; Mintlify also reads singular
                # `example`. Prefer those over synthesized required-only samples.
                existing = content.get("example")
                if isinstance(existing, dict) and has_nonempty_example_value(existing):
                    continue

                named_example = first_named_example_value(content.get("examples"))
                if named_example is not None:
                    if content.get("example") != named_example:
                        content["example"] = named_example
                        updated += 1
                    continue

                example = build_required_example(schema, schemas)
                if example is None:
                    if "example" in content:
                        del content["example"]
                        updated += 1
                    continue

                if content.get("example") != example:
                    content["example"] = example
                    updated += 1
    return updated


def wrap_ref_siblings(value) -> int:
    """Mintlify drops schema properties shaped as ``{$ref, ...siblings}`` (e.g. a $ref next to
    a description). OpenAPI 3.1 allows the siblings, but the renderer ignores the property.
    Rewrite such nodes into the ``allOf`` form Mintlify does render, scoped to schema refs so
    path-level response/parameter refs are left untouched."""
    updated = 0
    if isinstance(value, dict):
        ref = value.get("$ref")
        siblings = [key for key in value if key != "$ref"]
        if (
            isinstance(ref, str)
            and ref.startswith("#/components/schemas/")
            and siblings
        ):
            del value["$ref"]
            all_of = value.get("allOf")
            if isinstance(all_of, list):
                all_of.append({"$ref": ref})
            else:
                value["allOf"] = [{"$ref": ref}]
            updated += 1
        for child in value.values():
            updated += wrap_ref_siblings(child)
    elif isinstance(value, list):
        for child in value:
            updated += wrap_ref_siblings(child)
    return updated


def branch_schema_name(branch: dict) -> str | None:
    """Return the component schema name a oneOf/anyOf branch points at, if any."""
    if not isinstance(branch, dict):
        return None
    ref = branch.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return ref.removeprefix("#/components/schemas/")
    all_of = branch.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], dict):
        inner = all_of[0].get("$ref")
        if isinstance(inner, str) and inner.startswith("#/components/schemas/"):
            return inner.removeprefix("#/components/schemas/")
    return None


def annotate_polymorphic_titles(value) -> int:
    """Mintlify labels oneOf/anyOf tabs by each branch's `title`; without one it shows
    "Option 1/2/3", hiding which subtype (and therefore which discriminator value) each tab
    represents. Bare `{$ref}` branches have nowhere to carry a title, so tag each branch with
    its referenced schema name. Runs before wrap_ref_siblings, which folds the `{title, $ref}`
    pair into the `{title, allOf:[{$ref}]}` subschema form Mintlify renders as the tab label."""
    updated = 0
    if isinstance(value, dict):
        for key in ("oneOf", "anyOf"):
            branches = value.get(key)
            if not isinstance(branches, list):
                continue
            for branch in branches:
                if not isinstance(branch, dict) or "title" in branch:
                    continue
                name = branch_schema_name(branch)
                if name:
                    branch["title"] = name
                    updated += 1
        for child in value.values():
            updated += annotate_polymorphic_titles(child)
    elif isinstance(value, list):
        for child in value:
            updated += annotate_polymorphic_titles(child)
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


def normalize_spec(spec_path: Path, is_latest: bool = True) -> int:
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
    updated += annotate_polymorphic_titles(payload)
    updated += wrap_ref_siblings(payload)
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
            # UpdateReadMeOAS strips the date suffix from operationIds, so every version's
            # operation collapses to the same slug. Without disambiguation each version's
            # operation pins the same absolute href and they collide — Mintlify then serves a
            # single (often older) spec at that URL, dropping fields added in newer versions.
            # The latest/default version keeps the canonical clean URL; older versions get a
            # version-scoped route so the canonical URL is owned solely by the latest spec.
            if is_latest or not version_key:
                href = f"/api-reference/{service_route}/{tag_slug}/{operation_slug}"
            else:
                href = f"/api-reference/{service_route}/{version_display(version_key)}/{tag_slug}/{operation_slug}"
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

    # Determine the latest version per service so only it owns the canonical clean href.
    latest_version_by_service: dict[str, str] = {}
    for spec_path in root.glob("*.json"):
        service = spec_path.stem.rsplit("-", 1)[0]
        version_key = version_key_from_spec_path(spec_path)
        if version_key and version_key > latest_version_by_service.get(service, ""):
            latest_version_by_service[service] = version_key

    total = 0
    for spec_path in sorted(root.glob("*.json")):
        service = spec_path.stem.rsplit("-", 1)[0]
        version_key = version_key_from_spec_path(spec_path)
        is_latest = version_key is None or version_key == latest_version_by_service.get(service)
        total += normalize_spec(spec_path, is_latest=is_latest)
    print(f"Updated {total} OpenAPI operations in {root}")


if __name__ == "__main__":
    main()
