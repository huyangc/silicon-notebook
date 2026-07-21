# Source Detail Window Interactions

## Context

The source-detail dialog currently uses `PanelRightClose`, an icon whose sidebar-collapse meaning is unclear in a modal. The dialog is also fixed at the center of the overlay, so a desktop user cannot move it out of the way while referring to the notebook workspace behind it.

## Goals

- Render a conventional close icon with a Chinese accessible name.
- Let desktop users drag the source-detail dialog by its header.
- Preserve source content scrolling and every existing source action.
- Keep the dialog recoverable inside the viewport and retain its position for the current browser session.

## Non-goals

- Do not add resize controls.
- Do not make every utility modal draggable.
- Do not change source-detail data loading, parsing, deletion, or rendering.
- Do not enable floating-window geometry on narrow screens.

## Design

The source-detail dialog will reuse `useFloatingWindow` with a source-specific storage key and `resizable: false`. Its `cardRef` and computed `style` will be attached to the dialog card, while `dragHandleProps` will be attached only to the existing source-detail header.

The shared hook already ignores pointer starts on interactive descendants, clamps movement so the header remains recoverable, remembers geometry in `sessionStorage`, and disables floating geometry at widths of 720 px or less. Reusing it keeps this dialog consistent with the existing Knowhow floating windows without adding another pointer-event implementation.

The header button will replace `PanelRightClose` with Lucide's `X`. It will use `title="关闭"` and `aria-label="关闭来源详情"`; clicking it continues to clear `sourceDetail`. Dragging the header does not close the dialog, and dragging never begins from the close button.

The existing `.source-detail-body` scrolling behavior remains unchanged. No resize handle is rendered.

## Verification

- Add a source-level regression test that verifies the source-detail dialog is wired to `useFloatingWindow` with resizing disabled, mounts the returned card ref/style, and mounts drag props on the header.
- Verify the close control uses `X` and Chinese close semantics rather than `PanelRightClose`.
- Run the focused frontend test, frontend type checking, the production frontend build, and `scripts/check.sh`.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together because the repository contract requires documentation parity for product-behavior changes.
