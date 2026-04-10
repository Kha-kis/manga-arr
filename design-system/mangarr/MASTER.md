# Mangarr — Ink & Ember Editorial

**Generated:** 2026-04-10 via ui-ux-pro-max skill
**Category:** Self-hosted manga library manager (book/reading tracker + media tool)
**Design system:** Swiss Modernism 2.0 × Dark Mode OLED, with editorial typography triple-stack
**Stack:** FastAPI + Jinja2 + HTMX + Alpine.js + Bootstrap 5 + Tailwind CDN

> **LOGIC:** When building a specific page, first check `design-system/mangarr/pages/[page-name].md`.
> If that file exists, its rules override this Master file.
> If not, strictly follow the rules below.

---

## 1. Design Philosophy

Mangarr is a **literary tool** for someone who treats manga as books, not as DVDs. The redesign moves away from "self-hosted homebrew admin panel" aesthetics toward the visual language of a serious book-tracking app (Letterboxd, The Criterion Channel, a well-made magazine). Three principles:

1. **Editorial restraint.** Typography and grid do the work. Ornament is rare and intentional. Ember accent is used sparingly — it should feel like a signature, not a theme.
2. **Information has dignity.** Numbers get room to breathe. Stats are oversized tabular figures. Data is never cramped.
3. **The manga covers are the stars.** The design should frame them, not compete with them.

The brand continuity from the old "Ink & Ember" identity is preserved: dark background, ember accent, `Instrument Serif` italic for the wordmark. What changes: a proper editorial typography system, mathematical grid discipline, and a darker, more refined color scale.

---

## 2. Color Palette (Dark-first)

Based on the skill's Financial Dashboard + Podcast Platform dark palettes, adapted for manga reading and preserving the ember signature.

### Surfaces

| Token | Hex | Use |
|-------|-----|-----|
| `--void` | `#050510` | Page background (darker than before for OLED efficiency) |
| `--ink` | `#0b0b15` | Sidebar background, topbar background |
| `--ink-2` | `#12121f` | Card / panel background |
| `--ink-3` | `#1a1a28` | Hover states, nested panels, table row hover |
| `--ink-4` | `#242434` | Input backgrounds, raised elements |
| `--ink-5` | `#2e2e42` | Scrollbar thumb, subtle dividers |

### Borders

| Token | Hex | Use |
|-------|-----|-----|
| `--edge` | `#1f1f32` | Default border (panels, cards) |
| `--edge-2` | `#2a2a40` | Button borders, inputs |
| `--edge-3` | `#35354f` | Emphasized / hover borders |

### Text

| Token | Hex | Contrast on --void | Use |
|-------|-----|---|---|
| `--text` | `#f5f0e8` | 16.8:1 AAA | Primary text, display headings |
| `--text-2` | `#a8a89a` | 7.5:1 AAA | Secondary text, labels |
| `--text-3` | `#6a6a5a` | 3.8:1 AA-large | Tertiary (captions, helper text on 16px+) |
| `--text-4` | `#44445a` | — | Decorative only (dividers, placeholders) |

### Signature accent (Ember — kept from legacy)

| Token | Hex | Use |
|-------|-----|-----|
| `--ember` | `#f08428` | Primary accent, CTA buttons, active nav, focus rings |
| `--ember-hi` | `#ffb068` | Hover lift of --ember |
| `--ember-lo` | `rgba(240,132,40,0.08)` | Subtle ember-tinted backgrounds |
| `--ember-glow` | `rgba(240,132,40,0.22)` | Soft shadow glow |

### Semantic

| Token | Hex | Use |
|-------|-----|-----|
| `--jade` | `#22c87a` | Success, downloaded, healthy |
| `--gold` | `#f0b828` | Warning, grabbed, pending |
| `--ruby` | `#f05050` | Destructive, critical, failed |
| `--sky` | `#58a8f8` | Info, links, releasing status |
| `--iris` | `#9878f8` | Alt accent (hiatus, nzb, special) |
| `--rose` | `#f87898` | Alt accent (rare use) |

---

## 3. Typography — The Editorial Triple Stack

The skill's "Minimalist Monochrome Editorial" recommendation, adapted with Inter for UI chrome and retaining the brand italic.

### Stack

```css
--font-display: 'Fraunces', 'Playfair Display', 'Georgia', serif;
--font-serif:   'Source Serif 4', 'Libre Baskerville', 'Georgia', serif;
--font-ui:      'Inter', system-ui, sans-serif;
--font-mono:    'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
--font-italic:  'Instrument Serif', 'Georgia', serif;  /* Brand italic — legacy */
```

### Why these fonts

- **Fraunces** — variable optical-size serif. Display headings and big editorial numbers. Replaces old Syne for display.
- **Source Serif 4** — variable, highly legible body serif. For manga synopses, long descriptions, italic subtitles.
- **Inter** — UI chrome (buttons, form labels, small table cells).
- **JetBrains Mono** — tabular figures for numbers that change. Prevents layout shift.
- **Instrument Serif** — kept from legacy for the "Mangarr" wordmark and rare editorial flourishes.

### Type scale

| Role | Size | Line | Weight | Font | Features |
|------|------|------|--------|------|----------|
| Hero | `4rem` | `1` | 900 | display | tracking-tight |
| H1 | `2.5rem` | `1.1` | 700 | display | tracking-tight |
| H2 | `1.75rem` | `1.2` | 600 | display | — |
| H3 | `1.25rem` | `1.3` | 600 | display | — |
| Subtitle | `1rem` | `1.5` | 400 | serif | italic |
| Body | `1rem` | `1.6` | 400 | serif | — |
| UI default | `0.875rem` | `1.4` | 500 | ui | — |
| Label | `0.6875rem` | `1` | 600 | ui | uppercase tracking-widest |
| Num display | `3rem` | `0.95` | 500 | mono | tabular-nums |
| Num large | `1.5rem` | `1` | 500 | mono | tabular-nums |
| Num default | `0.875rem` | `1` | 500 | mono | tabular-nums |

### Rules
- Never skip heading levels
- Uppercase labels: Inter 600 + letter-spacing 0.08em + 11px
- All numerics render in mono with tabular-nums
- Italic is editorial (subtitles, pull quotes). Never italicize UI chrome.

---

## 4. Layout & Grid

- **Container**: max-w-7xl (1280px)
- **Page padding**: 32px desktop, 16px mobile
- **Sidebar**: 228px expanded, 60px collapsed (unchanged)
- **12-col grid**, 24px gutter
- **Spacing scale**: 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128
- **Section rhythm**: 48px (sm), 80px (md), 128px (chapter-level)

---

## 5. Editorial Component Vocabulary

Distinctive components that give the app its editorial character:

### `.page-head` — The masthead
- Running head (uppercase label, --text-3)
- Hero display title (Fraunces 900, 4rem)
- Italic subtitle (Source Serif 4 italic)
- 1px rule below (--edge)

### `.editorial-num` — Oversized stat
- Mono tabular 3rem number
- Uppercase label below in small caps

### `.section-rule` — Magazine section divider
- Horizontal rule with small uppercase label in the middle ("§ STATISTICS")

### `.pull-quote` — Callout / important info
- Left border 3px --ember
- Italic serif body
- Optional attribution line

### `.marginalia` — Small metadata
- Right-aligned, Inter 11px uppercase
- Used for dates, source attribution

### `.drop-cap` — For rare descriptive paragraphs
- First letter: float left, Fraunces 3.5rem, line-height 0.85, ember color

### `.page-number` — "01 / 12" style
- Mono tabular, bottom-right of lists

---

## 6. Motion & Interaction

- **Durations**: 150ms (micro), 200ms (default), 300ms (large). Never >400ms.
- **Easing**: `cubic-bezier(0.25,0.46,0.45,0.94)` entering, `cubic-bezier(0.55,0.085,0.68,0.53)` exiting.
- **Hover lift**: `translateY(-2px)` (subtle, not 8px).
- **Press**: `scale(0.98)` on buttons, 60ms.
- **Fade-in**: 200ms, no per-item stagger (editorial > demo-y).
- **Reduced motion**: respected — collapses to 0.01ms.
- **Focus rings**: 2px ember, 2px offset.

---

## 7. Cards & Surfaces

### Manga card (library grid) — "book spine on a shelf"
- Aspect 2/3
- Background `--ink-2` with 1px `--edge` border
- Border-radius: 4px (tighter, more editorial than current 12px)
- Hover: translateY(-2px) + border `rgba(240,132,40,0.45)` + shadow
- Title: Fraunces 700, 0.9rem, 2-line clamp
- Progress: 2px ember bar, jade when complete
- Editorial touch: thin ember bookmark strip animates in on hover

### Panel
- Background `--ink-2`, 1px `--edge`, 4px radius
- Header: `--ink-3` background, uppercase label font 13px 600
- Padding: 20px default, 32px for feature panels

### Table
- Header: `--ink-3`, 11px uppercase Inter 600 letter-spacing 0.08em
- Row divider: 1px `--edge`
- Row hover: `--ink-3` background
- Min row height: 44px (touch compliance)
- Numeric columns: mono tabular, right-aligned

---

## 8. Button System

### Primary (ember)
Background `--ember`, white text, 1px solid `--ember`, 6px radius, 8px 16px padding. Inter 600 13px. Hover: `--ember-hi` + glow. Active: scale(0.98).

### Secondary (neutral)
Transparent background, 1px `--edge-2`, `--text-2`. Hover: `--ink-3` bg, `--text`.

### Danger
`--ruby-bg` bg, `rgba(240,80,80,0.3)` border, `--ruby` text.

### Ghost (link)
No bg/border, `--text-2` with underline on hover.

### Icon-only
32×32 square, ghost default, requires title + aria-label.

---

## 9. Form Controls

- Height: 40px minimum
- Background: `--ink-3`
- Border: 1px `--edge-2`, 6px radius
- Text: `--text`, Inter 400, 14px
- Placeholder: `--text-3`
- Focus: border `--ember`, box-shadow `0 0 0 3px rgba(240,132,40,0.15)`
- Label: above input, 12px uppercase Inter 600 `--text-2`, 8px gap
- Helper text: below, 12px Inter 400 `--text-3`
- Error: border `--ruby`, helper becomes `--ruby`

---

## 10. Anti-patterns

- ❌ No emoji as structural icons (Bootstrap Icons or Lucide)
- ❌ No UI text below 11px
- ❌ No italic in UI chrome
- ❌ No Fraunces for body text (display-only)
- ❌ No skipped heading levels
- ❌ No color-only state (always icon + text)
- ❌ No animating width/height/top/left (exception: sidebar width transition — layout depends on margin-left responding to width; mobile sidebar correctly uses transform)
- ❌ No removed focus rings
- ❌ No border-radius > 8px on panels
- ❌ No stagger total > 400ms
- ❌ No more than ONE ember primary CTA per screen

---

## 11. Accessibility (non-negotiable)

- Contrast: 4.5:1 normal text, 3:1 large text
- Focus ring: 2px ember, 2px offset
- Keyboard nav + ESC closes modals
- aria-label on icon-only buttons
- aria-live on toasts, role=img on charts
- prefers-reduced-motion respected
- 200% zoom must not break layout
- Form labels always present

---

## 12. Per-page pattern hints

Each major page leans into a specific editorial pattern. Page-specific rules live in `design-system/mangarr/pages/<page>.md`.

- **Library index** — Masthead + grid, oversized editorial numbers for stats
- **Series detail** — Hero spread with large cover, metadata as marginalia, volume grid as "contents"
- **Wanted** — Table with oversized running count in the masthead
- **Stats** — Editorial data magazine: big numbers, small charts
- **Health** — Doctor's report: big status, issue list with clear severity
- **Settings** — Editorial form: label above input, section breaks with horizontal rules

---

## 13. Stack notes

- **Tailwind via CDN**. No build step.
- **Keep existing CSS variables** for backwards compat.
- **Progressive migration** — page by page. Untouched pages continue to work.
- **No new JS framework**. HTMX + Alpine only.
- **Bootstrap 5 coexists** with Tailwind utilities.
