# TradingOPS shell design-system acceptance

## Scope

- Actual local Docker Compose console at `http://127.0.0.1:8022/`.
- Authenticated `Default Workspace / Default Team` administrator session.
- Browser viewport: 1422 x 800 CSS pixels.
- No proposal review, order, transfer, signing, or broadcast action was submitted.

## Accepted evidence

- `01-home-dark.jpg`: dark theme, grouped navigation, persistent scope, local-environment label, visible theme mode, and one primary action.
- `02-home-light.jpg`: the same facts, hierarchy, navigation, and actions in the light theme.

## Checks

- Theme selection persisted after a full page reload.
- All 13 authenticated routes rendered with the same current scope and three navigation groups.
- All 13 routes reported no document-level horizontal overflow at the captured viewport.
- Browser console contained no warnings or errors during the route sweep.
- Light and dark foreground, muted, accent, warning, and danger tokens meet WCAG AA 4.5:1 against both page and panel backgrounds.
- CSS has no unresolved custom-property references.

## Remaining acceptance

- Exact 1024, 430, and 390 CSS-pixel browser captures.
- Keyboard-only traversal, focus order, screen-reader announcements, and automated accessibility checks across the four core flows.
