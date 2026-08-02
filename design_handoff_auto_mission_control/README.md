# Handoff: Strategy Studio — AUTO screen ("Mission Control")

## Overview
Redesign of the **AUTO** tab of Strategy Studio in NAUTILUS·LAB (autonomous backtest agent).
The current AUTO screen is a long vertical scroll: a large config form, then a run status bar, then a timeline, then an ever-growing list of step cards. Users lose the run state as soon as they scroll, and there is no view of the produced strategies.

The redesign turns AUTO into a **single non-scrolling screen** (mission-control layout):
- run configuration is condensed into a read-only **BRIEF** rail and moved to a slide-over editor,
- the center is a **live focus area** (current step, progress ring, phase strip, console),
- the right rail lists the **iterations/candidates** produced so far with equity sparklines and a promote action.

Chosen direction: option `1c`. Two rejected alternatives (`1a` split cockpit, `1b` command bar) are included for context.

## About the Design Files
The files in this bundle are **design references created in HTML** — prototypes showing intended look and behavior, **not production code to copy directly**. They use a small in-house streaming-component runtime (`support.js`, `<x-dc>` templates + a `Component` logic class); that runtime is an authoring convenience, not part of the deliverable.

The task is to **recreate these designs in the target codebase's existing environment** (React/Vue/Svelte + whatever styling system Strategy Studio already uses) with its established patterns, components and data layer. All data in the prototypes is mocked and hardcoded; wire it to the real research-loop backend.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and states are final and specified below. Recreate pixel-faithfully with the codebase's own component library. The color palette and mono type are derived from the existing AUTO screen screenshot (`reference-current-auto-screen.png`), so it should match the rest of the app.

---

## Screens / Views

### 1. AUTO — Mission Control (primary deliverable)
File: `AUTO Mission Control.dc.html`

**Purpose:** monitor and steer an autonomous research loop (generate strategy → backtest N times → robustness → catalog) without leaving one screen.

**Layout**
- Root: `position:fixed; inset:0; display:flex; flex-direction:column; overflow:hidden` — the screen never scrolls.
- Top bar: fixed 48px, `border-bottom:1px solid #241c17`, horizontal padding 20px.
- Body: CSS grid, `grid-template-columns: minmax(190px,240px) minmax(360px,1fr) minmax(230px,300px)`, `min-height:0` (critical — otherwise the center column can't shrink).
  - **Left rail** — BRIEF (read-only summary) + BUDGET + two run options, background `#120e0d`, `border-right:1px solid #241c17`.
  - **Center column** — `display:flex; flex-direction:column; padding: clamp(14px,2vh,24px) clamp(16px,2vw,28px); gap: clamp(12px,1.8vh,18px)`.
    1. Hero row — `flex:none` (must NOT be clipped): progress ring + status + headline, and iteration counter pushed right by a `flex:1` spacer.
    2. Phase strip — `flex:none`, one row, `flex-wrap:nowrap`; five cells `flex:1 1 0; min-width:0`, each cell's label and value carry `white-space:nowrap; overflow:hidden; text-overflow:ellipsis` (put it on the text nodes, not the cell).
    3. Console — `flex:1 1 200px; min-height:130px; overflow:hidden; position:relative`, inner list `flex-direction:column-reverse` so the newest line sits at the bottom next to the caret; a top gradient (`linear-gradient(180deg,#120e0d,transparent)`, 34px) fades older lines out.
  - **Right rail** — iteration/candidate cards, `overflow-y:auto; overflow-x:hidden; scrollbar-width:none`; the "Lideri kataloğa ekle" button is `flex:none` pinned at the bottom.
- **Brief slide-over** — `position:absolute; top:48px; bottom:0; left:0; width:420px`, `transform: translateX(-100%)` when closed → `0` when open, `transition: transform .22s ease`, `box-shadow: 24px 0 60px rgba(0,0,0,.55)`, plus a full-screen scrim `rgba(8,6,5,.6)` that closes on click.

**Components**

| Component | Spec |
|---|---|
| Top bar brand | 22px orange circle `#e0762e`, glyph `◕` in `#160f0a`; breadcrumb 11px, `letter-spacing:.18em`, `#8a7d71`; active segment `AUTO` in `#e0762e` |
| Mode tabs | text 10.5px; active tab `background:#2b1c12; color:#e0762e; border-radius:4px; padding:4px 10px` |
| STOP / START button | `padding:6px 14px; radius:4px; border:1px solid #6b3a2c; background:#241512; color:#d9755d; font-weight:700; font-size:10.5px`; hover `background:#301a15`. Label toggles `■ STOP` ⇄ `▶ START` |
| BRIEF rows | label `#8a7d71` 11px, value `#e9e1d7`; row `padding:7px 0; border-bottom:1px dashed #241c17`; timeframe value in `#e0762e` |
| BUDGET card | `border:1px solid #241c17; radius:6px; background:#151110; padding:11px 12px`; bars 3px tall on `#241c17`, time fill `#5ec9a0`, token fill `#e0762e` |
| Checkboxes | 13px box, radius 3px; off `border:1px solid #33281f`; on `border:1px solid #e0762e; background:#2b1c12`, check glyph `✓` 9px `#e0762e` |
| Progress ring | `width/height: clamp(70px,7vw,100px)`, `background: conic-gradient(#e0762e 0turn Xturn, #241c17 Xturn 1turn)`, inner disc 78% of size in `#0f0c0b` with `NN%` 19px 700 `#e0762e` over `ADIM` 8.5px `#8a7d71` |
| Status pill | running: `border:1px solid #2f5f4c; background:#15241e; color:#5ec9a0`, text `RUNNING`; stopped: `#33281f / #1a1513 / #8a7d71`, text `DURDURULDU` |
| Headline | `font-size: clamp(17px,2.1vw,28px); font-weight:700; line-height:1.15; letter-spacing:-.01em; text-wrap:pretty` |
| Iteration counter | `clamp(24px,2.6vw,36px)` 700; `/N` suffix at `.55em` in `#5a4f46` |
| Phase cell | label 9px `letter-spacing:.1em`; value 11.5px. Done: bg `#15201b`, value `#5ec9a0`. Active: bg `#231710`, label+value `#e0762e`. Pending: no bg, `#5a4f46` |
| Console line | 11.5px; time `#5a4f46`, kind tag fixed 34px wide (`data` `#5ec9a0`, `llm` `#d9a441`, `test` `#e0762e`), message `#c9bdb2`; live caret row ends with `▊` blinking 1s `steps(1)` infinite |
| Candidate card (leader) | `border:1px solid #2f5f4c; background:#15201b; radius:6px; padding:10px 11px`; title 11px, score `★ 1.84` `#5ec9a0`; SVG sparkline `stroke:#5ec9a0; stroke-width:1.5; fill:none`, `preserveAspectRatio="none"`; metrics row 9.5px `#8a7d71` |
| Candidate card (past) | `border:1px solid #241c17; background:#151110`, sparkline stroke `#8a7d71` |
| Candidate card (active) | `border:1px solid #e0762e; background:#1d1410`; 3px progress bar `#e0762e` on `#2a211b` |
| Queued card | `border:1px dashed #241c17; padding:9px 11px; font-size:10.5px; color:#5a4f46` |
| Primary button | `height:36px; radius:5px; background:#e0762e; color:#160f0a; font-weight:700; font-size:11px`; hover `#f19351` |
| Secondary button | `height:36px; radius:5px; border:1px solid #33281f; color:#c9bdb2` |
| Form field (slide-over) | `height:34px; border:1px solid #33281f; radius:4px; background:#0f0c0b; padding:0 10px; font-size:12px`; placeholder `#5a4f46` |
| Timeframe toggle | `flex:1; height:32px; radius:4px`; off `border:1px solid #33281f; color:#8a7d71`; on `border:1px solid #e0762e; background:#2b1c12; color:#e0762e`. Multi-select, at least one must stay selected |
| Iterations slider | native range, `accent-color:#e0762e`, min 1 max 20 |

**Exact copy used** (Turkish UI): `BRIEF`, `✎ değiştir`, `BÜTÇE`, `süre`, `token`, `Kazanandan sonra yeniden başlat`, `Bittiğinde bildir`, `ŞU AN`, `RUNNING` / `DURDURULDU`, `Backtest #3 · walk-forward` / `Döngü duraklatıldı`, `İTERASYON`, `DATA / STRATEJİ / BACKTEST / ROBUSTNESS / KATALOG`, `✓ 5 set`, `✓ üretildi`, `▸ çalışıyor`, `bekliyor`, `KONSOL`, `İTERASYONLAR`, `#4 sırada`, `Lideri kataloğa ekle`, `BRIEF'İ DÜZENLE`, `Boş guidance = tam otonom araştırma.`, `Vazgeç`, `Uygula`, `birden fazla seçilebilir`, `TARİH ARALIĞI`, `MAX SAAT 0=∞`, `MAX TOKEN 0=∞`.

### 2. Alternatives doc (context only)
File: `Strategy Studio AUTO (alternatives).dc.html` — three 1400×860 static mocks: `1a` split cockpit (config as a fixed left column), `1b` command bar (config collapsed to chips, screen given to activity + candidate leaderboard), `1c` mission control (shipped). Useful if the team wants to revisit; not part of the build.

---

## Interactions & Behavior
- **STOP / START** (top right) toggles `running`. When stopped: ring shows `—` and 0 fill, status pill → `DURDURULDU`, headline → `Döngü duraklatıldı`, sub-line → `Devam etmek için sağ üstten START…`, BACKTEST phase cell → `duraklatıldı`, active candidate badge → `duraklatıldı`, caret row dims to `opacity:.25`, ticker stops.
- **`✎ değiştir`** opens the brief slide-over (left, 420px, 220ms ease); scrim click, `✕`, `Vazgeç` and `Uygula` all close it. `Uygula` should commit the edited brief to the run config (prototype closes without diffing).
- **Timeframe chips** toggle multi-select; deselecting the last one is prevented.
- **Iterations slider** updates the brief value live and the `N` in `İTERASYON x/N`.
- **Checkboxes** toggle instantly.
- **Live ticker** (prototype: 1s interval): `progress += 2`; at ≥100 it wraps to 4, advances the iteration (wrapping back to 1 after N), and prepends a console line `backtest #N başladı · walk-forward 4 kat`; elapsed +1s and tokens +90 each tick. In production drive all of this from the backend event stream (SSE/WebSocket) instead.
- **Console** is bottom-anchored (`column-reverse`), capped at 9 stored lines in the prototype; no auto-scroll needed because the newest line is pinned at the bottom. Timestamps must increase monotonically.
- **Hover states**: primary button `#e0762e → #f19351`; STOP `#241512 → #301a15`; all clickable elements `cursor:pointer; user-select:none`.
- **Responsive**: single screen from ~920×540 up. The center column must always win space — the hero is `flex:none`, the phase strip never wraps (text ellipsizes), the console is the only flexible child with `min-height:130px`. Below ~900px wide, collapsing the right rail into a tab next to the console is the recommended next step (not yet designed).

## State Management
```
running: boolean            // loop active
progress: number 0..100     // current step progress → ring + active card bar
iter: number                // current iteration index
elapsed: number (seconds)   // → BÜTÇE süre + console clock
tokens: number              // → BÜTÇE token + top bar cost
briefOpen: boolean          // slide-over
loop: boolean               // "restart after a winner"
notify: boolean             // "notify when finished"
brief: { symbol, model, category, robustness, range, iterations, guidance, tfs[] }
lines: [{ t, kind, color, message }]   // newest first
candidates: [{ id, name, sharpe, pf, maxdd, trades, equity[], state }]
```
Data needs: run state + brief (GET/PATCH), an event stream for console lines and phase/progress changes, a candidates list with equity curves, and a "promote to catalog" mutation.

## Design Tokens
**Color**
```
--bg-app          #0f0c0b   page background
--bg-panel        #120e0d   rails, console, cards container
--bg-card         #151110   inset cards
--bg-inset        #0f0c0b   inputs, chart wells
--border          #241c17   panel/divider borders
--border-strong   #33281f   input borders
--accent          #e0762e   AUTO / active / primary
--accent-hover    #f19351
--accent-bg       #2b1c12   active chip background
--accent-bg-soft  #1d1410   active card background
--accent-bg-deep  #231710   active phase cell
--success         #5ec9a0   completed, positive metrics
--success-bg      #15201b / #15241e
--success-border  #2f5f4c
--warn            #d9a441   llm events
--danger          #d9755d   stop label   (#b4543a for negative metrics)
--danger-bg       #241512   --danger-border #6b3a2c
--text            #e9e1d7
--text-muted      #c9bdb2
--text-dim        #8a7d71
--text-faint      #5a4f46
```
**Type** — JetBrains Mono (400/500/700), everything monospace.
Scale: 8.5 / 9 / 9.5 (labels, `letter-spacing:.1–.18em`, uppercase) · 10–10.5 (meta) · 11–11.5 (body, table values) · 12 (form values) · `clamp(17,2.1vw,28)` headline · `clamp(24,2.6vw,36)` iteration counter · 19 (ring %).
**Spacing** — 3 / 5 / 8 / 10 / 12 / 14 / 16 / 18 / 24 px; column gutters `clamp(12,1.8vh,18)`.
**Radius** — 3 (bars/wells) · 4 (inputs, chips) · 5 (buttons) · 6 (cards, panels) · 20 (pills) · 50% (ring, brand mark).
**Borders** — 1px solid; dashed `#241c17` for brief rows and queued cards.
**Shadow** — only the slide-over: `24px 0 60px rgba(0,0,0,.55)`.
**Motion** — slide-over `transform .22s ease`; caret `blink 1s steps(1) infinite`; ticker 1000ms.

## Assets
No image assets. The brand mark is the glyph `◕` in an orange circle (placeholder — swap for the real NAUTILUS·LAB logo). Nav/status glyphs are Unicode (`▦ ▤ ◈ ⛭ ▢ ⌁ ▥ ◉ ≡ ✓ ▸ ○ ★ ■ ▶ ✎ ✕ ▊`) — replace with the codebase's icon set. Equity curves are inline `<svg><polyline>` with mock points. Font: JetBrains Mono via Google Fonts.

## Screenshots
`screens/` — captured at a narrow 924×540 viewport, i.e. the *worst case* the layout must survive (text wraps and phase-cell labels ellipsize here; at ≥1400px everything sits on one line as specified above).
- `01-auto-mission-control.png` — running state.
- `02-auto-mission-control.png` — brief slide-over open.
- `03-auto-mission-control.png` — stopped state (`DURDURULDU`).

## Files
- `AUTO Mission Control.dc.html` — the design to implement (open directly in a browser).
- `Strategy Studio AUTO (alternatives).dc.html` — the three explored options, context only.
- `support.js` — runtime required by the two HTML files; not part of the deliverable.
- `reference-current-auto-screen.png` — screenshot of the current AUTO screen the palette was derived from.
