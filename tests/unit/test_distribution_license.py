from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LICENSES = ROOT / "LICENSES"
POLYFORM_SHA256 = "c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


def test_official_polyform_text_is_vendored_unchanged() -> None:
    content = (LICENSES / "PolyForm-Noncommercial-1.0.0.md").read_bytes()

    assert hashlib.sha256(content).hexdigest() == POLYFORM_SHA256


def test_root_notice_requires_the_complete_community_license() -> None:
    license_notice = _normalized(ROOT / "LICENSE")
    notice = _normalized(ROOT / "NOTICE")

    for content in (license_notice, notice):
        assert "PolyForm Noncommercial 1.0.0" in content
        assert "TradingOPS Community Team Exception 1.0" in content
        assert "separately executed TradingOPS Commercial License" in content
        assert "Required Notice:" in content

    assert "not offered under an OSI-approved open-source license" in license_notice
    assert "grants no commercial rights by itself" in license_notice


def test_community_exception_freezes_product_boundary() -> None:
    exception = _read(LICENSES / "TradingOPS-Community-Team-Exception-1.0.md")

    assert "no more than three natural persons" in exception
    assert "Member-Owned Trading" in exception
    assert "beneficially owned solely" in exception
    assert "Noncommercial Organizations" in exception
    assert "replaced in full" in exception
    assert "Any use by or for an Organization requires" in exception
    for scope in ("SaaS", "Hosted or Managed Service", "White-Label", "Resale"):
        assert scope in exception
    assert "Authorization for one scope does not authorize any other scope" in exception


def test_commercial_template_is_not_self_executing() -> None:
    commercial = _read(LICENSES / "TradingOPS-Commercial-License-1.0.md")

    assert "No rights are granted by this repository copy" in commercial
    assert "LICENSOR" in commercial
    assert "LICENSEE" in commercial
    assert "FEE_AND_PAYMENT_TERMS" in commercial
    assert "GOVERNING_LAW" in commercial
    assert "sign it" in commercial


def test_package_metadata_uses_custom_dual_license_expression() -> None:
    project = tomllib.loads(_read(ROOT / "pyproject.toml"))["project"]

    assert project["license"] == (
        "LicenseRef-TradingOPS-Community-1.0 OR LicenseRef-TradingOPS-Commercial-1.0"
    )
    assert project["license-files"] == ["LICENSE", "NOTICE", "LICENSES/*"]


def test_contributions_include_dual_license_grant() -> None:
    contributing = _read(ROOT / "CONTRIBUTING.md")
    pull_request_template = _normalized(ROOT / ".github/pull_request_template.md")

    assert "Contributor license grant" in contributing
    assert "TradingOPS Commercial License" in contributing
    assert "Combined Community License" in pull_request_template
