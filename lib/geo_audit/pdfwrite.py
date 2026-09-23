"""A PDF 1.4 writer built directly from the file-format spec.

WHY THIS EXISTS
---------------
The marketplace ships as a self-contained skill directory. There is no
``pip install`` step and no network at run time, which rules out reportlab,
fpdf, weasyprint and every other PDF library. That constraint is not as
painful as it sounds: a PDF is not a mysterious binary blob, it is a mostly
ASCII object graph followed by a table of byte offsets, and the dozen drawing
operators an audit report actually needs (``re``, ``f``, ``S``, ``m``, ``l``,
``c``, ``Tj``) fit comfortably in one module.

The one genuine cost of writing it by hand is **font metrics**. Embedding a
font means parsing TrueType tables and building a CID map, so this module uses
only the base-14 fonts that every conforming reader already carries, and ships
the Adobe AFM advance widths for Helvetica and Helvetica-Bold so that text can
be measured — which is what makes word wrap, centred labels and auto-paginated
tables possible. Treating the fonts as monospace would misjudge line widths by
30% or more and produce ragged, overflowing output.

The second cost is **encoding**. The base-14 fonts are single-byte fonts; with
``/WinAnsiEncoding`` they reach Latin-1 and no further. A real audit reads live
web pages, so the text handed to this module routinely contains em-dashes,
curly quotes, arrows and occasionally CJK. Crashing on any of those would be a
bug in a report generator whose whole job is to survive the open web, so
:func:`_sanitize` folds everything outside the safe range down to an ASCII
equivalent before it is ever measured or emitted.

COORDINATE SYSTEM
-----------------
PDF's native origin is bottom-left with y increasing upward. Every public
method here instead takes **top-left origin, y increasing downward**, matching
SVG and matching how a document is read. The flip happens once, in
:meth:`PdfDocument._y`. This is deliberate: :mod:`charts` renders the same
geometry to SVG and to this canvas, and one shared convention removes an entire
class of mirror-image bugs.
"""

from __future__ import annotations

import math
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------

#: Page sizes in PostScript points (1/72 inch).
PAGE_SIZES: dict[str, tuple[float, float]] = {
    "letter": (612.0, 792.0),
    "a4": (595.28, 841.89),
    "legal": (612.0, 1008.0),
}


@dataclass
class Margins:
    """Page margins in points. Defaults are one inch on the long edges."""

    top: float = 64.0
    right: float = 56.0
    bottom: float = 58.0
    left: float = 56.0


# ---------------------------------------------------------------------------
# Base-14 font metrics
# ---------------------------------------------------------------------------

# Advance widths in 1/1000 em, from the Adobe Core-14 AFM files. Stored as
# (width, characters) groups rather than a flat dict so the table stays short
# enough to eyeball against the AFM source.
_HELVETICA_GROUPS = (
    (191, "'"),
    (222, "ijl"),
    (260, "|"),
    (278, " !,./:;Ift[\\]"),
    (333, "()-`r"),
    (334, "{}"),
    (355, '"'),
    (389, "*"),
    (469, "^"),
    (500, "Jcksvxyz"),
    (556, "#$0123456789?_abdeghnopquL"),
    (584, "+<=>~"),
    (611, "FTZ"),
    (667, "&ABEKPSVXY"),
    (722, "CDHNRUw"),
    (778, "GOQ"),
    (833, "Mm"),
    (889, "%"),
    (944, "W"),
    (1015, "@"),
)

_HELVETICA_BOLD_GROUPS = (
    (238, "'"),
    (278, " ,./ijlI\\"),
    (280, "|"),
    (333, "!()-:;`ft[]"),
    (389, "*r{}"),
    (474, '"'),
    (500, "z"),
    (556, "#$0123456789_aceksvxyJ"),
    (584, "+<=>^~"),
    (611, "?FLTZbdghnopqu"),
    (667, "EPSVXY"),
    (722, "ABCDHKNRU"),
    (778, "GOQw"),
    (833, "M"),
    (889, "%m"),
    (944, "W"),
    (975, "@"),
)


def _expand(groups: tuple[tuple[int, str], ...]) -> dict[str, int]:
    table: dict[str, int] = {}
    for width, chars in groups:
        for ch in chars:
            table[ch] = width
    return table


HELVETICA_WIDTHS = _expand(_HELVETICA_GROUPS)
HELVETICA_BOLD_WIDTHS = _expand(_HELVETICA_BOLD_GROUPS)

# Fallback advance for a character absent from the table. Chosen as the width
# of a lowercase 'o', which is close to the corpus-weighted mean for both
# faces, so long runs of accented text measure within a few percent.
_DEFAULT_WIDTH = {"helvetica": 556, "helvetica-bold": 611}

#: Logical font name -> (PDF resource name, BaseFont, width table key).
FONTS: dict[str, tuple[str, str, str]] = {
    "helvetica": ("F1", "Helvetica", "helvetica"),
    "bold": ("F2", "Helvetica-Bold", "helvetica-bold"),
    "oblique": ("F3", "Helvetica-Oblique", "helvetica"),
    "mono": ("F4", "Courier", "courier"),
}
_FONT_ALIASES = {
    "regular": "helvetica",
    "normal": "helvetica",
    "helvetica-bold": "bold",
    "helvetica-oblique": "oblique",
    "italic": "oblique",
    "courier": "mono",
    "code": "mono",
}

COURIER_WIDTH = 600  # Courier is monospace; no table needed.


def resolve_font(name: str) -> str:
    """Normalise a caller-supplied font name to a key of :data:`FONTS`."""
    key = (name or "helvetica").strip().lower()
    key = _FONT_ALIASES.get(key, key)
    if key not in FONTS:
        raise ValueError(f"unknown font {name!r}; use one of {sorted(FONTS)}")
    return key


def char_width(ch: str, font: str = "helvetica") -> int:
    """Advance width of one character in 1/1000 em."""
    key = resolve_font(font)
    table_key = FONTS[key][2]
    if table_key == "courier":
        return COURIER_WIDTH
    table = HELVETICA_BOLD_WIDTHS if table_key == "helvetica-bold" else HELVETICA_WIDTHS
    if ch in table:
        return table[ch]
    # Accented Latin-1: an 'é' advances like an 'e'. Decomposing is more
    # accurate than a flat default and costs nothing, since the AFM widths for
    # composed glyphs really are the base-letter widths in these faces.
    base = unicodedata.normalize("NFD", ch)
    if base and base[0] in table:
        return table[base[0]]
    return _DEFAULT_WIDTH[table_key]


def text_width(s: str, size: float, font: str = "helvetica") -> float:
    """Width of *s* in points at *size*, measured with the AFM tables."""
    s = _sanitize(s)
    key = resolve_font(font)
    table_key = FONTS[key][2]
    if table_key == "courier":
        return len(s) * COURIER_WIDTH * size / 1000.0
    table = HELVETICA_BOLD_WIDTHS if table_key == "helvetica-bold" else HELVETICA_WIDTHS
    default = _DEFAULT_WIDTH[table_key]
    total = 0
    for ch in s:
        width = table.get(ch)
        if width is None:
            base = unicodedata.normalize("NFD", ch)
            width = table.get(base[0], default) if base else default
        total += width
    return total * size / 1000.0


def _break_token(word: str, width: float, size: float, font: str) -> list[str]:
    """Split one unbreakable token into pieces that each fit *width*.

    Needed because audit reports are full of long URLs and hashed asset names,
    and a token wider than its column would otherwise run into the margin. At
    least one character is always taken, so this terminates even when a single
    glyph is wider than the column.
    """
    pieces: list[str] = []
    chunk = ""
    for ch in word:
        if chunk and text_width(chunk + ch, size, font) > width:
            pieces.append(chunk)
            chunk = ch
        else:
            chunk += ch
    if chunk:
        pieces.append(chunk)
    return pieces


# ---------------------------------------------------------------------------
# Text sanitising
# ---------------------------------------------------------------------------

# Characters that appear constantly in real web copy and have an unambiguous
# ASCII reading. Folding them is lossy but legible; leaving them would either
# fail the Latin-1 encode or print the wrong glyph under WinAnsiEncoding.
#
# Characters that are *already* Latin-1 -- the multiplication sign, +/-, the
# guillemets, the middle dot -- are deliberately absent. WinAnsiEncoding renders
# them correctly, so folding them would cost fidelity for no gain.
#
# Written with \\u escapes rather than literal glyphs on purpose: several of the
# entries below are visually identical in a source listing, and a duplicated
# dict key would silently drop one of them.
_TRANSLATIONS: dict[str, str] = {
    # dashes and hyphens
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "­": "",           # soft hyphen: invisible, safe to drop entirely
    # quotes
    "‘": "'", "’": "'", "‚": ",", "‛": "'",
    "“": '"', "”": '"', "„": ",,", "‟": '"',
    "′": "'", "″": '"', "‹": "<", "›": ">",
    # punctuation and list markers
    "…": "...", "•": "-", "‣": "-", "●": "-",
    "▪": "-", "■": "-", "⁃": "-", "‧": "-",
    "⁄": "/", "∕": "/",
    # arrows: an audit report is full of "observed -> expected"
    "←": "<-", "→": "->", "↔": "<->",
    "↑": "^", "↓": "v", "↵": "<-", "➤": "->",
    "⇐": "<=", "⇒": "=>", "⇔": "<=>",
    # maths and symbols
    "≈": "~", "≠": "!=", "≤": "<=", "≥": ">=",
    "∗": "*", "∞": "inf", "∑": "sum", "√": "sqrt",
    "Δ": "delta", "μ": "u", "‰": "o/oo",
    "™": "(TM)", "℗": "(P)",
    "✓": "yes", "✔": "yes", "✗": "no", "✘": "no",
    "★": "*", "☆": "*",
    # whitespace control
    "\t": " ", "\r": "", "\v": " ", "\f": " ",
    " ": " ",          # nbsp -> space, so word wrap has something to break on
}

# Unicode's fixed-width spaces, and its zero-width / variation-selector marks.
for _code in list(range(0x2000, 0x200B)) + [0x1680, 0x202F, 0x205F, 0x3000]:
    _TRANSLATIONS.setdefault(chr(_code), " ")
for _code in list(range(0x200B, 0x2010)) + list(range(0xFE00, 0xFE10)) + [0xFEFF]:
    _TRANSLATIONS.setdefault(chr(_code), "")
del _code

#: Stands in for any character with no Latin-1 or ASCII reading at all.
UNMAPPABLE = "?"


def _sanitize(s: str) -> str:
    """Fold *s* into the WinAnsi-safe subset this writer can actually emit.

    Three passes, cheapest first: an explicit table for the punctuation that
    turns up in every scraped page, then NFKD decomposition for accented forms
    the table missed, then a single ``?`` for anything left (CJK, emoji,
    Devanagari...).

    Runs of unmappable characters collapse to one ``?`` on purpose. A Chinese
    page title rendered as ``????????`` reads as file corruption; rendered as
    ``?`` it reads as one token this writer could not represent, which is the
    honest statement.
    """
    if not s:
        return ""
    out: list[str] = []
    previous_unmappable = False
    for ch in s:
        replacement = _TRANSLATIONS.get(ch)
        if replacement is None:
            code = ord(ch)
            if ch == "\n" or (32 <= code < 127):
                replacement = ch
            elif 160 <= code <= 255:
                replacement = ch  # printable Latin-1, identical under WinAnsi
            elif code < 160:
                # C0/C1 control range. WinAnsiEncoding puts typographic glyphs
                # at 0x80-0x9F, so passing these through would print garbage.
                replacement = ""
            else:
                decomposed = unicodedata.normalize("NFKD", ch)
                stripped = "".join(
                    c for c in decomposed if not unicodedata.combining(c)
                )
                if stripped and all(ord(c) < 256 for c in stripped):
                    replacement = stripped
                else:
                    replacement = UNMAPPABLE
        if replacement == UNMAPPABLE:
            if previous_unmappable:
                continue
            previous_unmappable = True
        else:
            previous_unmappable = False
        out.append(replacement)
    return "".join(out)


def _pdf_string(s: str) -> bytes:
    """Encode *s* as a PDF literal string, escaping ``(``, ``)`` and ``\\``.

    Non-printable and high bytes go out as three-digit octal escapes, which
    keeps the whole file 7-bit ASCII apart from compressed streams and makes it
    readable in a text editor when something goes wrong.
    """
    raw = _sanitize(s).encode("latin-1", errors="replace")
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):       # ( ) backslash
            out += b"\\" + bytes([byte])
        elif 32 <= byte <= 126:
            out.append(byte)
        else:
            out += b"\\%03o" % byte
    out += b")"
    return bytes(out)


# ---------------------------------------------------------------------------
# Colour and number formatting
# ---------------------------------------------------------------------------

#: Anything the colour helpers accept: ``'#RRGGBB'``, ``'#RGB'``, a 0..1
#: triple, or a 0..255 triple. Spelled as a string alias so the module keeps
#: importing on Pythons older than 3.10, where ``X | Y`` is not a runtime
#: expression.
ColorLike = "Union[str, Tuple[float, float, float]]"


def rgb(color) -> tuple[float, float, float]:
    """Normalise ``'#RRGGBB'``, ``'#RGB'``, a 0..1 triple or a 0..255 triple."""
    if color is None:
        raise ValueError("colour is None")
    if isinstance(color, str):
        text = color.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            raise ValueError(f"bad hex colour {color!r}")
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    values = tuple(float(v) for v in color)
    if len(values) != 3:
        raise ValueError(f"bad colour {color!r}")
    if any(v > 1.0 for v in values):
        values = tuple(v / 255.0 for v in values)  # type: ignore[assignment]
    return tuple(min(1.0, max(0.0, v)) for v in values)  # type: ignore[return-value]


def to_hex(color) -> str:
    """Render any accepted colour form as ``#rrggbb`` (for the SVG backend)."""
    r, g, b = rgb(color)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def _n(value: float) -> str:
    """Format a number for a content stream: short, never in scientific form."""
    if value == 0 or abs(value) < 5e-5:
        return "0"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


# ---------------------------------------------------------------------------
# Arc geometry, shared with charts.py
# ---------------------------------------------------------------------------

def arc_bezier(
    cx: float, cy: float, radius: float, start: float, end: float,
) -> list[tuple]:
    """Approximate an arc with cubic Béziers.

    Angles are in radians, parametrised as ``(cx + r cos t, cy + r sin t)``.
    Because this module's y axis points *down*, increasing ``t`` sweeps
    clockwise on screen, so a donut drawn from ``-pi/2`` upward starts at 12
    o'clock and runs clockwise, which is the conventional reading order.

    Returns segments in the form accepted by :meth:`PdfDocument.path`: a
    leading ``("m", x, y)`` followed by ``("c", x1, y1, x2, y2, x3, y3)``.
    Arcs are split so no single Bézier spans more than 90 degrees, where the
    approximation error stays under about one part in 10,000 of the radius.
    """
    segments: list[tuple] = []
    sweep = end - start
    if radius <= 0:
        return segments
    steps = max(1, int(math.ceil(abs(sweep) / (math.pi / 2) - 1e-9)))
    delta = sweep / steps
    kappa = (4.0 / 3.0) * math.tan(delta / 4.0)
    t = start
    segments.append(("m", cx + radius * math.cos(t), cy + radius * math.sin(t)))
    for _ in range(steps):
        t1 = t + delta
        x0, y0 = cx + radius * math.cos(t), cy + radius * math.sin(t)
        x3, y3 = cx + radius * math.cos(t1), cy + radius * math.sin(t1)
        x1 = x0 - kappa * radius * math.sin(t)
        y1 = y0 + kappa * radius * math.cos(t)
        x2 = x3 + kappa * radius * math.sin(t1)
        y2 = y3 - kappa * radius * math.cos(t1)
        segments.append(("c", x1, y1, x2, y2, x3, y3))
        t = t1
    return segments


_KAPPA = 0.5522847498307936  # 4/3 * (sqrt(2) - 1): circle-from-4-Béziers constant


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

@dataclass
class _Page:
    width: float
    height: float
    ops: list[str] = field(default_factory=list)


#: Point sizes for ``heading(level, ...)``. Level 1 is the report title.
HEADING_SIZES = {1: 19.0, 2: 14.0, 3: 11.0, 4: 9.5}
HEADING_SPACE_BEFORE = {1: 0.0, 2: 15.0, 3: 11.0, 4: 8.0}
HEADING_SPACE_AFTER = {1: 10.0, 2: 6.0, 3: 4.0, 4: 3.0}


class PdfDocument:
    """A paginated PDF canvas with a simple top-down flow layout on top.

    Two APIs share one page: absolute drawing (:meth:`text`, :meth:`rect`,
    :meth:`path` ...) in top-left coordinates, and a flowing layout
    (:meth:`heading`, :meth:`paragraph`, :meth:`table` ...) that tracks a
    cursor and starts a new page when it runs past the bottom margin. Charts
    bridge the two through :meth:`reserve`, which paginates and then hands back
    an absolute rectangle to draw into.
    """

    def __init__(
        self,
        page_size: str = "letter",
        margins: Margins | None = None,
        *,
        body_size: float = 9.6,
        leading: float = 1.38,
        title: str = "",
        author: str = "",
        subject: str = "",
        footer_text: str = "",
        footer_right: str = "Page {page} of {pages}",
        compress: bool = True,
    ) -> None:
        key = page_size.strip().lower()
        if key not in PAGE_SIZES:
            raise ValueError(f"unknown page size {page_size!r}; try {sorted(PAGE_SIZES)}")
        self.page_size_name = key
        self.width, self.height = PAGE_SIZES[key]
        self.margins = margins or Margins()
        self.body_size = body_size
        self.leading = leading
        self.title = title
        self.author = author
        self.subject = subject
        self.footer_text = footer_text
        self.footer_right = footer_right
        self.compress = compress

        self.ink = "#1a1a1a"
        self.muted = "#6b6b6b"
        self.rule = "#d0d0cc"

        self._pages: list[_Page] = []
        self._current: _Page | None = None
        self._sink: list[str] | None = None
        self.y = self.margins.top

    # -- geometry ---------------------------------------------------------

    @property
    def content_left(self) -> float:
        return self.margins.left

    @property
    def content_right(self) -> float:
        return self.width - self.margins.right

    @property
    def content_width(self) -> float:
        return self.width - self.margins.left - self.margins.right

    @property
    def content_bottom(self) -> float:
        return self.height - self.margins.bottom

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def _y(self, y: float) -> float:
        """Flip a top-down y into PDF's bottom-up page space."""
        return self.height - y

    # -- pages ------------------------------------------------------------

    def add_page(self) -> None:
        page = _Page(self.width, self.height)
        self._pages.append(page)
        self._current = page
        self._sink = page.ops
        self.y = self.margins.top

    def _ensure_page(self) -> _Page:
        if self._current is None:
            self.add_page()
        assert self._current is not None
        return self._current

    def _emit(self, op: str) -> None:
        self._ensure_page()
        assert self._sink is not None
        self._sink.append(op)

    # -- primitives -------------------------------------------------------

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float = 0.0,
        font: str = "helvetica",
        color=None,
        align: str = "left",
    ) -> float:
        """Draw one line of text with its **baseline** at *y*.

        Returns the advance width, so callers can chain runs. *align* shifts
        *x* to be the centre or right edge of the measured run.
        """
        size = size or self.body_size
        s = _sanitize(s).replace("\n", " ")
        if not s:
            return 0.0
        key = resolve_font(font)
        width = text_width(s, size, key)
        if align == "center":
            x -= width / 2.0
        elif align == "right":
            x -= width
        r, g, b = rgb(color if color is not None else self.ink)
        resource = FONTS[key][0]
        self._emit(
            f"BT {_n(r)} {_n(g)} {_n(b)} rg /{resource} {_n(size)} Tf "
            f"{_n(x)} {_n(self._y(y))} Td "
            + _pdf_string(s).decode("latin-1")
            + " Tj ET"
        )
        return width

    def text_width(self, s: str, size: float = 0.0, font: str = "helvetica") -> float:
        """Measured width of *s*, exposed so chart code can size labels."""
        return text_width(s, size or self.body_size, font)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill=None,
        stroke=None,
        line_width: float = 0.7,
    ) -> None:
        """Rectangle whose *top-left* corner is at (x, y)."""
        if w <= 0 or h <= 0:
            return
        painter = self._paint_op(fill, stroke)
        if painter is None:
            return
        ops = ["q"]
        ops.extend(self._colour_ops(fill, stroke, line_width))
        ops.append(f"{_n(x)} {_n(self._y(y + h))} {_n(w)} {_n(h)} re {painter}")
        ops.append("Q")
        self._emit(" ".join(ops))

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color=None,
        line_width: float = 0.7,
        dash: tuple[float, float] | None = None,
    ) -> None:
        r, g, b = rgb(color if color is not None else self.rule)
        dash_op = f"[{_n(dash[0])} {_n(dash[1])}] 0 d " if dash else ""
        self._emit(
            f"q {_n(r)} {_n(g)} {_n(b)} RG {_n(line_width)} w {dash_op}"
            f"{_n(x1)} {_n(self._y(y1))} m {_n(x2)} {_n(self._y(y2))} l S Q"
        )

    def path(
        self,
        segments,
        fill=None,
        stroke=None,
        line_width: float = 0.7,
        close: bool = True,
    ) -> None:
        """Draw a path from ``("m"|"l"|"c", ...)`` segments in top-down coords.

        This is the primitive donut arcs and every other curved shape go
        through; :func:`arc_bezier` produces segments in exactly this form.
        """
        painter = self._paint_op(fill, stroke)
        if painter is None or not segments:
            return
        ops = ["q"]
        ops.extend(self._colour_ops(fill, stroke, line_width))
        for segment in segments:
            kind = segment[0]
            if kind == "m":
                ops.append(f"{_n(segment[1])} {_n(self._y(segment[2]))} m")
            elif kind == "l":
                ops.append(f"{_n(segment[1])} {_n(self._y(segment[2]))} l")
            elif kind == "c":
                ops.append(
                    f"{_n(segment[1])} {_n(self._y(segment[2]))} "
                    f"{_n(segment[3])} {_n(self._y(segment[4]))} "
                    f"{_n(segment[5])} {_n(self._y(segment[6]))} c"
                )
            elif kind == "h":
                ops.append("h")
            else:
                raise ValueError(f"unknown path segment {kind!r}")
        if close:
            ops.append("h")
        ops.append(painter)
        ops.append("Q")
        self._emit(" ".join(ops))

    def polygon(self, points, fill=None, stroke=None, line_width: float = 0.7) -> None:
        """Closed polygon through *points*, a sequence of (x, y) pairs."""
        points = list(points)
        if len(points) < 2:
            return
        segments = [("m", points[0][0], points[0][1])]
        segments += [("l", px, py) for px, py in points[1:]]
        self.path(segments, fill=fill, stroke=stroke, line_width=line_width, close=True)

    def circle(
        self,
        cx: float,
        cy: float,
        radius: float,
        fill=None,
        stroke=None,
        line_width: float = 0.7,
    ) -> None:
        """Circle drawn as the classic four-Bézier approximation."""
        if radius <= 0:
            return
        k = _KAPPA * radius
        segments = [
            ("m", cx, cy - radius),
            ("c", cx + k, cy - radius, cx + radius, cy - k, cx + radius, cy),
            ("c", cx + radius, cy + k, cx + k, cy + radius, cx, cy + radius),
            ("c", cx - k, cy + radius, cx - radius, cy + k, cx - radius, cy),
            ("c", cx - radius, cy - k, cx - k, cy - radius, cx, cy - radius),
        ]
        self.path(segments, fill=fill, stroke=stroke, line_width=line_width, close=True)

    def arc(
        self,
        cx: float,
        cy: float,
        radius: float,
        start: float,
        end: float,
        color=None,
        line_width: float = 1.0,
    ) -> None:
        """Stroke an open arc between two angles (radians)."""
        self.path(
            arc_bezier(cx, cy, radius, start, end),
            stroke=color if color is not None else self.ink,
            line_width=line_width,
            close=False,
        )

    def _paint_op(self, fill, stroke) -> str | None:
        if fill is not None and stroke is not None:
            return "B"
        if fill is not None:
            return "f"
        if stroke is not None:
            return "S"
        return None

    def _colour_ops(self, fill, stroke, line_width: float) -> list[str]:
        ops: list[str] = []
        if fill is not None:
            r, g, b = rgb(fill)
            ops.append(f"{_n(r)} {_n(g)} {_n(b)} rg")
        if stroke is not None:
            r, g, b = rgb(stroke)
            ops.append(f"{_n(r)} {_n(g)} {_n(b)} RG {_n(line_width)} w")
        return ops

    # -- word wrap --------------------------------------------------------

    def wrap(self, s: str, width: float, size: float = 0.0, font: str = "helvetica") -> list[str]:
        """Break *s* into lines that fit *width* points.

        Greedy first-fit on whitespace. A single token wider than the column
        (a long URL, which an audit report is full of) is hard-broken by
        character rather than allowed to bleed into the margin.
        """
        size = size or self.body_size
        s = _sanitize(s)
        if width <= 0:
            return [s]
        lines: list[str] = []
        for hard_line in s.split("\n"):
            words = hard_line.split()
            if not words:
                lines.append("")
                continue
            current = ""
            for word in words:
                # Split an oversized token first, so the fitting loop below
                # only ever sees pieces that can stand on a line alone.
                pieces = (
                    _break_token(word, width, size, font)
                    if text_width(word, size, font) > width
                    else [word]
                )
                for piece in pieces:
                    candidate = f"{current} {piece}" if current else piece
                    if not current or text_width(candidate, size, font) <= width:
                        current = candidate
                    else:
                        lines.append(current)
                        current = piece
            if current:
                lines.append(current)
        return lines or [""]

    # -- flow layout ------------------------------------------------------

    def reserve(self, height: float, *, top_gap: float = 0.0) -> float:
        """Paginate if needed, then claim *height* points of vertical space.

        Returns the absolute top-left y of the claimed block and advances the
        cursor past it. This is how a chart, which draws absolutely, takes part
        in the flow.
        """
        self._ensure_page()
        self.y += top_gap
        if self.y + height > self.content_bottom and height <= self._usable_height():
            self.add_page()
        top = self.y
        self.y += height
        return top

    def _usable_height(self) -> float:
        return self.content_bottom - self.margins.top

    def _ensure_space(self, height: float) -> None:
        self._ensure_page()
        if self.y + height > self.content_bottom:
            self.add_page()

    def spacer(self, height: float = 8.0) -> None:
        """Blank vertical space. Never carried across a page break."""
        self._ensure_page()
        if self.y + height <= self.content_bottom:
            self.y += height

    def heading(self, level: int, text: str, color=None, rule: bool | None = None) -> None:
        """A flowing heading. Levels 1-4 map to :data:`HEADING_SIZES`."""
        level = max(1, min(4, int(level)))
        size = HEADING_SIZES[level]
        font = "bold" if level <= 3 else "helvetica"
        if rule is None:
            rule = level <= 2
        line_height = size * 1.22
        lines = self.wrap(text, self.content_width, size, font)
        needed = HEADING_SPACE_BEFORE[level] + len(lines) * line_height
        needed += HEADING_SPACE_AFTER[level] + (6.0 if rule else 0.0)
        # Keep a heading with at least two lines of what follows it.
        self._ensure_space(min(needed + 2 * self.body_size * self.leading, self._usable_height()))
        if self.y > self.margins.top:
            self.y += HEADING_SPACE_BEFORE[level]
        for line in lines:
            self.y += size * 0.95
            self.text(self.content_left, self.y, line, size, font, color or self.ink)
            self.y += line_height - size * 0.95
        if rule:
            self.y += 3.0
            self.line(
                self.content_left, self.y, self.content_right, self.y,
                self.rule, 0.9 if level == 1 else 0.6,
            )
        self.y += HEADING_SPACE_AFTER[level]

    def paragraph(
        self,
        text: str,
        size: float = 0.0,
        font: str = "helvetica",
        color=None,
        indent: float = 0.0,
        space_after: float = 5.0,
    ) -> None:
        """A wrapped, auto-paginated block of body text."""
        size = size or self.body_size
        step = size * self.leading
        width = self.content_width - indent
        for line in self.wrap(text, width, size, font):
            self._ensure_space(step)
            self.y += step
            if line:
                self.text(self.content_left + indent, self.y, line, size, font, color)
        self.y += space_after

    def bullet(
        self,
        text: str,
        size: float = 0.0,
        font: str = "helvetica",
        color=None,
        marker: str = "-",
        indent: float = 12.0,
        space_after: float = 2.5,
    ) -> None:
        """One bullet item; continuation lines hang under the first."""
        size = size or self.body_size
        step = size * self.leading
        marker = _sanitize(marker) or "-"
        gutter = max(8.0, text_width(marker, size, font) + 5.0)
        width = self.content_width - indent - gutter
        for index, line in enumerate(self.wrap(text, width, size, font)):
            self._ensure_space(step)
            self.y += step
            if index == 0:
                self.text(self.content_left + indent, self.y, marker, size, font, color or self.muted)
            self.text(self.content_left + indent + gutter, self.y, line, size, font, color)
        self.y += space_after

    def key_value(self, label: str, value: str, label_width: float = 118.0, size: float = 0.0) -> None:
        """A label/value row, used for the scope and metadata blocks."""
        size = size or self.body_size
        step = size * self.leading
        lines = self.wrap(value, self.content_width - label_width, size, "helvetica")
        self._ensure_space(step * len(lines))
        for index, line in enumerate(lines):
            self.y += step
            if index == 0:
                self.text(self.content_left, self.y, label, size, "bold", self.muted)
            self.text(self.content_left + label_width, self.y, line, size, "helvetica", self.ink)
        self.y += 1.5

    def table(
        self,
        headers,
        rows,
        col_widths=None,
        *,
        size: float = 0.0,
        header_fill="#eceae5",
        zebra="#f7f6f3",
        border="#d5d3cd",
        cell_padding: float = 4.0,
        align=None,
    ) -> None:
        """A wrapped, auto-paginated table. The header repeats on every page.

        *col_widths* may be absolute points or fractions of the content width;
        values that are all <= 1.0 are read as fractions. Omit it for equal
        columns. *align* is an optional per-column list of
        ``"left" | "center" | "right"``.
        """
        headers = [_sanitize(str(h)) for h in (headers or [])]
        rows = [[_sanitize(str(cell)) for cell in row] for row in (rows or [])]
        columns = max([len(headers)] + [len(row) for row in rows])
        if columns == 0:
            return
        size = size or self.body_size - 0.6
        widths = self._column_widths(columns, col_widths)
        align = list(align or []) + ["left"] * columns
        step = size * 1.32
        header_height = step + 2 * cell_padding

        def draw_header(y: float) -> float:
            self.rect(self.content_left, y, sum(widths), header_height, fill=header_fill)
            x = self.content_left
            for index in range(columns):
                label = headers[index] if index < len(headers) else ""
                self._cell_text(x, y, widths[index], label, size, "bold", self.ink,
                                cell_padding, step, align[index])
                x += widths[index]
            return y + header_height

        self._ensure_space(header_height + step + 2 * cell_padding)
        top = self.y
        self.y = draw_header(top)
        table_top = top
        stripe = 0
        for row in rows:
            wrapped = [
                self.wrap(row[i] if i < len(row) else "", widths[i] - 2 * cell_padding, size)
                for i in range(columns)
            ]
            row_height = max(len(cell) for cell in wrapped) * step + 2 * cell_padding
            if self.y + row_height > self.content_bottom:
                self._table_frame(table_top, self.y, widths, border)
                self.add_page()
                table_top = self.y
                self.y = draw_header(self.y)
            if stripe % 2 == 1:
                self.rect(self.content_left, self.y, sum(widths), row_height, fill=zebra)
            x = self.content_left
            for index in range(columns):
                self._cell_lines(x, self.y, widths[index], wrapped[index], size,
                                 cell_padding, step, align[index])
                x += widths[index]
            self.y += row_height
            stripe += 1
        self._table_frame(table_top, self.y, widths, border)
        self.y += 7.0

    def _column_widths(self, columns: int, col_widths) -> list[float]:
        available = self.content_width
        if not col_widths:
            return [available / columns] * columns
        values = [float(v) for v in col_widths][:columns]
        values += [0.0] * (columns - len(values))
        if values and all(v <= 1.0 for v in values) and sum(values) > 0:
            total = sum(values)
            return [available * v / total for v in values]
        total = sum(values)
        if total <= 0:
            return [available / columns] * columns
        # Scale absolute widths to fit rather than overflowing the margin.
        scale = available / total if total > available else 1.0
        return [v * scale for v in values]

    def _cell_text(self, x, y, width, text, size, font, color, padding, step, align) -> None:
        lines = self.wrap(text, width - 2 * padding, size, font)
        self._cell_lines(x, y, width, lines, size, padding, step, align, font, color)

    def _cell_lines(self, x, y, width, lines, size, padding, step, align,
                    font: str = "helvetica", color=None) -> None:
        if align == "right":
            anchor, mode = x + width - padding, "right"
        elif align == "center":
            anchor, mode = x + width / 2.0, "center"
        else:
            anchor, mode = x + padding, "left"
        cursor = y + padding
        for line in lines:
            cursor += step
            if line:
                self.text(anchor, cursor - step * 0.28, line, size, font, color, mode)

    def _table_frame(self, top: float, bottom: float, widths, border) -> None:
        if bottom <= top:
            return
        total = sum(widths)
        self.rect(self.content_left, top, total, bottom - top, stroke=border, line_width=0.6)
        x = self.content_left
        for width in widths[:-1]:
            x += width
            self.line(x, top, x, bottom, border, 0.6)

    # -- footers ----------------------------------------------------------

    def _build_footer(self, page: _Page, number: int, total: int) -> list[str]:
        """Footer ops for one page, produced fresh so ``save()`` is repeatable."""
        if not self.footer_text and not self.footer_right:
            return []
        sink: list[str] = []
        previous_page, previous_sink = self._current, self._sink
        self._current, self._sink = page, sink
        try:
            y = self.height - self.margins.bottom * 0.52
            self.line(self.margins.left, y - 9.0, self.width - self.margins.right, y - 9.0,
                      self.rule, 0.5)
            if self.footer_text:
                self.text(self.margins.left, y, self.footer_text, 7.4, "helvetica", self.muted)
            if self.footer_right:
                label = self.footer_right.format(page=number, pages=total)
                self.text(self.width - self.margins.right, y, label, 7.4, "helvetica",
                          self.muted, align="right")
        finally:
            self._current, self._sink = previous_page, previous_sink
        return sink

    # -- serialisation ----------------------------------------------------

    def _stream_object(self, payload: bytes) -> bytes:
        if self.compress:
            body = zlib.compress(payload, 9)
            header = f"<< /Length {len(body)} /Filter /FlateDecode >>\n".encode("latin-1")
        else:
            body = payload
            header = f"<< /Length {len(body)} >>\n".encode("latin-1")
        return header + b"stream\n" + body + b"\nendstream"

    def to_bytes(self) -> bytes:
        """Serialise the whole document, including the xref table."""
        if not self._pages:
            self.add_page()
        total = len(self._pages)

        # Object numbers are allocated up front so forward references
        # (Pages -> Kids, Page -> Contents) can be written in one pass.
        catalog_id = 1
        pages_id = 2
        font_ids = {key: 3 + index for index, key in enumerate(FONTS)}
        info_id = 3 + len(FONTS)
        first_page_id = info_id + 1

        objects: dict[int, bytes] = {}

        resources = " ".join(
            f"/{FONTS[key][0]} {font_ids[key]} 0 R" for key in FONTS
        )
        kids: list[str] = []
        for index, page in enumerate(self._pages):
            page_id = first_page_id + index * 2
            content_id = page_id + 1
            kids.append(f"{page_id} 0 R")
            objects[page_id] = (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {_n(page.width)} {_n(page.height)}] "
                f"/Resources << /Font << {resources} >> "
                f"/ProcSet [/PDF /Text] >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("latin-1")
            ops = list(page.ops) + self._build_footer(page, index + 1, total)
            payload = ("\n".join(ops) + "\n").encode("latin-1")
            objects[content_id] = self._stream_object(payload)

        objects[catalog_id] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
        objects[pages_id] = (
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {total} >>"
        ).encode("latin-1")
        for key, number in font_ids.items():
            objects[number] = (
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{FONTS[key][1]} "
                f"/Encoding /WinAnsiEncoding >>"
            ).encode("latin-1")

        stamp = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")
        info_parts = [f"/Producer {_pdf_string('geo_audit.pdfwrite').decode('latin-1')}"]
        if self.title:
            info_parts.append(f"/Title {_pdf_string(self.title).decode('latin-1')}")
        if self.author:
            info_parts.append(f"/Author {_pdf_string(self.author).decode('latin-1')}")
        if self.subject:
            info_parts.append(f"/Subject {_pdf_string(self.subject).decode('latin-1')}")
        info_parts.append(f"/CreationDate ({stamp})")
        objects[info_id] = ("<< " + " ".join(info_parts) + " >>").encode("latin-1")

        # %PDF-1.4 then a comment line with high bytes, which tells transfer
        # tools the file is binary and must not be newline-translated.
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        highest = max(objects)
        offsets: dict[int, int] = {}
        for number in range(1, highest + 1):
            body = objects.get(number)
            if body is None:
                continue
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode("latin-1")
            out += body
            out += b"\nendobj\n"

        xref_offset = len(out)
        size = highest + 1
        out += f"xref\n0 {size}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            # Every entry is exactly 20 bytes; readers index into this table
            # arithmetically, so the padding is load-bearing, not cosmetic.
            out += ("%010d %05d n \n" % (offsets.get(number, 0), 0)).encode("latin-1")
        out += (
            f"trailer\n<< /Size {size} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("latin-1")
        return bytes(out)

    def save(self, path: str) -> str:
        """Write the document to *path* and return the path."""
        data = self.to_bytes()
        with open(path, "wb") as handle:
            handle.write(data)
        return path


# ---------------------------------------------------------------------------
# Self-validation
# ---------------------------------------------------------------------------

def verify_xref(data: bytes) -> list[str]:
    """Re-parse a produced PDF and check every xref offset points at its object.

    Written because a wrong byte offset is the single most likely defect in a
    hand-rolled writer, and it is invisible in a hex dump but fatal in a
    reader. Returns a list of problems; empty means the table is consistent.
    """
    problems: list[str] = []
    if not data.startswith(b"%PDF-"):
        problems.append("file does not start with %PDF-")
    if not data.rstrip().endswith(b"%%EOF"):
        problems.append("file does not end with %%EOF")

    marker = data.rfind(b"startxref")
    if marker == -1:
        problems.append("no startxref")
        return problems
    try:
        tail = data[marker + len(b"startxref"):].split(b"%%EOF")[0].strip()
        xref_offset = int(tail.split()[0])
    except (ValueError, IndexError):
        problems.append("startxref is not an integer")
        return problems
    if not (0 <= xref_offset < len(data)):
        problems.append(f"startxref {xref_offset} is outside the file")
        return problems
    if not data[xref_offset:].startswith(b"xref"):
        problems.append(f"startxref {xref_offset} does not point at 'xref'")
        return problems

    # Skip the end-of-line that follows the "xref" keyword before reading the
    # "<first> <count>" subsection header.
    cursor = xref_offset + len(b"xref")
    while cursor < len(data) and data[cursor:cursor + 1] in (b"\r", b"\n", b" "):
        cursor += 1
    header_end = data.index(b"\n", cursor)
    try:
        first, count = (int(v) for v in data[cursor:header_end].split())
    except ValueError:
        problems.append(
            f"malformed xref subsection header {data[cursor:header_end]!r}"
        )
        return problems

    entries_start = header_end + 1
    for index in range(count):
        entry = data[entries_start + index * 20: entries_start + (index + 1) * 20]
        if len(entry) != 20:
            problems.append(f"xref entry {first + index} is truncated")
            continue
        offset_text, gen_text, kind = entry[:10], entry[11:16], entry[17:18]
        number = first + index
        if kind == b"f":
            continue
        if kind != b"n":
            problems.append(f"xref entry {number} has bad type {kind!r}")
            continue
        try:
            offset = int(offset_text)
        except ValueError:
            problems.append(f"xref entry {number} has a non-numeric offset")
            continue
        expected = b"%d %s obj" % (number, gen_text.lstrip(b"0") or b"0")
        actual = data[offset:offset + len(expected)]
        if actual != expected:
            problems.append(
                f"xref offset {offset} for object {number} points at "
                f"{actual!r}, expected {expected!r}"
            )
    return problems
