# Design sources

The canvas these files seed is the visual specification for
`src/newsletter/rendering/templates/newsletter.html.j2`. Change the design here,
then port the change into the template — the template is what ships.

| File | Artboard |
|------|----------|
| `LeaderIntelligenceSemanal.dc.html` | **current** — the "Broadsheet" edition, one artboard at desktop width |
| `LeaderIntelligenceSemanal.css` | the Broadsheet tokens and component classes, as exported |
| `Main.dc.html` | superseded — the previous edition at desktop width |
| `Mobile.dc.html` | superseded — the previous edition on a phone |
| `Components.dc.html` | superseded — every piece, labelled with the field it renders |
| `canvas.json` | artboard layout, notes, launch view (describes the superseded canvas) |

## Where the current design came from

`LeaderIntelligenceSemanal.*` is the owner's Broadsheet canvas, exported from
Claude Design and unpacked here verbatim so the specification lives in the
repository rather than behind a link. Two things in the export are deliberately
**not** ported into the shipped template:

- the `@font-face` blocks and their ~1.5 MB of woff2 payload — the template
  names `"Source Serif 4"` first and falls back to a system serif, because an
  edition is emailed and archived;
- the CMYK / halftone print treatments (`.cmyk`, `.cmyk-head`, `.halftone`),
  which need SVG filter defs and the `print-plates.js` driver. The edition ships
  no JavaScript.

Re-seed and republish after an edit (the seeded `.html` is generated and ignored):

```bash
node "<design skill>/seed-canvas.mjs" \
  --template "<design skill>/payload.template.html" \
  --out intelligence-weekly-edition.html \
  --title "Intelligence Weekly Edition" \
  --artboard design/Main.dc.html \
  --artboard design/Mobile.dc.html \
  --artboard design/Components.dc.html \
  --canvas design/canvas.json
```

Two constraints the design must keep, because the shipped HTML is bound by them:

- **No images and no external assets.** The engine stores no images and the
  edition must render offline, so the layout carries its weight with type, rules
  and accent fills rather than photographs, and the type is a system stack.
- **Only fields the engine produces.** Headline, summary, why it matters, key
  facts, source name, publication date, score and category. There is no author,
  no image and no reading time, so none appear.
