from __future__ import annotations

import re
from pathlib import Path

WEB_ROOT = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web"
STYLESHEETS = tuple(sorted(WEB_ROOT.glob("styles-*.css")))


def _tokens(selector: str, source: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", source, re.DOTALL)
    assert match is not None
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", match.group("body")))


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(left: str, right: str) -> float:
    brighter, darker = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


def test_web_design_tokens_are_complete_and_wcag_aa_in_both_themes() -> None:
    source = "\n".join(path.read_text() for path in STYLESHEETS)
    defined = set(re.findall(r"--([\w-]+)\s*:", source))
    referenced = set(re.findall(r"var\(--([\w-]+)", source))
    assert referenced <= defined

    light = _tokens(":root", source)
    dark = {**light, **_tokens(':root[data-theme="dark"]', source)}
    for theme in (light, dark):
        for foreground in ("ink", "muted", "accent", "success", "danger", "warning"):
            for background in ("bg", "panel"):
                assert _contrast(theme[foreground], theme[background]) >= 4.5
        assert _contrast(theme["action-fg"], theme["action-bg"]) >= 4.5


def test_compact_density_contract_covers_shell_content_and_mobile() -> None:
    base = (WEB_ROOT / "styles-base.css").read_text()
    components = (WEB_ROOT / "styles-components.css").read_text()
    shell = (WEB_ROOT / "styles-shell.css").read_text()
    api_access = (WEB_ROOT / "styles-api-access.css").read_text()
    index = (WEB_ROOT / "index.html").read_text()

    for token in (
        "--control-height: 36px",
        "--page-gutter: 20px",
        "--page-top: 18px",
        "--section-gap: 20px",
        "--card-padding: 16px",
        "--font-size-body: 14px",
        "--line-height-body: 1.5",
    ):
        assert token in base

    for rule in (
        ".app-shell { grid-template: 60px 1fr / 248px minmax(0, 1fr); }",
        ".main-content { padding: var(--page-top) var(--page-gutter) 48px; }",
        ".page, .api-access-page { width: 100%; max-width: none; margin: 0; }",
        "th, td { padding: 8px 10px; line-height: var(--line-height-meta); }",
        ".opportunity-card { min-height: 0; padding: var(--dense-card-padding); }",
        ".opportunity-card .link-row { margin: 0 0 var(--space-2); }",
        "--page-gutter: 10px",
        "--page-top: 12px",
        ".topbar { height: 56px; min-height: 56px; }",
    ):
        assert rule in components

    assert ".topbar { grid-template-columns: 248px" in shell
    assert ".sidebar { top: 56px; height: calc(100dvh - 56px); }" in shell
    assert ".api-access-page { max-width: none; }" in api_access
    for href in (
        "/assets/styles-base.css?v=22",
        "/assets/styles-components.css?v=24",
        "/assets/styles-api-access.css?v=4",
        "/assets/styles-shell.css?v=14",
    ):
        assert href in index
