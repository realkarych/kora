# Console tokens

Status: design detail behind [docs/console.md](../../../docs/console.md), written 2026-10-17. Every value here is a CSS custom property of the console under the name given; the stylesheet defines nothing by hand that this file does not name. The contrast ratios were computed on 2026-10-17 with the WCAG 2.2 relative-luminance formula; the thresholds are 4.5:1 for text (1.4.3) and 3:1 for non-text contrast (1.4.11).

## Typefaces

| role | family | property |
|---|---|---|
| the console's own text: facts, labels, controls, tables | IBM Plex Sans | `--font-sans` |
| everything a model wrote: the bundle's narrative, agent messages in logs, hypothesis texts, review comments | IBM Plex Serif | `--font-serif` |
| identifiers: commit hashes, node paths, run ids, branch names, commands, tool arguments in logs | IBM Plex Mono | `--font-mono` |

IBM Plex is published under the SIL Open Font License; the three families were designed together and share their metrics, so a serif block inside a sans page keeps the baseline grid. Numerals in the sans are set tabular (`font-variant-numeric: tabular-nums`) wherever tokens, USD amounts and durations align in a column.

## Type scale

| property | size / line height (px) | used for |
|---|---|---|
| `--text-xs` | 12 / 16 | the node's second line, the freshness word, provenance words, the strip's counts |
| `--text-sm` | 13 / 18 | table cells, the runs list, the node panel, the timeline |
| `--text-base` | 14 / 20 | body text, forms, the review page's facts |
| `--text-md` | 16 / 24 | the agent's narrative in serif, agent messages in logs |
| `--text-lg` | 20 / 28 | page headers, the graph header |
| `--text-xl` | 24 / 32 | the spend figure on the Cost page |

Nothing is set below 12 px; everything at 12 and 13 px is set in the text colour at full contrast, never muted.

## Spacing

The base is 4 px: `--space-1` 4, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-6` 24, `--space-8` 32, `--space-12` 48. Rows in lists are 40 px; the collapsed runs column's rows are 48 px; the top bar is 48 px; the regions of the graph page are in [pages.md](pages.md).

## Colours

Each state colour is a pair: a tint for the fill of a node, a segment or a badge, and a full colour for its 2 px border and its glyph. The fill differs from the canvas by only about 1.2:1 on purpose: the border and the glyph carry the 3:1 non-text contrast, the fill groups. Text on a fill is the theme's text colour. Secondary text on a coloured surface is tinted from that hue, never gray.

Light theme: `--canvas` `#FFFFFF`, `--text` `#1A1D21` (16.9:1 on the canvas).

| state | `--fill-*` | `--line-*` (border and glyph) | text on fill | line on canvas | line on fill |
|---|---|---|---|---|---|
| gray | `#ECEEF1` | `#6B7280` | 14.6:1 | 4.8:1 | 4.2:1 |
| blue | `#DCE9FB` | `#1D4ED8` | 13.8:1 | 6.7:1 | 5.5:1 |
| amber | `#FCEBC2` | `#B45309` | 14.3:1 | 5.0:1 | 4.3:1 |
| green | `#D9F2E2` | `#15803D` | 14.3:1 | 5.0:1 | 4.2:1 |
| red | `#FDE2E1` | `#B3261E` | 13.8:1 | 6.5:1 | 5.3:1 |

Dark theme: `--canvas` `#0F1115`, `--text` `#E7E9EE` (15.6:1 on the canvas).

| state | `--fill-*` | `--line-*` (border and glyph) | text on fill | line on canvas | line on fill |
|---|---|---|---|---|---|
| gray | `#23272E` | `#9CA3AF` | 12.3:1 | 7.4:1 | 5.9:1 |
| blue | `#142447` | `#60A5FA` | 12.6:1 | 7.4:1 | 6.0:1 |
| amber | `#3A2A0C` | `#FBBF24` | 11.4:1 | 11.3:1 | 8.3:1 |
| green | `#11301F` | `#4ADE80` | 11.8:1 | 10.8:1 | 8.2:1 |
| red | `#3B1618` | `#F87171` | 13.2:1 | 6.8:1 | 5.8:1 |

The theme follows the system setting and the switch in the user menu; both tables are checked again whenever a value changes.

## Node geometry and zoom

A node is 200 × 64 px at zoom 1, corner radius 6 px, border 2 px in `--line-*`, fill `--fill-*`; the kind icon is 20 px in the top-left corner, the state glyph 12 px in the top-right; the name is set at `--text-sm`, the second line at `--text-xs`; the strip is 8 px tall along the bottom edge, and a segment at least 12 px wide carries the glyph. Edges are 1.5 px in `--line-gray`. Below zoom 0.5 a node shows only the icon, the glyph and the strip; from 0.5 it shows the two lines. The zoom range is 0.1 to 2; the canvas fits the graph on first paint. The focus ring is 2 px in `--text` offset 2 px on every control, visible on every fill in both themes. Selection must remain distinguishable from keyboard focus, including when both apply. The earlier identical selection/focus outlines do not satisfy that requirement; the selection treatment remains an [open design question](../../architecture/detailed.md#open-implementation-questions).

## Motion

`--duration-state` 150 ms ease-out for a colour change after the first paint; `--duration-layout` 200 ms ease-out for the moves of a layout re-run on expand or collapse; nothing else animates, and `prefers-reduced-motion` sets both to 0.
