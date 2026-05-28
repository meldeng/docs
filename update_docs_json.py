#!/usr/bin/env python3
"""Generate Mintlify docs.json for Meld's versioned API reference."""

import argparse
import copy
import json
from pathlib import Path

VERSION_ORDER = [
    "20260203",
    "20250304",
    "20240427",
    "20231219",
    "20231122",
    "20230911",
    "20230401",
    "20221110",
    "20220921",
    "20220719",
]

PUBLIC_VERSION_ORDER = [
    "20260203",
    "20250304",
    "20231219",
]

SECTIONS_BY_VERSION = {
    "20260203": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20250304": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20240427": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20231219": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20231122": ["serviceproviders"],
    "20230911": ["crypto", "banklinking", "payments", "customer"],
    "20230401": ["crypto", "payments", "customer"],
    "20221110": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20220921": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
    "20220719": ["crypto", "banklinking", "payments", "customer", "serviceproviders", "webhooks", "beta"],
}

TAB_LABELS = {
    "crypto": "Crypto",
    "banklinking": "Bank Linking",
    "payments": "Payments",
    "customer": "Customer",
    "serviceproviders": "Service Providers",
    "webhooks": "Webhooks",
    "beta": "Beta",
}

SERVICE_ROUTE_SEGMENTS = {
    "crypto": "crypto",
    "banklinking": "bank-linking",
    "payments": "payments",
    "customer": "customer",
    "serviceproviders": "service-providers",
    "webhooks": "webhooks",
    "beta": "beta",
}


def version_display(vkey: str) -> str:
    return f"{vkey[:4]}-{vkey[4:6]}-{vkey[6:8]}"


def extract_docs_tab(template_docs: dict) -> dict:
    nav = template_docs.get("navigation", {})
    for tab in nav.get("tabs", []):
        if tab.get("tab") == "Documentation":
            return copy.deepcopy(tab)
    for version in nav.get("versions", []):
        for tab in version.get("tabs", []):
            if tab.get("tab") == "Documentation":
                return copy.deepcopy(tab)
    raise ValueError("Could not find a Documentation tab in the template.")


def openapi_config(service: str, version_key: str) -> dict:
    version = version_display(version_key)
    route = SERVICE_ROUTE_SEGMENTS[service]
    return {
        "source": f"openapi/{service}-{version_key}.json",
        "directory": f"_generated-api/{version}/{route}",
    }


def api_groups_for_version(version_key: str) -> list:
    return [
        {
            "group": TAB_LABELS[service],
            "openapi": openapi_config(service, version_key),
        }
        for service in SECTIONS_BY_VERSION[version_key]
    ]


def api_reference_tab_for_version(version_key: str) -> dict:
    return {
        "tab": "API Reference",
        "groups": api_groups_for_version(version_key),
    }


def build_version_entry(version_key: str, docs_tab: dict, is_default: bool) -> dict:
    entry = {
        "version": version_display(version_key),
        "tabs": [
            copy.deepcopy(docs_tab),
            api_reference_tab_for_version(version_key),
        ],
    }
    if is_default:
        entry["default"] = True
        entry["tag"] = "Latest"
    return entry


def validate_openapi_sources(repo_root: Path):
    missing = []
    for version_key in VERSION_ORDER:
        for service in SECTIONS_BY_VERSION[version_key]:
            path = repo_root / f"openapi/{service}-{version_key}.json"
            if not path.exists():
                missing.append(path)
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(f"Missing OpenAPI files:\n{joined}")


def main():
    parser = argparse.ArgumentParser(description="Generate docs.json for Mintlify versioned API reference")
    parser.add_argument("--docs", default="docs.json", help="Path to docs.json output")
    parser.add_argument("--template", default="docs.template.json", help="Path to docs template")
    parser.add_argument("--version", default="all", help="Retained for workflow compatibility; docs.json is rebuilt in full")
    parser.add_argument("--service", default="all", help="Retained for workflow compatibility; docs.json is rebuilt in full")
    args = parser.parse_args()

    docs_path = Path(args.docs)
    template_path = Path(args.template)
    repo_root = docs_path.parent.resolve()

    template_docs = json.loads(template_path.read_text(encoding="utf-8"))
    docs_tab = extract_docs_tab(template_docs)
    validate_openapi_sources(repo_root)

    generated = {key: value for key, value in template_docs.items() if key != "navigation"}
    generated["navigation"] = {
        "versions": [
            build_version_entry(version_key, docs_tab, is_default=(index == 0))
            for index, version_key in enumerate(PUBLIC_VERSION_ORDER)
        ]
    }

    docs_path.write_text(json.dumps(generated, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {docs_path} with {len(PUBLIC_VERSION_ORDER)} public versions")


if __name__ == "__main__":
    main()
