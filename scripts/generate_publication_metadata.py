#!/usr/bin/env python3
"""Generate deterministic public SBOM and third-party license inventory."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SBOM_PATH = ROOT / "sbom.cdx.json"
NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
PROJECT_LICENSE = "GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0"

# Metadata overrides are version-pinned to uv.lock and sourced from the matching
# PyPI release metadata when a dependency is not installed in the generator venv.
LOCKED_LICENSE_OVERRIDES = {
    ("colorama", "0.4.6"): "BSD-3-Clause",
    ("greenlet", "3.5.3"): "MIT AND PSF-2.0",
}

CLASSIFIER_LICENSES = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_for_distribution(distribution: importlib.metadata.Distribution) -> str:
    metadata = distribution.metadata
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    found: list[str] = []
    for classifier in metadata.get_all("Classifier", []):
        if classifier.startswith("License :: OSI Approved :: "):
            label = classifier.rsplit(" :: ", 1)[-1]
            mapped = CLASSIFIER_LICENSES.get(label)
            if mapped and mapped not in found:
                found.append(mapped)
        elif classifier == "License :: Public Domain" and "LicenseRef-Public-Domain" not in found:
            found.append("LicenseRef-Public-Domain")
    if found:
        return " OR ".join(found)
    raw = " ".join((metadata.get("License") or "").strip().split())
    aliases = {
        "Apache": "Apache-2.0",
        "Apache 2.0": "Apache-2.0",
        "Apache-2.0": "Apache-2.0",
        "BSD, Public Domain": "BSD-3-Clause OR LicenseRef-Public-Domain",
        "Dual License": "BSD-3-Clause OR Apache-2.0",
        "ISC license": "ISC",
        "MIT": "MIT",
        "MIT License": "MIT",
        "MPL-2.0": "MPL-2.0",
        "PSF-2.0": "PSF-2.0",
        "Simplified BSD": "BSD-2-Clause",
        "3-Clause BSD License": "BSD-3-Clause",
        "BSD-3-Clause": "BSD-3-Clause",
    }
    return aliases.get(raw, "NOASSERTION")


def _installed_python_licenses() -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            result[(_normalized(name), distribution.version)] = _license_for_distribution(
                distribution
            )
    return result


def _run_json(command: list[str]) -> dict:
    environment = os.environ.copy()
    environment.setdefault(
        "UV_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "tradingops-uv-cache"),
    )
    completed = subprocess.run(  # noqa: S603 - fixed internal command list
        command,
        cwd=ROOT,
        check=True,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    return json.loads(completed.stdout)


def _license_expression(component: dict) -> str:
    expressions: list[str] = []
    for item in component.get("licenses", []):
        if "expression" in item:
            expressions.append(item["expression"])
        elif item.get("license", {}).get("id"):
            expressions.append(item["license"]["id"])
        elif item.get("license", {}).get("name"):
            expressions.append(item["license"]["name"])
    return " OR ".join(expressions) or "NOASSERTION"


def _generate() -> tuple[str, str]:
    python = _run_json(
        [
            "uv",
            "export",
            "--preview-features",
            "sbom-export",
            "--format",
            "cyclonedx1.5",
            "--all-groups",
            "--frozen",
        ]
    )
    node = _run_json(["npm", "sbom", "--package-lock-only", "--sbom-format", "cyclonedx"])
    installed = _installed_python_licenses()

    components: list[dict] = []
    inventory: list[tuple[str, str, str, str]] = []
    for component in python.get("components", []):
        component = dict(component)
        key = (_normalized(component["name"]), component.get("version", ""))
        license_expression = installed.get(key, LOCKED_LICENSE_OVERRIDES.get(key, "NOASSERTION"))
        component["licenses"] = [{"expression": license_expression}]
        component.setdefault("properties", []).append(
            {"name": "tradingops:ecosystem", "value": "Python"}
        )
        components.append(component)
        inventory.append(
            (component["name"], component.get("version", ""), "Python", license_expression)
        )
    for component in node.get("components", []):
        component = dict(component)
        component.setdefault("properties", []).append(
            {"name": "tradingops:ecosystem", "value": "Node.js"}
        )
        components.append(component)
        inventory.append(
            (
                component["name"],
                component.get("version", ""),
                "Node.js",
                _license_expression(component),
            )
        )

    lock_digest = hashlib.sha256(
        (ROOT / "uv.lock").read_bytes() + (ROOT / "package-lock.json").read_bytes()
    ).hexdigest()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"tradingops:{lock_digest}")
    refs = sorted(component["bom-ref"] for component in components)
    dependencies = [
        dependency
        for document in (python, node)
        for dependency in document.get("dependencies", [])
        if dependency.get("ref") in refs
    ]
    dependencies.append({"ref": "tradingops@0.1.0", "dependsOn": refs})
    sbom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "tradingops@0.1.0",
                "name": "TradingOPS",
                "version": "0.1.0",
                "licenses": [{"expression": PROJECT_LICENSE}],
            },
            "properties": [
                {"name": "tradingops:lock-sha256", "value": lock_digest},
                {"name": "tradingops:generation", "value": "deterministic-from-lockfiles"},
            ],
        },
        "components": sorted(
            components,
            key=lambda item: (
                next(
                    (
                        prop["value"]
                        for prop in item.get("properties", [])
                        if prop.get("name") == "tradingops:ecosystem"
                    ),
                    "",
                ),
                item["name"].lower(),
                item.get("version", ""),
            ),
        ),
        "dependencies": sorted(dependencies, key=lambda item: item["ref"]),
    }
    sbom_text = json.dumps(sbom, indent=2, sort_keys=False, ensure_ascii=False) + "\n"

    rows = [
        "# Third-Party Notices",
        "",
        "This inventory is generated from the locked Python and Node.js dependency graphs. "
        "Each dependency remains subject to its own license; this file does not replace "
        "the upstream license text.",
        "",
        "The bundled IBM Plex font files are licensed under SIL Open Font License 1.1. "
        "See `LICENSES/IBM-Plex-OFL-1.1.txt` and `LICENSES/IBM-Plex-NOTICE.md`.",
        "",
        "The machine-readable dependency inventory is `sbom.cdx.json` (CycloneDX 1.5).",
        "",
        "| Package | Version | Ecosystem | Declared license |",
        "| --- | --- | --- | --- |",
    ]
    for name, version, ecosystem, license_expression in sorted(
        inventory, key=lambda item: (item[2], item[0].lower(), item[1])
    ):
        rows.append(f"| `{name}` | `{version}` | {ecosystem} | `{license_expression}` |")
    rows.extend(
        [
            "",
            "## Review status",
            "",
            "The inventory records upstream package metadata, including `NOASSERTION` when "
            "a package does not expose an unambiguous machine-readable license. Release review "
            "must resolve every `NOASSERTION` and retain any attribution files required by the "
            "selected distribution format.",
            "",
        ]
    )
    return sbom_text, "\n".join(rows)


def main() -> int:
    check = "--check" in sys.argv[1:]
    sbom_text, notices_text = _generate()
    expected = {SBOM_PATH: sbom_text, NOTICES_PATH: notices_text}
    if check:
        stale = [
            str(path.relative_to(ROOT))
            for path, content in expected.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            print("stale publication metadata: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("publication metadata is current")
        return 0
    for path, content in expected.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
