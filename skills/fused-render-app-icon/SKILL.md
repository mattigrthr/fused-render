---
name: fused-render-app-icon
description: Design the optional icon.svg for a fused-render app — the file the shell shows as the app's sidebar glyph and browser-tab favicon, and the one a published app installs as its phone home-screen icon. Use when the user asks for an app icon, home-screen icon, logo, favicon or glyph, or wants to add, change or fix icon.svg.
---

# An app's icon.svg

An app folder may carry an `icon.svg` (that exact lowercase name) next to its entry page. Nothing registers it — the shell finds it by name and uses it in three places:

- the app's glyph in the sidebar's **Projects** list (a 14 px slot);
- the **browser-tab favicon** on the app's page (`/apps/<folder>`) and on any of its files opened in the explorer (16 px in most browsers);
- the **home-screen icon of the published app**, when a reader uses Add to Home Screen (~180 px, and masked — see below). Publishing copies this same file: it is the `apple-touch-icon` and the web manifest's icon both.

Skip the file and a generic mark is used — in the shell a generic glyph, on a published app a lettermark built from the folder name (`chinese-hsk-cards` → "CH" on a hue hashed from the name). Edit it and the new drawing shows on the next navigation, no reload of the shell.

The home screen is the one place the icon outlives your editing: what a reader installed is a copy taken at install time, and a re-publish does not reliably replace it. Settle the icon before you hand anyone the link.

## It renders as is

The svg is drawn **untouched**: no recolouring, no tint, no frame added. Whatever colours and background you draw are exactly what the user sees. Consequences:

- **Own your background.** The icon lands on the sidebar (light or dark theme), on a browser tab strip (light or dark, per the user's OS/browser), and on a phone home screen over the reader's wallpaper. A transparent icon must read on all of them — a black glyph vanishes on dark, a white one on light — and on the home screen transparency is not honoured at all: iOS fills the transparent parts of a touch icon, and an Android launcher fills them with a ground of its own choosing. Paint an opaque background across the whole viewBox so contrast is decided inside the file.
- Nothing is clipped or inset for you. Fill the viewBox.

## The home-screen icon is masked

Both platforms crop the file to their own shape — iOS to its squircle, Android to whatever the launcher uses (the published manifest declares the icon `purpose: "any maskable"`, which is what lets a launcher crop it instead of letterboxing it on a white card). Two rules follow:

- **Full bleed, and no rounded corners of your own.** A background drawn as `<rect rx="…">` gets rounded a second time by the mask, leaving wedges in the corners where your rounding and theirs disagree. Draw the background as a plain square filling the viewBox and let each platform round it. The generated fallback lettermark does exactly this, and is the shape to copy.
- **Keep the subject inside the middle ~80%.** A launcher may crop to a circle inscribed in the square, so anything near a corner is at risk. Background to the edges, glyph away from them — roughly a tenth of the viewBox clear on every side.

## Three sizes, one file

14 px in the sidebar, 16 px in a tab, ~180 px on a home screen. Only the last has room for an illustration, and the same file serves all three.

- **Design at 180, then check at 16.** An emoji-style drawing is fine at home-screen size — a few flat shapes with clean edges — provided its silhouette still says what it is when it is 16 px wide. Thin lines (under ~1/12 of the viewBox), fine texture, gradients and drop shadows look right on the phone and turn to mud in the tab. To check: zoom the browser out until the svg is ~16 px on screen, on a light and a dark page.
- **One subject, two or three colours.** No scenes, no text longer than two characters.
- **A white or light background is allowed** — it reads well on the home screen, where the mask supplies the edge — but it costs you the favicon, because a light tile on a light tab strip has no edge at all. Make the subject fill most of the tile so the silhouette is what identifies the app. Don't fix it with a hairline border: the mask crops the border off at the corners.
- **Square viewBox** (`viewBox="0 0 512 512"`, what the generated fallback uses) so it fills every slot without letterboxing. Set no fixed `width`/`height`, or set them equal.

## Text and emoji render in someone else's fonts

An svg carries no fonts, and this one is rasterized on the reader's device.

- **Never put an emoji character in `<text>`** to get an emoji-style icon. Emoji coverage differs per platform, and flags are the worst case: a flag emoji is a pair of regional-indicator letters, so on a platform with no flag glyphs it renders as those two letters — "CN" where you wanted a flag. Draw the shapes instead.
- **Latin letters in `<text>` are safe** with a font-family stack, which is what the generated lettermark relies on.
- **A CJK — or any non-Latin — character in `<text>` depends on the device having that face.** Phones do; a desktop browser may not, and the miss shows as tofu. Convert the character to a `<path>` if you want a 汉字 monogram.

## A serviceable default

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#1b1d21"/>
  <circle cx="256" cy="256" r="128" fill="#e5ff44"/>
</svg>
```

Full-bleed dark square, one bright glyph well inside the safe area — reads on any background, survives both masks, legible from 16 px to 180. Swap the glyph (a path, a monogram `<text>` at `font-size="232"` with `text-anchor="middle"` and `dominant-baseline="central"`, a simple mark) and the two colours for the app's own.

## Keep it a plain file

- Inline everything: no external `<image>`, fonts or CSS `@import` — the favicon is fetched as a standalone document, and publishing copies this one file and nothing beside it.
- No scripts, no animation (favicons do not animate, the sidebar ignores it, and a home-screen icon is a still).
- Small: a few KB. Optimise with `svgo` if exported from a design tool.
