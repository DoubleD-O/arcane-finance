# Changing how the Comex deck looks

The deck's numbers and wording come from the close tool. Which slides it has, in what order and with what titles, comes from a pack file in `config/deck/packs/` (see [Choosing and editing packs](#choosing-and-editing-packs)). Its look comes from two files you can edit yourself in `config/deck/`. Your changes apply every time the deck is regenerated, for every release.

| File | Open it with | What it controls |
|---|---|---|
| `template.pptx` | PowerPoint | Fonts, theme colours, background, logo, title position and style, footer position |
| `style.toml` | Notepad or any text editor | Bridge chart colours, table header colour, footer text colour, table and footer text sizes |

## Change the template

1. Open `config/deck/template.pptx` in PowerPoint.
2. Go to **View > Slide Master**.
3. Make your changes on the master (the large slide at the top of the left-hand list) and on two layouts:
   - **Title Slide** is used for slide 1. Its title holds the deck name, and its subtitle holds the release, approval, currency and status lines.
   - **Title Only** is used for every other slide. Only its title is filled in. The tables and chart are placed below it.
4. Close Master View and save the file. Keep the name `template.pptx` and the PowerPoint format (.pptx).
5. Regenerate the deck, for example `fct deck --release 2026-10-R2 --out decks`.

Any slides you leave in the template are ignored. You can keep a sample slide in it to preview your changes.

**Safe to change.** Theme fonts and colours (**Design > Variants > Colors / Fonts**), background, logos and shapes on the master, the title's font, size, colour and alignment, and the slide size (**Design > Slide Size**, for example 4:3). Tables use the template's default table style. To change it, add a table to a sample slide, right-click a style under **Table Design > Table Styles**, choose **Set as Default**, then delete the table. The style follows the theme's **Accent 1** colour unless `style.toml` sets the header colour.

**Keep in mind.**
- Keep the layout names **Title Slide** and **Title Only**. If one is missing or renamed, the tool uses the first layout that has a title instead and prints which one it used.
- Keep the title in the top strip of the slide (about the top 15%). Tables and the chart start just below it, and a title moved lower will overlap them.
- The footer placeholder on the master sets where the footer line goes. Everything else is fitted above it, so moving it up squeezes the tables. Keep it in the bottom half of the slide.
- Long titles wrap on a narrow slide. If the deck name wraps into the lines below it, make the Title Slide title smaller or its box taller.
- On a 4:3 slide, some table headings wrap onto two lines. Setting `table_size = 12` in `style.toml` avoids most of it.

## Change the style file

`config/deck/style.toml` holds the colours and sizes PowerPoint's theme can't reach, because the tool sets them on each table, chart bar and footer:

```toml
[colors]                  # hex RGB, as in PowerPoint's More Colors > Custom > Hex
favourable = "2F855A"     # bridge bars and labels better than budget
unfavourable = "C0392B"   # bridge bars and labels worse than budget
total = "1F3864"          # bridge budget and actual bars
table_header = "1F3864"   # put a # in front to use the table style's own header colour
footer_text = "6B7785"

[fonts]                   # points
table_size = 13           # tables and chart; the denser tables are 1-2 points smaller
footer_size = 10
```

Every line is optional. Delete one and the tool's original value comes back: favourable `2E7D32`, unfavourable `C62828`, total `44546A`, no table header colour, footer text `595959`, table size 14, footer size 10. A misspelt name, a colour that isn't six hex digits (no `#`), or a size outside 6 to 40 stops the run with a message naming the line, and no deck is written.

## What the starter design uses

`config/deck/template.pptx` and `style.toml` ship with a navy and teal office design. To change a colour everywhere, change it in the theme (**Design > Variants > Colors > Customize Colors**) and, for the four colours the tool applies itself, in `style.toml` too.

| Colour | Hex | Where | Set in |
|---|---|---|---|
| Navy | `1F3864` | Titles, title slide background, table headers, bridge totals | Theme **Text/Background - Dark 2** and **Accent 1**; `style.toml` `total`, `table_header` |
| Teal | `0F7C8C` | Accent bars under the titles and above the deck name | Theme **Accent 2** |
| Light grey | `F2F4F7` | Footer band | Theme **Text/Background - Light 2** |
| Mid grey | `6B7785` | Footer text | Theme **Accent 3**; `style.toml` `footer_text` |
| Near-black | `1E2530` | Body text, table text, chart axes | Theme **Text/Background - Dark 1** |
| Green | `2F855A` | Favourable bridge bars and labels | `style.toml` `favourable` (also theme **Accent 4**) |
| Red | `C0392B` | Unfavourable bridge bars and labels | `style.toml` `unfavourable` (also theme **Accent 5**) |
| Steel blue | `8FA3BF` | Title slide tag line | Theme **Accent 6** |
| Pale grey-blue | `D3DBE6` | Title slide release and status lines | The **Title Slide** layout's subtitle |

Fonts come from the theme (**Design > Variants > Fonts > Customize Fonts**): headings **Segoe UI Semibold**, body **Segoe UI**. Titles are 26 pt on content slides and 40 pt on the title slide; the title slide lines are 18 pt. Tables use **Medium Style 3 - Accent 1** (navy header, light grey banding, no grid lines).

The shapes on the master and layouts are ordinary shapes you can move, recolour or delete in Slide Master view: **Footer band** on the master, **Accent bar** on both layouts, and **Tag line** ("APAC FINANCE | EXECUTIVE COMMITTEE") on the Title Slide layout.

## What the tool controls

Every number and its formatting, the Favourable and Unfavourable wording, the footer text (release, period, currency, checksum, DRAFT), and where tables and the chart sit relative to the slide size. Body text on the title and outlook slides, other than the subtitle, keeps fixed sizes. The slides, their order and titles come from the pack.

## Trying a different file

`fct deck ... --template other.pptx --style other.toml` uses those files for one run without touching `config/deck/`. If `config/deck/` is empty, the deck is built with the tool's original built-in look and the Comex pack.

## Choosing and editing packs

A pack is a text file that lists the deck's slides in order. Three ship in `config/deck/packs/`:

| Pack | Slides | Needs commentary |
|---|---|---|
| `comex` (the default) | Title, KPI scorecard, P&L for APAC, markets, EBITDA bridge, changes since the previous release, outlook and actions | Yes |
| `board` | Title, headline KPIs (revenue, EBITDA, EBITDA %), EBITDA bridge, outlook and actions | Yes |
| `market_review` | Title, markets, a P&L for each market, changes since the previous release | No |

Choose one with `fct deck --release 2026-10-R2 --out decks --pack board`, or give your own file with `--pack my_pack.toml`. `fct deck packs` lists the packs and their slides. In the UI, choose the pack on the **Releases and downloads** page, or choose **Upload a pack file** and upload yours.

The deck's file name starts with the pack's file name: `Comex_2026-10_2026-10-R2.pptx`, `Board_2026-10_2026-10-R2.pptx`, `Market_review_2026-10_2026-10-R2.pptx`.

To make your own, copy a pack in `config/deck/packs/` to a new name, for example `cfo.toml`, and edit it in Notepad. It then shows in `fct deck packs` and in the UI as `cfo`. Editing `comex.toml` changes the default deck.

```toml
name = "CFO pack"
title = "APAC CFO pack, {period_long}"   # the title slide; leave it out for "APAC <name>, <month year>"

[[slides]]
type = "title"

[[slides]]
type = "kpi_scorecard"
kpis = ["revenue", "ebitda"]

[[slides]]
type = "bridge"
kpi = "gross_margin"
title = "Where gross margin moved, {period_long}"
```

Each `[[slides]]` block is one slide, in order. `type` says which slide. Every slide may set a `title`; without one it keeps the title below.

| Type | Options | Default title |
|---|---|---|
| `title` | none | the pack's `title` |
| `kpi_scorecard` | `kpis`: a list from `revenue`, `gross_margin`, `gm_pct`, `ebitda`, `ebitda_pct`, in the order to show them (default: all five) | `KPI scorecard, APAC` |
| `pnl` | `scope`: `"APAC"` (default), a market code such as `"AU"`, or `"each_market"` for one slide per market in `close.toml` order | `P&L vs budget, {market}` |
| `markets` | none | `Markets` |
| `bridge` | `kpi`: `revenue`, `gross_margin` or `ebitda` (default) | `{kpi} bridge, budget to actual` |
| `changes` | none; left out of the deck when the release has no previous release | `Changes since {previous}` |
| `outlook` | none; shows the commentary file | `Outlook and actions` |

Titles can use these placeholders:

| Placeholder | Becomes | Where |
|---|---|---|
| `{period}` | `2026-10` | every title |
| `{period_long}` | `October 2026` | every title |
| `{release}` | `2026-10-R2`, or `PROPOSED` | every title |
| `{currency}` | `SGD` | every title |
| `{market}` | `APAC` or the market code | `pnl` slides |
| `{kpi}` | `Revenue`, `Gross margin` or `EBITDA` | `bridge` slides |
| `{previous}` | the previous release, e.g. `2026-10-R1` | `changes` slides |

Write `{{` and `}}` for a literal brace.

**DRAFT.** A deck built from an approved release is DRAFT only when its pack has an `outlook` slide and no commentary file was given. A pack without an `outlook` slide needs no commentary. A proposed release is always DRAFT.

**Mistakes stop the run.** An unknown slide type, option, KPI, market or placeholder stops the run with a message naming the file and the slide number, for example `cfo.toml: slide 3 (pnl): unknown scope 'NZ'; expected one of APAC, SG, AU, each_market`. No deck is written. The footer and every number stay the tool's; a pack only picks slides and titles.

To start again from the starter template, run `python scripts/make_deck_template.py`. It overwrites `config/deck/template.pptx`. `--design classic` writes the plainer template that matches the tool's built-in look instead (use it with a `style.toml` that has every line deleted).
