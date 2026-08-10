from __future__ import annotations

import hashlib
import re
from pathlib import Path

STYLESHEET = (
    Path(__file__).parents[2]
    / "src"
    / "trading_control_plane"
    / "web"
    / "styles.css"
)
LOGO = STYLESHEET.with_name("tradingops-logo.png")
PRODUCT_OWNER_LOGO_SHA256 = "24b27b23e1007ade0de4bdc0bb6880ba087b3116be2b970f89abf84d023432ae"


def _tokens(selector: str, source: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", source, re.DOTALL)
    assert match is not None
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", match.group("body")))


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(left: str, right: str) -> float:
    brighter, darker = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


def test_web_design_tokens_are_complete_and_wcag_aa_in_both_themes() -> None:
    source = STYLESHEET.read_text()
    defined = set(re.findall(r"--([\w-]+)\s*:", source))
    referenced = set(re.findall(r"var\(--([\w-]+)", source))
    assert referenced <= defined

    light = _tokens(":root", source)
    dark = {**light, **_tokens(':root[data-theme="dark"]', source)}
    for theme in (light, dark):
        for foreground in ("ink", "muted", "accent", "danger", "warning"):
            for background in ("bg", "panel"):
                assert _contrast(theme[foreground], theme[background]) >= 4.5


def test_brand_logo_matches_the_product_owner_asset() -> None:
    content = LOGO.read_bytes()

    assert hashlib.sha256(content).hexdigest() == PRODUCT_OWNER_LOGO_SHA256
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
