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
    updated = normalize_descriptions(payload)
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
