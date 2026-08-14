from __future__ import annotations

import hashlib
import re
from pathlib import Path

STYLESHEET = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "styles.css"
LOGO = STYLESHEET.with_name("tradingops-logo.png")
INDEX = STYLESHEET.with_name("index.html")
EXECUTION = STYLESHEET.with_name("execution.js")
APP_CORE = STYLESHEET.with_name("app-core.js")
APP = STYLESHEET.with_name("app.js")
WORKSPACE = STYLESHEET.with_name("workspace.js")
SIGNALS = STYLESHEET.with_name("signals.js")
PROPOSALS = STYLESHEET.with_name("proposals.js")
CAPITAL = STYLESHEET.with_name("capital.js")
SERVICE_WORKER = STYLESHEET.with_name("sw.js")
LICENSES = STYLESHEET.parents[3] / "LICENSES"
FONT_DIR = STYLESHEET.with_name("fonts")
PRODUCT_OWNER_LOGO_SHA256 = "24b27b23e1007ade0de4bdc0bb6880ba087b3116be2b970f89abf84d023432ae"
IBM_PLEX_FONT_SHA256 = {
    "IBMPlexMono-Regular.woff2": (
        "ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350"
    ),
    "IBMPlexMono-SemiBold.woff2": (
        "6a825b4824c01cbb401e829e5a066a1818411bcb3538b5a5792c5ca9b82343c3"
    ),
    "IBMPlexSansSC-Regular.woff2": (
        "dc51399ce38f7200806df891476d7216a81c8aede47284e421a54413608c407c"
    ),
    "IBMPlexSansSC-SemiBold.woff2": (
        "31c2e2375deec6d2ea3d371bec2d3cb2bfe43a51a9e8af986d5b25cd7cb279d3"
    ),
}


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
    source = STYLESHEET.read_text()
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


def test_brand_logo_matches_the_product_owner_asset() -> None:
    content = LOGO.read_bytes()

    assert hashlib.sha256(content).hexdigest() == PRODUCT_OWNER_LOGO_SHA256
    assert content.startswith(b"\x89PNG\r\n\x1a\n")


def test_ibm_plex_fonts_are_vendored_preloaded_and_licensed() -> None:
    styles = STYLESHEET.read_text()
    index = INDEX.read_text()
    service_worker = SERVICE_WORKER.read_text()
    license_text = (LICENSES / "IBM-Plex-OFL-1.1.txt").read_text()
    notice = (LICENSES / "IBM-Plex-NOTICE.md").read_text()

    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
    assert 'Reserved Font Name "Plex"' in license_text
    assert "IBM Plex Sans SC: version 1.1.0" in notice
    assert "IBM Plex Mono: version 2.5.0" in notice
    for filename, expected_hash in IBM_PLEX_FONT_SHA256.items():
        content = (FONT_DIR / filename).read_bytes()
        assert content.startswith(b"wOF2")
        assert hashlib.sha256(content).hexdigest() == expected_hash
        asset_url = f"/assets/fonts/{filename}"
        assert f'url("{asset_url}")' in styles
        assert f'href="{asset_url}" as="font" type="font/woff2" crossorigin' in index
        assert f"'{asset_url}'" in service_worker


def test_font_system_uses_plex_sc_for_ui_and_plex_mono_for_figures() -> None:
    styles = STYLESHEET.read_text()

    assert '--font-sans: "IBM Plex Sans SC"' in styles
    assert '--font-mono: "IBM Plex Mono"' in styles
    assert "unicode-range: U+0030-0039;" in styles
    assert "unicode-range: U+0000-002F, U+003A-10FFFF;" in styles
    assert "font-family: var(--font-mono);" in styles
    assert 'input[type="number"]' in styles
    assert ".technical-id" in styles
    assert "[data-mono]" in styles
    assert "font-variant-numeric: tabular-nums;" in styles
    assert "font-synthesis: none;" in styles
    assert "h1 { font-weight: 600;" in styles
    assert "h2 { font-weight: 600;" in styles
    assert "h3, h4 { font-weight: 600; }" in styles
    assert "label { font-size: 13px; line-height: 1.5; }" in styles
    assert "input, select, textarea { font-size: 14px; line-height: 1.5; }" in styles
    assert "th { font-size: 12px; line-height: 1.5; }" in styles
    assert "font-size: clamp(36px, 3.3vw, 48px);" in styles
    assert "Minimum operational type scale" in styles
    assert ":root .app-shell small," in styles
    assert ":root .app-shell button { font-size: 13px; }" in styles
    assert "font-size: 13px;\n  font-weight: 600;\n  letter-spacing: 0;" in styles
    assert "font-size: 14px;\n  font-weight: 400;" in styles
    assert "fonts.googleapis.com" not in styles
    assert "fonts.gstatic.com" not in styles
    assert "@import url" not in styles
    assert set(re.findall(r"font-weight:\s*(\d+);", styles)) <= {"400", "600"}
    assert re.search(r"font-size:\s*(?<!\d)(?:8|9|10|11)px", styles) is None
    assert re.search(r"font:\s*[^;]*?(?<!\d)(?:8|9|10|11)px", styles) is None


def test_role_navigation_and_task_entries_match_capabilities() -> None:
    index = INDEX.read_text()
    workspace = WORKSPACE.read_text()
    signals = SIGNALS.read_text()

    trade_section = index.split('<p class="nav-section-label">交易流程</p>', maxsplit=1)[1].split(
        '<p class="nav-section-label">运行与风控</p>', maxsplit=1
    )[0]
    operations_section = index.split('<p class="nav-section-label">运行与风控</p>', maxsplit=1)[
        1
    ].split('<p class="nav-section-label">团队配置</p>', maxsplit=1)[0]
    team_section = index.split('<p class="nav-section-label">团队配置</p>', maxsplit=1)[1]
    assert 'href="/proposals"' in trade_section
    assert 'href="/reviews"' in trade_section
    assert 'href="/campaigns"' in trade_section
    assert 'href="/capital"' in operations_section
    assert 'href="/risk"' in operations_section
    assert 'href="/notifications"' in operations_section
    assert 'href="/signals"' in team_section
    assert 'href="/admin/users"' in team_section
    assert 'data-role-preset="OBSERVER"' in workspace
    assert 'data-role-preset="TREASURY_ADMIN"' in workspace
    assert "observerOnly" in workspace
    assert "无需操作" in workspace
    assert "当前身份保持只读" in signals


def test_primary_navigation_preserves_the_established_order() -> None:
    index = INDEX.read_text()
    navigation = index.split('<nav aria-label="主导航">', maxsplit=1)[1].split(
        "</nav>", maxsplit=1
    )[0]
    paths = re.findall(r'<a(?: class="[^"]+")? href="([^"]+)" data-link', navigation)

    assert paths == [
        "/home",
        "/opportunities",
        "/webhook-signals",
        "/proposals",
        "/reviews",
        "/campaigns",
        "/results",
        "/accounts",
        "/capital",
        "/risk",
        "/positions",
        "/notifications",
        "/signals",
        "/admin/users",
        "/team-settings",
    ]
    assert (
        '<a href="/team-settings" data-link data-nav-capability="venue.view">模式设置</a>'
        in index
    )


def test_setup_team_routes_render_the_requested_page_without_repeating_creation_panels() -> None:
    app_core = APP_CORE.read_text()
    execution = EXECUTION.read_text()

    route = app_core.split("async function route()", maxsplit=1)[1]
    assert "teamSetupPaths" not in route
    assert "!session.active_workspace || !session.active_team" in route
    assert "!session.active_team.trading_enabled" not in route
    assert "renderScopeSetup();" in route
    assert "模式设置" in execution
    assert "switchAllowed ? '' : 'disabled'" in execution
    assert "账户凭据" in execution
    assert "不阻止切换团队模式" in execution


def test_navigation_keeps_current_deployment_and_team_mode_visible() -> None:
    index = INDEX.read_text()
    app_core = APP_CORE.read_text()

    assert 'class="sidebar-environment" aria-label="当前环境"' in index
    assert "data-current-environment" in index
    assert "data-current-mode" in index
    assert "authStatus?.environment" in app_core
    assert "session?.active_team?.execution_mode" in app_core
    assert "updateEnvironmentIndicators();" in app_core
    assert "environmentBadge.textContent" in app_core


def test_hierarchy_pass_uses_neutral_surfaces_readable_type_and_one_primary_action() -> None:
    styles = STYLESHEET.read_text()
    index = INDEX.read_text()
    workspace = WORKSPACE.read_text()
    light = _tokens(":root", styles)
    dark = {**light, **_tokens(':root[data-theme="dark"]', styles)}

    assert light["ink"] == "#181818"
    assert light["muted"] == "#666664"
    assert dark["bg"] == "#101010"
    assert dark["panel"] == "#151515"
    assert light["action-bg"] == "#161616"
    assert dark["action-bg"] == "#f2f2ef"
    assert "Iowan Old Style" not in styles
    assert "Palatino Linotype" not in styles
    assert "Georgia" not in styles
    assert "--font-display: var(--font-sans);" in styles
    assert ".sidebar nav a {\n  min-height: 42px;" in styles
    assert "font-size: 14px;\n  font-weight: 400;" in styles
    assert ".eyebrow {\n  margin-bottom: 10px;" in styles
    assert "font-size: 12px;" in styles
    assert ".primary, .secondary, .danger, button {" in styles
    assert '<p class="nav-section-label">工作台</p>' in index
    assert '<p class="nav-section-label">交易流程</p>' in index
    assert '<p class="nav-section-label">运行与风控</p>' in index
    assert '<p class="nav-section-label">团队配置</p>' in index
    assert '<a class="text-link" href="/proposals"' in workspace
    assert '<a class="secondary" href="/opportunities"' in workspace


def test_direct_unauthorized_route_does_not_destroy_the_session() -> None:
    app_core = APP_CORE.read_text()

    unauthorized_handler = app_core.split("function handleUnauthorizedResponse()", maxsplit=1)[
        1
    ].split("function showApiError", maxsplit=1)[0]
    assert "? routeCapability(location.pathname)" in unauthorized_handler
    assert "!hasCapability(requiredCapability)" in unauthorized_handler
    denied_index = unauthorized_handler.index("!hasCapability(requiredCapability)")
    session_reset_index = unauthorized_handler.index("session = null")
    assert denied_index < session_reset_index


def test_workspace_switch_does_not_restore_a_shell_after_session_expiry() -> None:
    workspace = WORKSPACE.read_text()

    select_scope = workspace.split("async function selectScope", maxsplit=1)[1].split(
        "function accessRoleOptions", maxsplit=1
    )[0]
    handled_index = select_scope.index("if (error?.handled || !session) return")
    shell_index = select_scope.index("setShell(true)", select_scope.index("catch"))
    assert handled_index < shell_index


def test_proposal_detail_preserves_the_users_origin() -> None:
    proposals = PROPOSALS.read_text()

    assert (
        "const detailOrigin = status ? 'reviews' : historyMode ? 'history' : 'current'" in proposals
    )
    assert "new URLSearchParams({from:detailOrigin})" in proposals
    assert "params.set('environment', reviewEnvironment)" in proposals
    assert "returnDestination" in proposals
    assert "label:'审核队列'" in proposals
    assert "label:'历史提案'" in proposals


def test_capital_center_is_split_into_task_focused_views() -> None:
    capital = CAPITAL.read_text()
    styles = STYLESHEET.read_text()

    assert "function capitalViewSelection" in capital
    assert "function groupCapitalView" in capital
    assert "资金总览" in capital
    assert "资金路径" in capital
    assert "操作与回执" in capital
    assert "capital-view-panel" in styles


def test_capital_and_risk_pages_expose_environment_scoped_account_workflows() -> None:
    capital = CAPITAL.read_text()
    risk = CAPITAL.with_name("risk.js").read_text()
    execution = EXECUTION.read_text()

    for expected in (
        "当前模式",
        "选择要叠加的当前模式账户曲线（可多选）",  # noqa: RUF001
        "只影响展示",
        "selected_account_keys",
        "account_options",
    ):
        assert expected in capital
    for expected in (
        "管理员直接控制",
        "恢复自动加仓",
        "暂停所有风险",
        "解除风险暂停",
        "async function renderCampaignList()",
        "item.environment === environment",
    ):
        assert expected in execution
    assert "modeAccountEnvironmentLabel" not in capital
    assert "发起风控变更提案" in risk
    assert "POLICY_UPDATE" in risk
    assert "提案 · 独立审核" in risk


def test_trading_mode_mutations_keep_server_side_safety_contracts() -> None:
    execution = EXECUTION.read_text()

    assert "expected_version:data.version" in execution
    assert "I_CONFIRM_LIVE_PRODUCTION_MONEY" in execution
    assert "SWITCH_TO_TESTNET" in execution
    assert "idempotency_key:crypto.randomUUID()" in execution
    assert "expected_version:Number(button.dataset.version)" in execution
    assert "data-account-delete" in execution
    assert "DELETE:" in execution


def test_workflow_environment_comes_only_from_persisted_team_mode() -> None:
    app_core = APP_CORE.read_text()
    start = app_core.index("const currentWorkflowEnvironment")
    end = app_core.index("const roleLabels", start)
    implementation = app_core[start:end]

    assert "active_team?.execution_mode" in implementation
    assert "URLSearchParams" not in implementation
    assert "SETUP" in implementation


def test_institutional_minimal_shell_and_semantic_color_rules() -> None:
    index = INDEX.read_text()
    styles = STYLESHEET.read_text()
    capital = CAPITAL.read_text()

    topbar = index.split('<header class="topbar">', maxsplit=1)[1].split("</header>", maxsplit=1)[0]
    sidebar = index.split('<aside id="sidebar"', maxsplit=1)[1].split("</aside>", maxsplit=1)[0]
    assert 'id="scope-control"' in topbar
    assert 'id="current-date"' in topbar
    assert 'class="header-preferences"' in topbar
    assert topbar.count("data-preference-select=") == 2
    user_menu = index.split('<section id="user-menu-panel"', maxsplit=1)[1].split(
        "</section>", maxsplit=1
    )[0]
    assert "data-preference-select=" not in user_menu
    assert 'id="scope-control"' not in sidebar
    assert "Institutional Minimal — selected product direction" in styles
    assert "--radius-md: 5px" in styles
    light = _tokens(":root", styles)
    dark = {**light, **_tokens(':root[data-theme="dark"]', styles)}
    assert light["action-bg"] == "#161616"
    assert light["action-fg"] == "#ffffff"
    assert dark["action-bg"] == "#f2f2ef"
    assert dark["action-fg"] == "#151515"
    assert light["accent"] == "#202020"
    assert dark["accent"] == "#f2f2ef"
    assert light["success"] != light["accent"]
    assert dark["success"] != dark["accent"]
    assert light["chart-total"] != light["success"]
    assert dark["chart-total"] != dark["success"]
    primary_rule = (
        ".primary { background: var(--action-bg); border-color: var(--action-bg); "
        "color: var(--action-fg); }"
    )
    assert primary_rule in styles
    assert ".secondary { background: transparent; }" in styles
    assert "--testnet-accent: var(--accent)" in styles
    assert "linear-gradient" not in styles
    assert "radial-gradient" not in styles
    assert "drop-testnet" not in styles
    assert ".account-list-hero::after" not in styles
    assert "--market-up: #2f6d4f" in styles
    assert "--market-down: #a04440" in styles
    assert ".direction-long { color: var(--market-up); }" in styles
    assert ".direction-short { color: var(--market-down); }" in styles
    assert "--chart-source-1" in styles
    assert "['--chart-source-1','--chart-source-2','--chart-source-3','--chart-total']" in capital


def test_navigation_hierarchy_uses_neutral_states_and_accessible_current_page() -> None:
    index = INDEX.read_text()
    styles = STYLESHEET.read_text()
    app = APP.read_text()

    assert '<p class="nav-link-group-label">实时信号</p>' in index
    assert '<span aria-hidden="true">⌁</span>' not in index
    assert "<span>⌂</span>" not in index
    assert "--sidebar-bg: #191919;" in styles
    assert "--nav-active-bg: #f5f1e8;" in styles
    assert ".nav-section + .nav-section {" in styles
    assert "border-top: 1px solid var(--nav-divider);" in styles
    assert ".nav-link-group::before {" in styles
    assert 'a[aria-current="page"]' in styles
    assert "cursor: pointer;" in styles
    assert "link.setAttribute('aria-current', 'page')" in app
    assert "else link.removeAttribute('aria-current')" in app


def test_header_preferences_are_not_closed_by_the_user_menu_outside_click() -> None:
    app = APP.read_text()

    close_user_menu = app.split("function closeUserMenu", maxsplit=1)[1].split(
        "identityChip.addEventListener", maxsplit=1
    )[0]
    identity_menu_handler = app.split("identityChip.addEventListener", maxsplit=1)[1].split(
        "function preferenceElements", maxsplit=1
    )[0]
    assert "closePreferenceDropdowns" not in close_user_menu
    assert "closePreferenceDropdowns();" in identity_menu_handler


def test_workspace_gate_header_reclaims_the_hidden_scope_column() -> None:
    styles = STYLESHEET.read_text()

    assert ".topbar > .topbar-actions { grid-column: 3; }" in styles
    assert ".topbar:has(> #scope-control[hidden]) {" in styles
    assert "grid-template-columns: 220px minmax(0, 1fr);" in styles
    assert ".topbar:has(> #scope-control[hidden]) > .topbar-actions { grid-column: 2; }" in styles
