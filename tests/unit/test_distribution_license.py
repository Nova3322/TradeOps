from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LICENSES = ROOT / "LICENSES"
GPL_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
LICENSE_EXPRESSION = "GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


def test_official_gpl_text_is_vendored_unchanged() -> None:
    content = (LICENSES / "GPL-3.0-only.txt").read_bytes()

    assert hashlib.sha256(content).hexdigest() == GPL_SHA256


def test_root_notice_describes_the_dual_license_without_restricting_gpl_commerce() -> None:
    license_notice = _normalized(ROOT / "LICENSE")
    notice = _normalized(ROOT / "NOTICE")

    for content in (license_notice, notice):
        assert LICENSE_EXPRESSION in content
        assert "permits commercial use" in content
        assert "separate" in content and "commercial" in content.lower()
    assert "does not reduce or replace rights already received under the GPL" in license_notice
    assert "THIRD_PARTY_NOTICES.md" in notice


def test_commercial_reference_is_not_self_executing_and_preserves_gpl_rights() -> None:
    commercial = _normalized(LICENSES / "LicenseRef-TradingOPS-Commercial-1.0.txt")

    assert "not an offer and not a grant of commercial rights" in commercial
    assert "written agreement" in commercial
    assert "Nothing in a commercial agreement" in commercial
    assert "The GPL permits commercial use" in commercial
    assert "Third-party components" in commercial


def test_package_metadata_uses_the_public_dual_license_expression() -> None:
    pyproject = tomllib.loads(_read(ROOT / "pyproject.toml"))
    project = pyproject["project"]

    assert project["license"] == LICENSE_EXPRESSION
    assert project["license-files"] == ["LICENSE", "NOTICE", "LICENSES/*"]
    assert (
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)" in project["classifiers"]
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "docs/AI_API_QUICKSTART.md": "trading_control_plane/web/AI_API_QUICKSTART.md",
        "docs/API_KEY_QUICKSTART.md": "trading_control_plane/web/API_KEY_QUICKSTART.md",
    }


def test_contributions_include_explicit_dual_license_cla_grant() -> None:
    contributing = _read(ROOT / "CONTRIBUTING.md")
    cla = _read(ROOT / "CLA.md")
    pull_request_template = _normalized(ROOT / ".github/pull_request_template.md")

    assert "Contributor License Agreement" in contributing
    assert LICENSE_EXPRESSION in contributing
    assert "retain copyright ownership" in cla
    assert "relicense the contribution" in cla
    assert "GPL-3.0-only" in cla and "commercial licenses" in cla
    assert "CLA.md" in pull_request_template


def test_third_party_inventory_and_sbom_cover_bundled_fonts_and_dependencies() -> None:
    notices = _read(ROOT / "THIRD_PARTY_NOTICES.md")
    sbom = json.loads(_read(ROOT / "sbom.cdx.json"))

    assert "IBM Plex" in notices and "SIL Open Font License 1.1" in notices
    assert (LICENSES / "IBM-Plex-OFL-1.1.txt").is_file()
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert len(sbom["components"]) >= 100
    assert all(
        component.get("licenses") and "NOASSERTION" not in str(component["licenses"])
        for component in sbom["components"]
    )
