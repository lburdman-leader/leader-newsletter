# Design sources

The canvas these files seed is the visual specification for
`src/newsletter/rendering/templates/newsletter.html.j2`. Change the design here,
then port the change into the template — the template is what ships.

| File | Artboard |
|------|----------|
| `Main.dc.html` | the edition at desktop width |
| `Mobile.dc.html` | the same edition on a phone |
| `Components.dc.html` | every piece, labelled with the field it renders |
| `canvas.json` | artboard layout, notes, launch view |

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
