# Design brief: dashboard visual refresh (editorial-minimal)

Status: BRIEF (not yet built)
Scope: the "Hours & Feelings" web dashboard only (`klok/dashboard.html`, `docs/dashboard/hours-and-feelings.html`). The CLI stays terminal; this does not touch command behavior.

## Reference
User reference: `https://aicommon.creamu.co.jp/` — luxury/editorial Japanese landing page. Captured design language (from the live page, not guessed):

- **Ground:** warm near-black `#0a0a09` (not pure black, slightly warm).
- **Text:** warm cream `#e9e5da` (off-white with a warm cast).
- **Accent:** a single muted bronze/taupe `#756044`. One accent only.
- **Type:** light-weight Helvetica Neue (weight 300), Noto Sans JP for Japanese. Oversized thin headings (hero ~100px, weight 300, letter-spacing ~-1px); tiny letter-spaced uppercase meta labels ("TOKYO, 2026").
- **Layout:** generous negative space, long scroll, minimal chrome, content floats on the dark ground, one accent used sparingly.
- **Feel:** calm, premium, editorial. Restraint over decoration.

## Mapping to the dashboard
Current dashboard is slate-indigo and fairly dense. Move it toward:
- Tokens: `--bg: #0a0a09; --fg: #e9e5da; --muted: ~#8a857a; --accent: #756044; --panel: ~#141311` (warm dark surfaces, not cool slate).
- Type: large thin section headings, lots of air, tiny tracked labels for metric captions (ENERGY, DEEP WORK, STREAK). System-font stack with a thin-weight sans; no webfont dependency unless embedded (offline-first constraint: the page is a single offline HTML file, so any font must be system or embedded, never fetched).
- Charts/viz: redraw the energy-time map, mood calendar, trend lines and the new digest findings in the warm-dark + single-bronze palette (encode intensity by lightness of the cream, accent only for highlights).
- Keep it one accent. Resist adding colors.

## Constraints
- Offline-first: single self-contained HTML file, no network fetches, no new runtime deps. Fonts must be system-stack or embedded, never a Google Fonts link.
- Accessibility: cream-on-warm-black must hold contrast; the bronze accent is decorative, never the only signal.

## How to build it (sequencing + tooling)
- Sequence AFTER the energy-aware features land, so the refresh can also surface the new `klok digest` findings in the same pass (this is the design doc's "B phase: dashboard shows the findings").
- Use the installed `ui-ux-pro-max` skill (available after the next Claude Code restart) for palette/type/layout intelligence, and run gstack `/design-consultation` or `/plan-design-review` on the mockup before implementing.
- Deliver as its own slice/PR separate from the functional energy-aware work.

## Open question for the user
- Confirm the dashboard is the intended target (vs. some other surface), and whether to prioritize this before or after the `review`/`digest` features ship.
