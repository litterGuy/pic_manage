"""Inline SVG icons.

Emoji read as toy-like next to a dense instrument-panel layout, and they render
differently on every platform -- the same page is a different page on Windows,
Android and iOS.  These are 24x24 stroked paths that inherit ``currentColor``,
so an icon weighs exactly as much as the text beside it, scales with the font,
and costs no extra request.

Every entry is bare geometry; :func:`icon` adds the ``<svg>`` wrapper.  Paths are
written on a 24-unit grid with a 1.5 stroke, rounded caps and joins.
"""

from __future__ import annotations

from markupsafe import Markup

#: Bare geometry, drawn inside a 24x24 box.
_PATHS: dict[str, str] = {
    # -- media types -------------------------------------------------------
    "image": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<circle cx="8.6" cy="8.6" r="1.6"/>'
        '<path d="M20.6 15.4 16.3 11 3.6 20.4"/>'
    ),
    "video": (
        '<rect x="2.5" y="4" width="19" height="16" rx="2"/>'
        '<path d="M2.5 9h19M2.5 15h19M7.5 4v16M16.5 4v16"/>'
    ),
    "audio": '<path d="M4 10v4M8 6.5v11M12 3v18M16 7v10M20 10v4"/>',

    # -- navigation --------------------------------------------------------
    "search": '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m19.6 19.6-4.4-4.4"/>',
    "chevron-left": '<path d="M14.5 18.5 8 12l6.5-6.5"/>',
    "chevron-right": '<path d="M9.5 18.5 16 12l-6.5-6.5"/>',
    "arrow-left": '<path d="M19.5 12H5"/><path d="M11.5 18.5 5 12l6.5-6.5"/>',
    "download": '<path d="M12 3.5v11"/><path d="m7.5 10 4.5 4.5L16.5 10"/><path d="M4 19.5h16"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',

    # -- objects -----------------------------------------------------------
    "layers": (
        '<path d="m12 3.2 8.6 4.8L12 12.8 3.4 8z"/>'
        '<path d="m3.4 12.6 8.6 4.8 8.6-4.8"/>'
        '<path d="m3.4 16.9 8.6 4.8 8.6-4.8"/>'
    ),
    "folder": (
        '<path d="M3.2 7.2a2 2 0 0 1 2-2h3.9l2 2.4h7.7a2 2 0 0 1 2 2v8.2a2 2 0 0 1-2 2H5.2a2 2 0 0 1-2-2z"/>'
    ),
    "grid": (
        '<rect x="3.2" y="3.2" width="7.2" height="7.2" rx="1.2"/>'
        '<rect x="13.6" y="3.2" width="7.2" height="7.2" rx="1.2"/>'
        '<rect x="3.2" y="13.6" width="7.2" height="7.2" rx="1.2"/>'
        '<rect x="13.6" y="13.6" width="7.2" height="7.2" rx="1.2"/>'
    ),
    "duplicate": (
        '<rect x="8.4" y="8.4" width="12" height="12" rx="2"/>'
        '<path d="M16 8.4V6.4a2 2 0 0 0-2-2H6.4a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h2"/>'
    ),

    # -- state -------------------------------------------------------------
    "info": '<circle cx="12" cy="12" r="8.6"/><path d="M12 11v5.2"/><path d="M12 7.8h.01"/>',
    "alert": '<path d="M12 4.2 3.2 19.8h17.6z"/><path d="M12 10v4.2"/><path d="M12 17.2h.01"/>',
    "check": '<path d="m5.5 12.8 4.2 4.2 8.8-9.8"/>',
    "undo": '<path d="M3.6 12a8.4 8.4 0 1 0 2.8-6.3"/><path d="M3.4 4.2v4.6h4.6"/>',
    "sort": '<path d="M7 4.5v15M7 19.5l-3-3M7 19.5l3-3"/><path d="M17 19.5v-15M17 4.5l-3 3M17 4.5l3 3"/>',

    #: Filled on purpose: an outlined triangle disappears at 14px.
    "play": '<path d="M9.4 6.3 18 12l-8.6 5.7z" fill="currentColor" stroke="none"/>',
}

_SVG_ATTRS = (
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"'
)


def icon(name: str, *, cls: str = "") -> Markup:
    """The ``<svg>`` for ``name``, or an empty string when the name is unknown.

    Returning markup (rather than a path the caller must wrap) keeps every
    template call site a one-liner, and ``Markup`` marks it as already-safe so
    Jinja's autoescaping leaves the tags alone.
    """
    body = _PATHS.get(name)
    if body is None:
        return Markup("")
    classes = "icon" + (f" {cls}" if cls else "")
    return Markup(
        f'<svg class="{classes}" {_SVG_ATTRS} '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )


def glyph(media_type: str) -> Markup:
    """The icon for a media type.

    Separate name from :func:`icon` only because templates talk about "glyphs"
    when they mean "the picture/video/audio mark".
    """
    return icon(media_type)


__all__ = ["icon", "glyph"]
