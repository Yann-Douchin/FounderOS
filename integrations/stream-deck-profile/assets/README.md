# FounderOS icon suite

This suite covers every visible surface across the five Stream Deck profiles. It contains 60 dedicated SVG masters and 60 PNG outputs in 144 x 144 pixel RGBA sRGB format.

The visual system uses one grid and one graphic vocabulary:

- rounded midnight-blue background designed for Stream Deck OLED and LCD displays;
- text-free white glyph, centered above the native key title;
- uniform stroke weight with rounded corners and endpoints;
- top marker and status dot in the functional color;
- blue for FounderOS, purple for focus, green for capture, amber for calls, red for studio and risky actions, cyan for presentations;
- explicit variants for active, inactive, pause, and resume states.

Filenames encode the profile, controller, position, and action. Dials, the mode wheel, the Prompter stack, Stream Deck Pedal, and iPhone therefore have dedicated assets even when they share a semantic glyph.

## Regeneration

On macOS, run:

```sh
python3 integrations/stream-deck-profile/assets/generate_icons.py \
  --plugin-root integrations/stream-deck/com.yanndouchin.founderos-actions.sdPlugin
```

The generator uses Quick Look to render the SVG masters accurately, then ImageMagick to normalize the PNG files and build the preview. The plugin can also regenerate only its SVG files with `npm run icons`.

Generated PNG files are deterministic artifacts that stay alongside their SVG masters. Use `icon-suite-preview.png` for visual QA of the whole suite.
