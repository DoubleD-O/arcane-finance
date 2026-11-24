# Deck styling: template and style file

Goal: a finance user changes the look and feel of the Comex deck in PowerPoint, without code, and the change survives every regeneration. Content, numbers and slide order stay owned by the code (`docs/design-step2.md`).

## What the finance user edits

1. **`config/deck/template.pptx`**: an ordinary PowerPoint file. He opens it, edits **View → Slide Master** (fonts, theme colours, background, logo, footer area), and saves. Any sample slides in it are ignored.
2. **`config/deck/style.toml`**: the colours and sizes the code applies itself, because a theme doesn't reach them:
   ```toml
   [colors]            # hex RGB
   favourable = "2E7D32"
   unfavourable = "C62828"
   total = "44546A"
   table_header = "4F81BD"   # omit to use the template's table style
   footer_text = "595959"
   [fonts]
   table_size = 14           # points
   footer_size = 10
   ```
   Every key is optional; a missing key falls back to today's look. Unknown keys and invalid hex are a clear error.

## Rules for the code

- `deck --template FILE --style FILE` override the defaults above; when `config/deck/` files are absent, output is today's deck exactly (existing tests unchanged).
- Layouts are found **by name**: "Title Slide" for slide 1 and "Title Only" for the rest. If a template lacks one, fall back to the first layout with a title placeholder, and say which layout was used.
- Remove the template's own slides before adding ours.
- **Positions are proportional to the template's slide size** (no hard-coded 13.33 × 7.5 in), so a 4:3 or custom-size template still fits. Tables and charts stay within the slide, above the footer.
- Text takes the template's theme fonts unless `style.toml` sets a size; colours not in `style.toml` come from the theme. Tables take the template's default table style (PowerPoint draws only its built-in table styles, so a template picks one rather than defining its own).
- Deck parity, R1-unchanged, D1–D5 and the bridge-geometry tests must pass with the default style **and** with a test template that has a different slide size, font and colours.
- Ship a starter `config/deck/template.pptx` and `style.toml` with an office design (navy and teal theme, Segoe UI, a footer band; `scripts/make_deck_template.py`, colours listed in `docs/deck-styling.md`), plus `docs/deck-styling.md`: a one-page how-to in plain English (open template, edit Slide Master, save, run `fct deck`, what's safe to change, what the code controls).
- Render the R2 deck with the starter template and with the test template to PNG through PowerPoint and look at every slide.
