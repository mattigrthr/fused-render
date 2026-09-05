"""The published app's icon — the author's ``icon.svg``, or one made from its name.

An app added to a phone's Home Screen is, from then on, whatever its icon looks
like. On iOS that matters twice over: the parent issue's open question is that
Safari's tracking prevention deletes script-writable storage after seven days
without interaction, and a **home-screen** app is exempt — so the icon is not
decoration, it is the thing that keeps a reader's progress alive.

An app with no ``icon.svg`` therefore cannot be allowed to block, and equally
cannot be allowed to ship no icon at all (the reader gets a screenshot of the
page, at whatever it happened to be showing). So a fallback is synthesized: the
app's initials on a flat ground, in a hue derived from its name. Not a logo —
just something stable, legible at 60px, and distinguishable from the next app the
same reader installs.

Same name, same icon, forever: the hue comes from a hash of the name, so a
re-publish never changes the icon under someone who has already installed it.
"""

from __future__ import annotations

import os
import re

def _hue(name: str) -> int:
    """A stable 0-359 hue for ``name``.

    Not :func:`hash` — that is salted per process, so the same app would get a
    different colour on every server restart, and an icon that changes when you
    re-publish is an icon the reader stops recognising.
    """
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % 360


def initials(name: str) -> str:
    """One or two letters for ``name``: ``chinese-hsk-cards`` -> ``CH``.

    Words split on the separators a folder name actually uses. Two initials from
    the first two words, or the first two characters of a single word — never
    three, which stops being legible at 60px on a phone.
    """
    words = [w for w in re.split(r"[-_\s.]+", name) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def fallback_svg(name: str) -> str:
    """A square lettermark for ``name``, as SVG source.

    ``viewBox="0 0 512 512"`` with a full-bleed background: a maskable icon on
    Android is cropped to whatever shape the launcher wants, and anything with
    transparent corners comes out as a white square with a picture floating in
    it. Text is centred with ``dominant-baseline`` rather than a nudged ``y`` so
    it stays centred whatever the renderer's font metrics are.
    """
    hue = _hue(name)
    text = initials(name)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        f'role="img" aria-label="{text}">'
        f'<rect width="512" height="512" fill="hsl({hue} 62% 38%)"/>'
        f'<text x="256" y="256" fill="#ffffff" font-size="232" font-weight="600" '
        'text-anchor="middle" dominant-baseline="central" '
        'font-family="ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif"'
        f">{text}</text></svg>"
    )


def resolve(app_dir: str, name: str) -> tuple[str, bool]:
    """``(svg source, is_the_author's)`` for the app at ``app_dir``.

    Reads the app's own ``icon.svg`` — the same file the sidebar and the app
    page's favicon already draw (``current_apps.ICON_NAME``), so a published app
    looks like the one the author has been using — and falls back to a lettermark
    when there is none or it cannot be read.
    """
    path = os.path.join(app_dir, "icon.svg")
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        if source.strip():
            return source, True
    except OSError:
        pass
    return fallback_svg(name), False
