**Source and state**

- Source visual truth: `/var/folders/c1/6j8smjg96430htljxp_sx8sr0000gn/T/codex-clipboard-0dca9201-6c29-4c36-bbf8-7b9261b5fdd7.png`
- Source pixels: 3024 × 1722.
- Intended implementation viewport: desktop, 1512 × 861 CSS pixels at device scale factor 2.
- Intended state: authenticated SHADOW opportunities page; capital page is an extension of the same design system.
- Implementation screenshot: unavailable.
- Density normalization: source is treated as a 2× capture of the intended CSS viewport.

**Findings**

- [P2] Browser-rendered comparison is unavailable.
  Location: opportunities, proposal dialog, and capital center.
  Evidence: the in-app browser could not attach because the active Codex desktop window was displaying another task; no implementation screenshot could be captured from this task.
  Impact: typography, spacing, responsive wrapping, and chart rendering could not be judged against the supplied screenshot.
  Fix: focus this task in the Codex desktop window, open the local application, capture the three relevant states at 1512 × 861 CSS pixels, and run a side-by-side comparison.

**Required fidelity surfaces**

- Fonts and typography: code uses the existing Inter/system UI stack and Georgia display hierarchy; browser comparison is blocked.
- Spacing and layout rhythm: additions reuse the existing panel, grid, radius, and spacing tokens; browser comparison is blocked.
- Colors and visual tokens: additions only use existing theme variables; browser comparison is blocked.
- Image quality and asset fidelity: no new raster or decorative assets were introduced; the data chart is rendered from live values.
- Copy and content: filters, default proposal explanation, dual links, and capital snapshot disclosure are present in the implementation.

**Full-view and focused evidence**

- Full-view comparison: blocked because there is no browser-rendered implementation screenshot.
- Focused comparison: blocked for the same reason.
- Primary interactions covered by automated/code verification: filter event wiring, default proposal payload construction, advanced-dialog submission, external URL construction, and capital chart rendering path.
- Console errors checked: JavaScript syntax check passed; browser console inspection unavailable.

**Comparison history**

- Initial pass: blocked by unavailable implementation capture.
- No visual fixes were made from a comparison because a valid same-state comparison could not be produced.

**Implementation checklist**

- Capture opportunities default state and an active filter state.
- Capture the advanced proposal dialog.
- Capture capital center with at least two valid balance locations.
- Verify desktop and mobile wrapping, keyboard focus, and browser console.

**Follow-up polish**

- Consider adding true historical net-worth snapshots later; the current chart is deliberately labeled as a current capital-composition snapshot.

final result: blocked
