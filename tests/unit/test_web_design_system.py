from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

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


def _css_blocks(source: str) -> list[tuple[str, str]]:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    blocks: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(source):
        opening = source.find("{", cursor)
        if opening < 0:
            break
        prelude = source[cursor:opening].strip()
        depth = 1
        closing = opening + 1
        while closing < len(source) and depth:
            depth += (source[closing] == "{") - (source[closing] == "}")
            closing += 1
        assert depth == 0
        blocks.append((prelude, source[opening + 1 : closing - 1]))
        cursor = closing
    return blocks


def _media_applies(prelude: str, viewport_width: int) -> bool:
    max_widths = [int(value) for value in re.findall(r"max-width:\s*(\d+)px", prelude)]
    min_widths = [int(value) for value in re.findall(r"min-width:\s*(\d+)px", prelude)]
    return all(viewport_width <= value for value in max_widths) and all(
        viewport_width >= value for value in min_widths
    )


def _selector_parts(selector_list: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(selector_list):
        depth += character == "("
        depth -= character == ")"
        if character == "," and depth == 0:
            parts.append(selector_list[start:index].strip())
            start = index + 1
    parts.append(selector_list[start:].strip())
    return [part for part in parts if part]


def _specificity(selector: str) -> tuple[int, int, int]:
    selector = re.sub(r":where\([^)]*\)", "", selector)
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^]]+\]|:(?!:)[\w-]+", selector))
    stripped = re.sub(r"#[\w-]+|\.[\w-]+|\[[^]]+\]|::?[\w-]+(?:\([^)]*\))?", " ", selector)
    elements = len(re.findall(r"(?<![-\w])[A-Za-z][\w-]*", stripped))
    return ids, classes, elements


def _box_value(value: str, side: str) -> str | None:
    values = re.findall(r"(?:[\w-]+\([^)]*\)|[^\s]+)", value)
    if not 1 <= len(values) <= 4:
        return None
    if len(values) == 1:
        top = right = bottom = left = values[0]
    elif len(values) == 2:
        top = bottom = values[0]
        right = left = values[1]
    elif len(values) == 3:
        top, right, bottom = values
        left = right
    else:
        top, right, bottom, left = values
    return {"top": top, "right": right, "bottom": bottom, "left": left}[side]


@lru_cache(maxsize=4)
def _active_declarations(viewport_width: int) -> tuple[tuple[str, str, str, bool, int], ...]:
    index = (WEB_ROOT / "index.html").read_text()
    stylesheet_names = re.findall(r'href="/assets/(styles-[^?\"]+)', index)
    declarations: list[tuple[str, str, str, bool, int]] = []
    order = 0

    def visit(source: str, active: bool = True) -> None:
        nonlocal order
        for prelude, body in _css_blocks(source):
            if prelude.startswith("@media"):
                visit(body, active and _media_applies(prelude, viewport_width))
                continue
            if prelude.startswith("@") or not active:
                continue
            for match in re.finditer(r"([\w-]+)\s*:\s*([^;{}]+);", body):
                name, value = match.groups()
                important = value.rstrip().endswith("!important")
                value = re.sub(r"\s*!important\s*$", "", value).strip()
                declarations.append((prelude, name, value, important, order))
                order += 1

    for name in stylesheet_names:
        visit((WEB_ROOT / name).read_text())
    return tuple(declarations)


def _effective_css_value(html: str, target: str, property_name: str, width: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one(target)
    assert element is not None
    winner: tuple[bool, tuple[int, int, int], int, str] | None = None
    box_prefix, _, box_side = property_name.partition("-")
    for selector_list, name, value, important, order in _active_declarations(width):
        candidate = value if name == property_name else None
        if candidate is None and name == box_prefix and box_prefix in {"margin", "padding"}:
            candidate = _box_value(value, box_side)
        if candidate is None:
            continue
        for selector in _selector_parts(selector_list):
            try:
                matches = any(node is element for node in soup.select(selector))
            except (NotImplementedError, SelectorSyntaxError):
                continue
            if not matches:
                continue
            resolved = (important, _specificity(selector), order, candidate)
            if winner is None or resolved[:3] >= winner[:3]:
                winner = resolved
    assert winner is not None, f"no {property_name} declaration matched {target}"
    return winner[3]


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
        ".app-shell { grid-template: 60px 1fr / 216px minmax(0, 1fr); }",
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

    assert ".topbar { grid-template-columns: 216px" in shell
    assert ".sidebar { top: 56px; height: calc(100dvh - 56px); }" in shell
    assert ".api-access-page { max-width: none; }" in api_access
    for href in (
        "/assets/styles-base.css?v=22",
        "/assets/styles-components.css?v=31",
        "/assets/styles-api-access.css?v=4",
        "/assets/styles-shell.css?v=15",
    ):
        assert href in index


def test_spacing_regression_contract_uses_the_final_stylesheet_cascade() -> None:
    root = '<html id="target"><body></body></html>'
    performance = """
        <html><body>
          <section class="performance-capital-panel">
            <div class="capital-overview"></div>
            <div id="target-panel" class="capital-chart-panel">
              <canvas id="capital-chart"></canvas>
            </div>
          </section>
        </body></html>
    """
    expanded_performance = performance.replace(
        'class="capital-chart-panel"', 'class="capital-chart-panel is-expanded"'
    )
    home = """
        <html><body><div class="home-layout"><section></section><aside id="target"></aside></div>
        </body></html>
    """
    home_actions = """
        <html><body><article class="home-quick-start">
          <div id="quick-actions" class="stacked-actions home-quick-actions">
            <a class="secondary">Perptape</a><a class="secondary">Webhook</a>
            <a id="manual-action" class="text-link">Manual</a>
          </div>
        </article></body></html>
    """
    account_grids = """
        <html><body>
          <div id="account-list" class="mode-account-grid"><article></article></div>
          <div id="account-detail" class="account-detail-config-grid"><article></article></div>
        </body></html>
    """
    venue_record_tools = """
        <html><body><div id="record-tools" class="proposal-list-tools venue-record-tools">
          <label>Search<input></label><label>Status<select></select></label><span>7 / 7</span>
        </div></body></html>
    """
    system = """
        <html><body>
          <section class="page system-status-page">
            <section><div class="table-wrap"><table></table></div></section>
            <details id="target" class="card create-member-panel system-monitoring-disclosure">
              <article class="system-health-card"><p id="health-copy">Short status.</p></article>
            </details>
          </section>
        </body></html>
    """
    ordinary_empty = '<html><body><section id="target" class="empty-state"></section></body></html>'
    compact_empty = ordinary_empty.replace("empty-state", "empty-state compact-empty")
    error_state = ordinary_empty.replace("empty-state", "error-state")
    api_empty = ordinary_empty.replace("empty-state", "empty-state api-client-empty")
    shell = """
        <html><body>
          <div class="app-shell" id="shell">
            <header class="topbar" id="topbar"><a class="brand"></a></header>
            <aside class="sidebar" id="sidebar"></aside>
            <button class="desktop-nav-toggle" id="toggle"></button>
          </div>
        </body></html>
    """
    campaign = """
        <html><body><div class="campaign-list-table"><table><tbody>
          <tr id="campaign-row">
            <td id="instrument" class="campaign-instrument-cell">
              <div class="campaign-instrument"><b>BTC</b>
                <a id="campaign-link" class="row-link campaign-id-link">abc123…</a>
              </div>
            </td>
            <td id="direction" class="campaign-direction-cell">
              <span class="direction-pill">Long</span>
            </td>
          </tr>
        </tbody></table></div></body></html>
    """
    proposal_compose = """
        <html><body><form class="proposal-compose">
        <div id="intent-grid" class="field-grid proposal-intent-grid">
          <label id="account-field" class="proposal-account-field">Account
            <span id="account-help" class="field-help">Authorized exact accounts only</span>
            <select><option>acct-1</option></select>
          </label>
          <label id="venue-field" class="proposal-venue-field">Exchange
            <select><option>Binance</option></select>
          </label>
          <label id="symbol-field" class="proposal-symbol-field">Symbol<input></label>
          <label id="direction-field" class="proposal-direction-field">
            Direction<select></select>
          </label>
          <label id="trigger-field" class="proposal-trigger-field">Trigger<input></label>
        </div></form></body></html>
    """
    capital_tag = """
        <html><body><button class="capital-account-tag">
          <span>acct-1</span><span id="remove-copy" class="capital-account-remove">Remove</span>
        </button></body></html>
    """

    assert _effective_css_value(root, "#target", "--section-gap", 1440) == "20px"
    assert _effective_css_value(root, "#target", "--section-gap", 390) == "16px"
    assert _effective_css_value(root, "#target", "--space-5", 1440) == "20px"

    assert _effective_css_value(performance, "#capital-chart", "height", 1440) == (
        "clamp(380px, 28vw, 400px)"
    )
    assert _effective_css_value(performance, "#capital-chart", "height", 1024) == (
        "clamp(380px, 28vw, 400px)"
    )
    assert _effective_css_value(performance, "#capital-chart", "height", 390) == "300px"
    assert _effective_css_value(expanded_performance, "#capital-chart", "height", 1440) == (
        "100%"
    )
    assert _effective_css_value(expanded_performance, "#capital-chart", "height", 390) == "100%"
    assert _effective_css_value(performance, "#target-panel", "margin-bottom", 1440) == "0"
    assert _effective_css_value(
        performance, ".performance-capital-panel", "margin-bottom", 1440
    ) == "var(--section-gap)"

    for width in (1440, 390):
        assert _effective_css_value(home, "#target", "margin-top", width) == "var(--space-5)"
        assert _effective_css_value(home, "#target", "padding-top", width) == "0"
    for width in (1440, 1024):
        assert _effective_css_value(home_actions, "#quick-actions", "display", width) == "flex"
        assert _effective_css_value(home_actions, "#quick-actions", "gap", width) == (
            "var(--space-1)"
        )
        assert _effective_css_value(home_actions, ".secondary", "padding-left", width) == (
            "var(--space-2)"
        )
    assert _effective_css_value(home_actions, "#quick-actions", "display", 390) == "grid"
    assert _effective_css_value(home_actions, "#quick-actions", "grid-template-columns", 390) == (
        "1fr"
    )
    for width in (1440, 1024, 390):
        assert _effective_css_value(account_grids, "#account-list", "align-items", width) == (
            "start"
        )
        assert _effective_css_value(account_grids, "#account-detail", "align-items", width) == (
            "start"
        )
    for width in (1440, 1024):
        assert _effective_css_value(
            venue_record_tools, "#record-tools", "grid-template-columns", width
        ) == "minmax(220px, 1fr) minmax(140px, 190px) max-content"
        assert _effective_css_value(
            venue_record_tools, "#record-tools > span", "grid-column", width
        ) == "auto"
    assert _effective_css_value(system, "#target", "margin-bottom", 1440) == (
        "var(--section-gap)"
    )
    assert _effective_css_value(system, "#target", "margin-bottom", 390) == (
        "var(--section-gap)"
    )
    for width in (1440, 1024, 390):
        assert _effective_css_value(system, "#target", "margin-top", width) == (
            "var(--space-3)"
        )
        assert _effective_css_value(system, "#target", "padding-top", width) == "0"
        assert _effective_css_value(system, "#target", "padding-bottom", width) == "0"
    assert _effective_css_value(system, "#health-copy", "min-height", 1440) == "38px"

    assert _effective_css_value(ordinary_empty, "#target", "min-height", 1440) == "160px"
    assert _effective_css_value(ordinary_empty, "#target", "min-height", 390) == "150px"
    assert _effective_css_value(compact_empty, "#target", "min-height", 1440) == "120px"
    assert _effective_css_value(compact_empty, "#target", "min-height", 390) == "120px"
    assert _effective_css_value(api_empty, "#target", "min-height", 1440) == "120px"
    assert _effective_css_value(api_empty, "#target", "min-height", 390) == "120px"
    assert _effective_css_value(error_state, "#target", "min-height", 1440) == "240px"
    assert _effective_css_value(error_state, "#target", "min-height", 390) == "190px"

    assert _effective_css_value(shell, "#shell", "grid-template", 1440) == (
        "60px 1fr / 216px minmax(0, 1fr)"
    )
    assert _effective_css_value(shell, "#topbar", "grid-template-columns", 1440) == (
        "216px minmax(190px, 250px) minmax(0, 1fr)"
    )
    assert _effective_css_value(shell, "#toggle", "left", 1440) == "198px"
    assert _effective_css_value(shell, "#shell", "display", 1024) == "block"
    assert _effective_css_value(shell, "#sidebar", "width", 1024) == (
        "min(360px, calc(100% - 54px))"
    )
    assert _effective_css_value(shell, "#sidebar", "width", 390) == (
        "min(340px, calc(100% - 34px))"
    )
    assert _effective_css_value(campaign, "#campaign-row", "grid-template-columns", 390) == (
        "repeat(2, minmax(0, 1fr))"
    )
    assert _effective_css_value(campaign, ".campaign-instrument", "display", 1440) == "flex"
    assert _effective_css_value(campaign, ".campaign-instrument", "white-space", 1440) == (
        "nowrap"
    )
    assert _effective_css_value(campaign, ".campaign-instrument", "display", 390) == "grid"
    assert _effective_css_value(campaign, ".campaign-instrument", "white-space", 390) == (
        "normal"
    )
    assert _effective_css_value(campaign, "#instrument", "grid-column", 390) == "auto"
    assert _effective_css_value(campaign, "#direction", "justify-self", 390) == "end"
    assert _effective_css_value(campaign, "#campaign-link", "min-height", 390) == "40px"
    for width in (1440, 1024, 390):
        assert _effective_css_value(
            proposal_compose, "#account-field", "align-content", width
        ) == "start"
        assert _effective_css_value(capital_tag, "#remove-copy", "white-space", width) == (
            "nowrap"
        )
    for width in (1440, 1024):
        assert _effective_css_value(
            proposal_compose, "#intent-grid", "grid-template-columns", width
        ) == "repeat(12, minmax(0, 1fr))"
        for selector, expected in (
            ("#account-field", "span 5"),
            ("#venue-field", "span 3"),
            ("#symbol-field", "span 4"),
            ("#direction-field", "span 6"),
            ("#trigger-field", "span 6"),
        ):
            assert _effective_css_value(
                proposal_compose, selector, "grid-column", width
            ) == expected
        assert _effective_css_value(
            proposal_compose, "#account-help", "min-height", width
        ) == "36px"
    assert _effective_css_value(
        proposal_compose, "#intent-grid", "grid-template-columns", 390
    ) == "1fr"
    assert _effective_css_value(proposal_compose, "#account-help", "min-height", 390) == "14px"
