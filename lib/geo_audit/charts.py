"""Vector charts that render identically to SVG and to a PDF page.

WHY ONE GEOMETRY, TWO BACKENDS
------------------------------
The audit emits an HTML report and a PDF report from the same run. If the
charts were drawn twice — once with SVG string templates, once with PDF
operators — the two would drift, and a reader comparing them would have no way
to know which one was wrong. So each chart function computes its geometry
**once** into a list of backend-neutral shapes, and :class:`SvgCanvas` and
:class:`PdfCanvas` are thin emitters that replay that list. A layout bug shows
up in both outputs or in neither, which is the only honest arrangement.

Both backends share the same coordinate convention: **top-left origin, y
increasing downward**. That is SVG's native convention, and :mod:`pdfwrite`
adopts it too, so no shape ever needs flipping.

Text is measured with the Helvetica AFM tables in :mod:`pdfwrite`. The SVG
backend asks the browser for ``Helvetica, Arial, sans-serif``, which is
metrically compatible with the AFM widths, so a label centred in the PDF is
centred in the SVG.

WHY THE Q&A BAR POINTS THE OTHER WAY
------------------------------------
:func:`evidence_genre_chart` is the one chart in this module that exists to
argue with its reader. "Add an FAQ section" is the most repeated piece of GEO
advice on the open web, and it is the single evidence genre arXiv:2604.25707
measures as *negative* (§8.2: -5.74% mean influence). Every other genre in that
table lifts influence by 41-77%. Rendering Q&A as a short positive bar, or
dropping it because it spoils the axis, would erase the finding. It is drawn
left of the zero line, in a different colour, on purpose.

GRACEFUL DEGRADATION
--------------------
Every builder accepts empty, all-zero, missing or malformed input and returns
an "insufficient data" placeholder chart of the correct size rather than
raising or dividing by zero. An audit of a five-page brochure site legitimately
produces empty inputs, and a report generator that crashes on its own thin
data is worse than useless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .pdfwrite import PdfDocument, arc_bezier, rgb, text_width, to_hex

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

#: One palette for every chart, so a severity colour means the same thing
#: wherever it appears. Contrast against white is at least 3.5:1 for every
#: fill that carries meaning, which keeps the charts readable in greyscale
#: print and for the most common colour-vision deficiencies.
PALETTE: dict[str, str] = {
    # severity, matching the report's SEVERITIES ordering
    "critical": "#B4232C",
    "high": "#D9722A",
    "medium": "#C79A1E",
    "low": "#3E7CB1",
    "info": "#7A7A7A",
    # structural ink
    "ink": "#1A1A1A",
    "muted": "#6B6B6B",
    "faint": "#9A9A9A",
    "grid": "#DEDCD6",
    "axis": "#B4B1A9",
    "panel": "#F5F4F0",
    "paper": "#FFFFFF",
    # semantic accents
    "positive": "#2F6D8C",
    "negative": "#B4232C",
    "warn": "#D9722A",
    "good": "#3F7D52",
    "accent": "#1F4B61",
}

#: The pipeline is an ordered funnel, so its colours are a ramp rather than a
#: categorical set: a reader can see progression without reading the legend.
LAYER_RAMP: dict[str, str] = {
    "preflight": "#2C4E6C",
    "reachability": "#2F6D8C",
    "selection": "#3A8B95",
    "absorption": "#4E9E86",
    "trust": "#6DAE72",
    "engagement": "#96BC63",
}

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
LAYER_ORDER = ("preflight", "reachability", "selection", "absorption", "trust", "engagement")

# ---------------------------------------------------------------------------
# Paper constants
# ---------------------------------------------------------------------------

# arXiv:2604.25707 §8.2, relative difference in mean influence_score between
# pages that carry an evidence genre and pages that do not. Duplicated from
# textstats.EVIDENCE_GENRE_UPLIFT rather than imported so this module stays
# usable standalone; the values are asserted equal in the test suite.
PAPER_GENRE_UPLIFT: tuple[tuple[str, str, float], ...] = (
    ("code", "Code / worked examples", +76.88),
    ("numeric", "Numeric evidence", +61.55),
    ("definition", "Definitions", +57.33),
    ("comparison", "Comparisons", +55.28),
    ("howto", "How-to procedures", +41.20),
    ("qa_format", "Q&A / FAQ formatting", -5.74),
)

# arXiv:2604.25707 §8.1 Fig. 6, mean influence_score by word-count bin.
PAPER_WORD_COUNT_BINS: tuple[tuple[str, int, int, float], ...] = (
    ("0-100", 0, 100, 0.055),
    ("101-300", 101, 300, 0.085),
    ("301-600", 301, 600, 0.113),
    ("601-1000", 601, 1000, 0.112),
    ("1001-3000", 1001, 3000, 0.126),
    (">3000", 3001, 10 ** 9, 0.146),
)

# Thresholds for the absorption-readiness gauge. Each value is where the named
# band *begins*; below the first is "thin".
GAUGE_BANDS: tuple[tuple[float, float, str, str], ...] = (
    (0.00, 0.25, "thin", "#C4756E"),
    (0.25, 0.45, "weak", "#D9A05B"),
    (0.45, 0.70, "adequate", "#C9C077"),
    (0.70, 1.00, "strong", "#6FA97F"),
)
GAUGE_MARKERS: tuple[tuple[float, str], ...] = ((0.25, "weak"), (0.45, "adequate"), (0.70, "strong"))

# ---------------------------------------------------------------------------
# Default geometry
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 468.0     # fits the content column of both Letter and A4
TITLE_SIZE = 10.5
SUBTITLE_SIZE = 7.6
LABEL_SIZE = 7.6
VALUE_SIZE = 7.6
TICK_SIZE = 7.0


# ---------------------------------------------------------------------------
# Backend-neutral shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RectShape:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = None
    stroke: str | None = None
    line_width: float = 0.7


@dataclass(frozen=True)
class LineShape:
    x1: float
    y1: float
    x2: float
    y2: float
    color: str = PALETTE["grid"]
    line_width: float = 0.6
    dash: tuple | None = None


@dataclass(frozen=True)
class TextShape:
    x: float
    y: float          # baseline
    s: str
    size: float = LABEL_SIZE
    font: str = "helvetica"
    color: str = PALETTE["ink"]
    align: str = "left"


@dataclass(frozen=True)
class PathShape:
    segments: tuple
    fill: str | None = None
    stroke: str | None = None
    line_width: float = 0.8
    close: bool = True


@dataclass(frozen=True)
class PolygonShape:
    points: tuple
    fill: str | None = None
    stroke: str | None = None
    line_width: float = 0.7


# ---------------------------------------------------------------------------
# Canvases
# ---------------------------------------------------------------------------

class Canvas:
    """The five primitives every chart is built from.

    Written as a plain base class rather than a ``typing.Protocol`` so the
    contract is enforceable at import time on any supported Python, and so a
    third backend (a PNG rasteriser, say) has something concrete to subclass.
    """

    def rect(self, shape: RectShape) -> None: raise NotImplementedError
    def line(self, shape: LineShape) -> None: raise NotImplementedError
    def text(self, shape: TextShape) -> None: raise NotImplementedError
    def path(self, shape: PathShape) -> None: raise NotImplementedError
    def polygon(self, shape: PolygonShape) -> None: raise NotImplementedError


def _xml_escape(s: str) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_SVG_ANCHOR = {"left": "start", "center": "middle", "right": "end"}
_SVG_FONT = {
    "helvetica": ('Helvetica, Arial, "Helvetica Neue", sans-serif', "normal", "normal"),
    "bold": ('Helvetica, Arial, "Helvetica Neue", sans-serif', "700", "normal"),
    "oblique": ('Helvetica, Arial, "Helvetica Neue", sans-serif', "normal", "italic"),
    "mono": ('"SFMono-Regular", Menlo, Consolas, monospace', "normal", "normal"),
}


class SvgCanvas(Canvas):
    """Emits SVG elements. Call :meth:`render` for the finished document."""

    def __init__(self, width: float, height: float, background: str | None = None) -> None:
        self.width = width
        self.height = height
        self.background = background
        self.parts: list[str] = []

    # -- primitives -------------------------------------------------------

    def rect(self, shape: RectShape) -> None:
        if shape.w <= 0 or shape.h <= 0:
            return
        self.parts.append(
            f'<rect x="{_f(shape.x)}" y="{_f(shape.y)}" '
            f'width="{_f(shape.w)}" height="{_f(shape.h)}"'
            + self._paint(shape.fill, shape.stroke, shape.line_width)
            + "/>"
        )

    def line(self, shape: LineShape) -> None:
        dash = f' stroke-dasharray="{_f(shape.dash[0])} {_f(shape.dash[1])}"' if shape.dash else ""
        self.parts.append(
            f'<line x1="{_f(shape.x1)}" y1="{_f(shape.y1)}" '
            f'x2="{_f(shape.x2)}" y2="{_f(shape.y2)}" '
            f'stroke="{to_hex(shape.color)}" stroke-width="{_f(shape.line_width)}"{dash}/>'
        )

    def text(self, shape: TextShape) -> None:
        if not str(shape.s):
            return
        family, weight, style = _SVG_FONT.get(shape.font, _SVG_FONT["helvetica"])
        extra = ""
        if weight != "normal":
            extra += f' font-weight="{weight}"'
        if style != "normal":
            extra += f' font-style="{style}"'
        self.parts.append(
            f'<text x="{_f(shape.x)}" y="{_f(shape.y)}" '
            f'font-family=\'{family}\' font-size="{_f(shape.size)}" '
            f'fill="{to_hex(shape.color)}" '
            f'text-anchor="{_SVG_ANCHOR.get(shape.align, "start")}"{extra}>'
            f"{_xml_escape(shape.s)}</text>"
        )

    def path(self, shape: PathShape) -> None:
        if not shape.segments:
            return
        commands: list[str] = []
        for segment in shape.segments:
            kind = segment[0]
            if kind == "m":
                commands.append(f"M {_f(segment[1])} {_f(segment[2])}")
            elif kind == "l":
                commands.append(f"L {_f(segment[1])} {_f(segment[2])}")
            elif kind == "c":
                commands.append(
                    "C " + " ".join(_f(v) for v in segment[1:7])
                )
            elif kind == "h":
                commands.append("Z")
        if shape.close:
            commands.append("Z")
        self.parts.append(
            f'<path d="{" ".join(commands)}"'
            + self._paint(shape.fill, shape.stroke, shape.line_width)
            + "/>"
        )

    def polygon(self, shape: PolygonShape) -> None:
        if len(shape.points) < 2:
            return
        points = " ".join(f"{_f(px)},{_f(py)}" for px, py in shape.points)
        self.parts.append(
            f'<polygon points="{points}"'
            + self._paint(shape.fill, shape.stroke, shape.line_width)
            + "/>"
        )

    # -- output -----------------------------------------------------------

    def _paint(self, fill, stroke, line_width: float) -> str:
        out = f' fill="{to_hex(fill)}"' if fill is not None else ' fill="none"'
        if stroke is not None:
            out += f' stroke="{to_hex(stroke)}" stroke-width="{_f(line_width)}"'
        return out

    def render(self, title: str = "", description: str = "") -> str:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(self.width)}" '
            f'height="{_f(self.height)}" viewBox="0 0 {_f(self.width)} {_f(self.height)}" '
            f'role="img" aria-label="{_xml_escape(title or "chart")}">'
        )
        body = []
        if title:
            body.append(f"<title>{_xml_escape(title)}</title>")
        if description:
            body.append(f"<desc>{_xml_escape(description)}</desc>")
        if self.background:
            body.append(
                f'<rect x="0" y="0" width="{_f(self.width)}" height="{_f(self.height)}" '
                f'fill="{to_hex(self.background)}"/>'
            )
        return head + "".join(body) + "".join(self.parts) + "</svg>"


class PdfCanvas(Canvas):
    """Replays shapes onto a :class:`~geo_audit.pdfwrite.PdfDocument`.

    ``origin`` translates chart-local coordinates onto the page, so a chart
    built at (0, 0) can be placed anywhere in the document flow.
    """

    def __init__(self, doc: PdfDocument, x: float = 0.0, y: float = 0.0) -> None:
        self.doc = doc
        self.ox = x
        self.oy = y

    def rect(self, shape: RectShape) -> None:
        self.doc.rect(
            self.ox + shape.x, self.oy + shape.y, shape.w, shape.h,
            fill=shape.fill, stroke=shape.stroke, line_width=shape.line_width,
        )

    def line(self, shape: LineShape) -> None:
        self.doc.line(
            self.ox + shape.x1, self.oy + shape.y1,
            self.ox + shape.x2, self.oy + shape.y2,
            color=shape.color, line_width=shape.line_width, dash=shape.dash,
        )

    def text(self, shape: TextShape) -> None:
        self.doc.text(
            self.ox + shape.x, self.oy + shape.y, shape.s,
            size=shape.size, font=shape.font, color=shape.color, align=shape.align,
        )

    def path(self, shape: PathShape) -> None:
        segments = []
        for segment in shape.segments:
            kind = segment[0]
            if kind in ("m", "l"):
                segments.append((kind, self.ox + segment[1], self.oy + segment[2]))
            elif kind == "c":
                segments.append((
                    "c",
                    self.ox + segment[1], self.oy + segment[2],
                    self.ox + segment[3], self.oy + segment[4],
                    self.ox + segment[5], self.oy + segment[6],
                ))
            else:
                segments.append((kind,))
        self.doc.path(
            segments, fill=shape.fill, stroke=shape.stroke,
            line_width=shape.line_width, close=shape.close,
        )

    def polygon(self, shape: PolygonShape) -> None:
        self.doc.polygon(
            [(self.ox + px, self.oy + py) for px, py in shape.points],
            fill=shape.fill, stroke=shape.stroke, line_width=shape.line_width,
        )


def _f(value: float) -> str:
    """Compact number formatting shared by both backends."""
    if value is None:
        return "0"
    if abs(value) < 5e-4:
        return "0"
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


# ---------------------------------------------------------------------------
# Chart container
# ---------------------------------------------------------------------------

@dataclass
class Chart:
    """A finished chart: fixed size, plus the shape list both backends replay."""

    title: str
    width: float
    height: float
    shapes: list = field(default_factory=list)
    description: str = ""
    empty: bool = False

    def emit(self, canvas: Canvas) -> Canvas:
        """Replay every shape onto *canvas*. This is the only draw path."""
        for shape in self.shapes:
            if isinstance(shape, RectShape):
                canvas.rect(shape)
            elif isinstance(shape, LineShape):
                canvas.line(shape)
            elif isinstance(shape, TextShape):
                canvas.text(shape)
            elif isinstance(shape, PathShape):
                canvas.path(shape)
            elif isinstance(shape, PolygonShape):
                canvas.polygon(shape)
            else:  # pragma: no cover - guards against a new shape type
                raise TypeError(f"unknown shape {type(shape).__name__}")
        return canvas

    def to_svg(self, background: str | None = None) -> str:
        canvas = SvgCanvas(self.width, self.height, background)
        self.emit(canvas)
        return canvas.render(self.title, self.description)

    def draw_pdf(self, doc: PdfDocument, x: float, y: float) -> None:
        """Draw at absolute page coordinates (top-left corner at *x*, *y*)."""
        self.emit(PdfCanvas(doc, x, y))

    def flow(
        self,
        doc: PdfDocument,
        *,
        top_gap: float = 6.0,
        bottom_gap: float = 10.0,
        align: str = "center",
    ) -> float:
        """Place the chart in the document flow, paginating first if needed."""
        top = doc.reserve(self.height + bottom_gap, top_gap=top_gap)
        x = doc.content_left
        if align == "center":
            x += max(0.0, (doc.content_width - self.width) / 2.0)
        elif align == "right":
            x += max(0.0, doc.content_width - self.width)
        self.draw_pdf(doc, x, top)
        return top


# ---------------------------------------------------------------------------
# Shared drawing helpers
# ---------------------------------------------------------------------------

def _tint(color, amount: float) -> str:
    """Blend *color* toward white. Used where a bar means "absent"."""
    r, g, b = rgb(color)
    amount = min(1.0, max(0.0, amount))
    return to_hex((
        r + (1.0 - r) * amount,
        g + (1.0 - g) * amount,
        b + (1.0 - b) * amount,
    ))


def _nice_ticks(maximum: float, target: int = 4) -> tuple[list[float], float]:
    """Round *maximum* up to a readable axis top and return its tick positions.

    Steps are drawn from the 1 / 2 / 2.5 / 5 / 10 family, which is what makes
    an axis read as "0, 5, 10, 15" instead of "0, 4.7, 9.4".
    """
    if maximum <= 0 or not math.isfinite(maximum):
        return [0.0, 1.0], 1.0
    raw = maximum / max(1, target)
    magnitude = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = magnitude * 10
    for multiple in (1, 2, 2.5, 5, 10):
        if magnitude * multiple >= raw:
            step = magnitude * multiple
            break
    top = math.ceil(maximum / step - 1e-9) * step
    count = int(round(top / step))
    return [i * step for i in range(count + 1)], top


def _fmt_count(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _placeholder(title: str, width: float, height: float, message: str) -> Chart:
    """The graceful-degradation output: a labelled empty frame, never a crash."""
    shapes: list = [
        RectShape(0, 0, width, height, fill=PALETTE["panel"], stroke=PALETTE["grid"], line_width=0.7),
    ]
    if title:
        shapes.append(TextShape(0, 11.5, title, TITLE_SIZE, "bold", PALETTE["ink"]))
    shapes.append(
        TextShape(width / 2.0, height / 2.0 + 3.0, message, 8.4, "oblique", PALETTE["muted"], "center")
    )
    return Chart(title=title, width=width, height=height, shapes=shapes,
                 description=message, empty=True)


def _title_block(shapes: list, title: str, subtitle: str, width: float) -> float:
    """Draw the title (and optional subtitle) and return the y below them."""
    y = 0.0
    if title:
        y += TITLE_SIZE
        shapes.append(TextShape(0, y, title, TITLE_SIZE, "bold", PALETTE["ink"]))
        y += 4.0
    if subtitle:
        y += SUBTITLE_SIZE
        shapes.append(TextShape(0, y, subtitle, SUBTITLE_SIZE, "oblique", PALETTE["muted"]))
        y += 4.0
    if title or subtitle:
        y += 2.0
        shapes.append(LineShape(0, y, width, y, PALETTE["grid"], 0.6))
        y += 9.0
    return y


def _swatch(shapes: list, x: float, y: float, size: float, color, stroke=None) -> None:
    shapes.append(RectShape(x, y, size, size, fill=color, stroke=stroke,
                            line_width=0.5 if stroke else 0.0))


# ---------------------------------------------------------------------------
# 1 + 2. Horizontal category bars
# ---------------------------------------------------------------------------

def _horizontal_bars(
    *,
    title: str,
    subtitle: str,
    rows: list[tuple[str, float, str]],
    width: float,
    axis_title: str,
    row_height: float,
    empty_message: str,
) -> Chart:
    """Shared geometry for :func:`severity_bar` and :func:`layer_bar`.

    Zero-valued rows are kept rather than filtered out: "0 critical findings"
    is itself a result, and dropping the row would make two audits with
    different profiles produce charts of different shapes.
    """
    if not rows:
        return _placeholder(title, width, 96.0, empty_message)

    label_gutter = min(
        118.0,
        max(52.0, max(text_width(label, LABEL_SIZE, "bold") for label, _, _ in rows) + 10.0),
    )
    value_gutter = 34.0
    plot_left = label_gutter
    plot_right = width - value_gutter
    plot_width = max(40.0, plot_right - plot_left)

    shapes: list = []
    top = _title_block(shapes, title, subtitle, width)

    peak = max((value for _, value, _ in rows), default=0.0)
    ticks, axis_top = _nice_ticks(peak, target=4)
    all_zero = peak <= 0

    bar_height = row_height * 0.62
    plot_top = top
    plot_bottom = plot_top + len(rows) * row_height

    # Light grid first, so bars paint over it.
    for tick in ticks:
        tx = plot_left + (tick / axis_top) * plot_width if axis_top else plot_left
        shapes.append(LineShape(tx, plot_top - 2.0, tx, plot_bottom + 1.0,
                                PALETTE["grid"], 0.5))
        shapes.append(TextShape(tx, plot_bottom + 11.0, _fmt_count(tick),
                                TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
    shapes.append(LineShape(plot_left, plot_top - 2.0, plot_left, plot_bottom + 1.0,
                            PALETTE["axis"], 0.9))

    for index, (label, value, color) in enumerate(rows):
        row_top = plot_top + index * row_height
        centre = row_top + row_height / 2.0
        bar_top = centre - bar_height / 2.0
        fraction = (value / axis_top) if axis_top > 0 else 0.0
        bar_width = max(0.0, fraction) * plot_width
        shapes.append(TextShape(plot_left - 7.0, centre + LABEL_SIZE * 0.35, label,
                                LABEL_SIZE, "bold", PALETTE["ink"], "right"))
        if bar_width >= 0.6:
            shapes.append(RectShape(plot_left, bar_top, bar_width, bar_height, fill=color))
        else:
            # A visible stub keeps a zero row from reading as a missing row.
            shapes.append(RectShape(plot_left, bar_top, 1.6, bar_height,
                                    fill=PALETTE["grid"]))
        shapes.append(TextShape(plot_left + max(bar_width, 1.6) + 5.0,
                                centre + VALUE_SIZE * 0.35, _fmt_count(value),
                                VALUE_SIZE, "bold",
                                PALETTE["muted"] if value <= 0 else PALETTE["ink"]))

    baseline = plot_bottom + 15.0
    if axis_title:
        shapes.append(TextShape(plot_left + plot_width / 2.0, baseline + 8.5, axis_title,
                                TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
        baseline += 11.0
    if all_zero:
        shapes.append(TextShape(plot_left + plot_width / 2.0, plot_top + len(rows) * row_height / 2.0,
                                "no findings recorded", 8.2, "oblique", PALETTE["faint"], "center"))

    return Chart(title=title, width=width, height=baseline + 4.0, shapes=shapes,
                 description=subtitle or title, empty=all_zero)


def severity_bar(
    counts: dict,
    *,
    width: float = DEFAULT_WIDTH,
    title: str = "Findings by severity",
    subtitle: str = "",
) -> Chart:
    """Finding counts by severity, in the report's canonical severity order."""
    counts = counts or {}
    rows = [
        (name, _as_number(counts.get(name)), PALETTE[name])
        for name in SEVERITY_ORDER
    ]
    return _horizontal_bars(
        title=title,
        subtitle=subtitle,
        rows=rows,
        width=width,
        axis_title="number of findings",
        row_height=19.0,
        empty_message="no severity counts supplied",
    )


def layer_bar(
    counts: dict,
    *,
    width: float = DEFAULT_WIDTH,
    title: str = "Findings by pipeline layer",
    subtitle: str = "Layers run in order; an upstream defect often explains a downstream symptom.",
) -> Chart:
    """Finding counts per pipeline layer, in pipeline order rather than by size.

    Sorting by count would be more flattering to the eye and less useful: the
    reading order of these layers is causal, so it is preserved.
    """
    counts = counts or {}
    rows = [
        (name, _as_number(counts.get(name)), LAYER_RAMP[name])
        for name in LAYER_ORDER
    ]
    return _horizontal_bars(
        title=title,
        subtitle=subtitle,
        rows=rows,
        width=width,
        axis_title="number of findings",
        row_height=18.0,
        empty_message="no layer counts supplied",
    )


def _as_number(value) -> float:
    """Coerce anything a report dict might hold into a finite, non-negative float."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


# ---------------------------------------------------------------------------
# 3. Donut
# ---------------------------------------------------------------------------

def donut(
    segments,
    *,
    width: float = DEFAULT_WIDTH,
    height: float = 168.0,
    title: str = "Composition",
    subtitle: str = "",
    centre_label: str = "",
    inner_ratio: float = 0.58,
) -> Chart:
    """A donut chart from ``(label, value, color)`` triples.

    Arcs are cubic-Bézier approximations (see
    :func:`~geo_audit.pdfwrite.arc_bezier`), split at 90 degrees so the error
    stays well under a printed pixel. A single segment covering the whole
    circle is drawn as two concentric subpaths instead of a wedge, which avoids
    a visible radial seam at 12 o'clock.
    """
    cleaned: list[tuple[str, float, str]] = []
    for entry in segments or []:
        try:
            label, value, color = entry[0], _as_number(entry[1]), entry[2]
        except (TypeError, IndexError):
            continue
        if value > 0:
            cleaned.append((str(label), value, color))

    total = sum(value for _, value, _ in cleaned)
    if not cleaned or total <= 0:
        return _placeholder(title, width, height, "insufficient data - nothing to apportion")

    shapes: list = []
    top = _title_block(shapes, title, subtitle, width)

    plot_height = height - top - 6.0
    radius = max(24.0, min(plot_height / 2.0, 62.0))
    cx = radius + 8.0
    cy = top + plot_height / 2.0
    inner = radius * min(0.85, max(0.1, inner_ratio))

    # Start at 12 o'clock. Because y grows downward, increasing the angle
    # sweeps clockwise on screen, which is the direction a reader expects.
    angle = -math.pi / 2.0
    for label, value, color in cleaned:
        sweep = 2.0 * math.pi * (value / total)
        if value / total >= 0.9995:
            # Whole circle: two subpaths, outer clockwise and inner reversed,
            # so the nonzero-winding fill leaves a clean hole.
            outer = arc_bezier(cx, cy, radius, angle, angle + 2.0 * math.pi)
            hole = arc_bezier(cx, cy, inner, angle + 2.0 * math.pi, angle)
            shapes.append(PathShape(tuple(outer + hole), fill=color,
                                    stroke=PALETTE["paper"], line_width=0.8))
        else:
            end = angle + sweep
            segments_out = arc_bezier(cx, cy, radius, angle, end)
            segments_in = arc_bezier(cx, cy, inner, end, angle)
            # Replace the inner arc's leading "m" with a "l" so the two arcs
            # join into one closed wedge instead of two subpaths.
            segments_in[0] = ("l", segments_in[0][1], segments_in[0][2])
            shapes.append(PathShape(tuple(segments_out + segments_in), fill=color,
                                    stroke=PALETTE["paper"], line_width=0.8))
        angle += sweep

    if centre_label:
        shapes.append(TextShape(cx, cy - 1.0, centre_label, 13.0, "bold",
                                PALETTE["ink"], "center"))
        shapes.append(TextShape(cx, cy + 10.0, "total", 7.0, "helvetica",
                                PALETTE["muted"], "center"))
    else:
        shapes.append(TextShape(cx, cy + 2.0, _fmt_count(total), 14.0, "bold",
                                PALETTE["ink"], "center"))

    # Legend: label, absolute value, and share, so the donut never has to be
    # read by eye to recover a number.
    legend_x = cx + radius + 22.0
    legend_width = width - legend_x
    step = min(15.0, max(11.0, plot_height / max(1, len(cleaned))))
    legend_y = cy - (len(cleaned) * step) / 2.0 + step * 0.5
    for label, value, color in cleaned:
        _swatch(shapes, legend_x, legend_y - 6.0, 8.0, color)
        share = 100.0 * value / total
        stat = f"{_fmt_count(value)}  ({share:.0f}%)"
        stat_width = text_width(stat, LABEL_SIZE, "bold")
        room = legend_width - 12.0 - stat_width - 8.0
        shapes.append(TextShape(legend_x + 12.0, legend_y, _ellipsize(label, room, LABEL_SIZE),
                                LABEL_SIZE, "helvetica", PALETTE["ink"]))
        shapes.append(TextShape(width, legend_y, stat, LABEL_SIZE, "bold",
                                PALETTE["ink"], "right"))
        legend_y += step

    return Chart(title=title, width=width, height=height, shapes=shapes,
                 description=subtitle or title)


def _ellipsize(text: str, room: float, size: float, font: str = "helvetica") -> str:
    """Trim *text* to fit *room* points, with a trailing ellipsis."""
    text = str(text)
    if room <= 0 or text_width(text, size, font) <= room:
        return text
    trimmed = text
    while trimmed and text_width(trimmed + "...", size, font) > room:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() + "...") if trimmed else ""


# ---------------------------------------------------------------------------
# 4. Evidence genres (diverging)
# ---------------------------------------------------------------------------

def evidence_genre_chart(
    present: dict | None = None,
    *,
    width: float = DEFAULT_WIDTH,
    title: str = "Evidence genres: measured influence uplift vs. your coverage",
    subtitle: str = "Bars are the paper's measured effect (arXiv:2604.25707 §8.2); fill shows whether your site carries the genre.",
) -> Chart:
    """The paper's per-genre uplift, with this site's coverage overlaid.

    The bars are *not* this site's data — they are the corpus-level effect
    sizes, which is why they never change between audits. What changes is the
    fill: a solid bar means the site carries that genre, a pale bar means it
    does not. Reading the chart is then a single visual question, "which of the
    tall bars are pale?"

    ``present`` may be empty or missing keys; a genre with no entry is drawn as
    absent and the footnote says the coverage data was not supplied.
    """
    present = present if isinstance(present, dict) else {}
    has_site_data = bool(present)

    label_gutter = min(
        126.0,
        max(88.0, max(text_width(label, LABEL_SIZE, "bold") for _, label, _ in PAPER_GENRE_UPLIFT) + 10.0),
    )
    badge_gutter = 46.0
    plot_left = label_gutter
    plot_right = width - badge_gutter
    plot_width = max(60.0, plot_right - plot_left)

    values = [value for _, _, value in PAPER_GENRE_UPLIFT]
    lower = min(min(values), 0.0)
    upper = max(max(values), 0.0)
    # Round outward to a 10-point grid so the axis reads in round percentages.
    domain_min = math.floor(lower / 10.0) * 10.0 - (10.0 if lower % 10 == 0 else 0.0)
    domain_max = math.ceil(upper / 10.0) * 10.0
    span = domain_max - domain_min
    if span <= 0:
        return _placeholder(title, width, 180.0, "insufficient data")

    def x_of(value: float) -> float:
        return plot_left + (value - domain_min) / span * plot_width

    shapes: list = []
    top = _title_block(shapes, title, subtitle, width)

    row_height = 20.0
    bar_height = 11.5
    plot_top = top
    plot_bottom = plot_top + len(PAPER_GENRE_UPLIFT) * row_height

    tick = domain_min
    while tick <= domain_max + 1e-9:
        tx = x_of(tick)
        shapes.append(LineShape(tx, plot_top - 2.0, tx, plot_bottom + 1.0, PALETTE["grid"], 0.5))
        shapes.append(TextShape(tx, plot_bottom + 11.0, f"{tick:+.0f}%" if tick else "0",
                                TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
        tick += 20.0

    zero_x = x_of(0.0)
    shapes.append(LineShape(zero_x, plot_top - 4.0, zero_x, plot_bottom + 3.0,
                            PALETTE["axis"], 1.1))

    for index, (key, label, uplift) in enumerate(PAPER_GENRE_UPLIFT):
        centre = plot_top + index * row_height + row_height / 2.0
        bar_top = centre - bar_height / 2.0
        is_present = bool(present.get(key, False))
        negative = uplift < 0
        base = PALETTE["negative"] if negative else PALETTE["positive"]
        fill = base if is_present else _tint(base, 0.74)

        end_x = x_of(uplift)
        bar_x = min(zero_x, end_x)
        bar_w = abs(end_x - zero_x)
        shapes.append(RectShape(bar_x, bar_top, max(bar_w, 0.8), bar_height,
                                fill=fill, stroke=base, line_width=0.7))

        shapes.append(TextShape(plot_left - 7.0, centre + LABEL_SIZE * 0.34, label,
                                LABEL_SIZE, "bold", PALETTE["ink"], "right"))
        # Value labels sit outside the bar, on the side it grows toward — except
        # for a negative bar, which is short enough that a left-placed label
        # collides with the category labels. The row's positive half is empty,
        # so the label goes there and the bar itself carries the direction.
        if negative:
            shapes.append(TextShape(zero_x + 6.0, centre + VALUE_SIZE * 0.34,
                                    f"{uplift:+.2f}%", VALUE_SIZE, "bold",
                                    PALETTE["negative"]))
        else:
            shapes.append(TextShape(bar_x + bar_w + 4.0, centre + VALUE_SIZE * 0.34,
                                    f"{uplift:+.2f}%", VALUE_SIZE, "bold",
                                    PALETTE["ink"]))

        badge = "on site" if is_present else "absent"
        badge_color = PALETTE["good"] if is_present else PALETTE["faint"]
        if key == "qa_format" and is_present:
            # Present-and-negative is the one combination worth calling out.
            badge, badge_color = "on site", PALETTE["warn"]
        shapes.append(TextShape(width, centre + 2.6, badge, 7.0, "bold",
                                badge_color if has_site_data else PALETTE["faint"], "right"))

    y = plot_bottom + 15.0
    shapes.append(TextShape(plot_left + plot_width / 2.0, y + 8.5,
                            "relative difference in mean influence score (paper, n = corpus)",
                            TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
    y += 14.0

    note = (
        "Q&A / FAQ formatting is the paper's one negative genre: pages carrying it "
        "averaged 5.74% LESS influence, so an FAQ wrapper is not a substitute for "
        "the evidence above it."
    )
    if not has_site_data:
        note += "  Site coverage was not supplied, so every row is drawn as absent."
    y += 9.0
    shapes.append(TextShape(0, y, _ellipsize(note, width, 6.9), 6.9, "oblique", PALETTE["muted"]))

    return Chart(title=title, width=width, height=y + 6.0, shapes=shapes,
                 description=subtitle)


# ---------------------------------------------------------------------------
# 5. Absorption gauge
# ---------------------------------------------------------------------------

def absorption_gauge(
    score,
    *,
    width: float = DEFAULT_WIDTH,
    title: str = "Absorption readiness",
    subtitle: str = "Level 3 mechanism proxy, not a prediction of citation.",
) -> Chart:
    """A 0..1 gauge with the weak / adequate / strong band boundaries marked.

    The band names come from :data:`GAUGE_BANDS`; each marker is the value at
    which the next band begins. Out-of-range input is clamped and labelled
    rather than rejected, but ``None`` and NaN produce the placeholder, because
    "we could not score this" and "this scored zero" are different statements.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return _placeholder(title, width, 96.0, "insufficient data - no absorption score")
    if not math.isfinite(value):
        return _placeholder(title, width, 96.0, "insufficient data - no absorption score")
    clamped = min(1.0, max(0.0, value))

    shapes: list = []
    top = _title_block(shapes, title, subtitle, width)

    track_left = 0.0
    track_right = width - 52.0     # room for the numeric readout on the right
    track_width = track_right - track_left
    pointer_room = 15.0
    track_top = top + pointer_room
    track_height = 17.0

    def x_of(fraction: float) -> float:
        return track_left + fraction * track_width

    for low, high, name, color in GAUGE_BANDS:
        shapes.append(RectShape(x_of(low), track_top, x_of(high) - x_of(low),
                                track_height, fill=_tint(color, 0.42)))
        mid = (low + high) / 2.0
        shapes.append(TextShape(x_of(mid), track_top + track_height + 10.0, name,
                                TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
    shapes.append(RectShape(track_left, track_top, track_width, track_height,
                            stroke=PALETTE["axis"], line_width=0.7))

    # Band boundaries: a full-height rule plus the numeric threshold.
    for marker, name in GAUGE_MARKERS:
        mx = x_of(marker)
        shapes.append(LineShape(mx, track_top - 3.0, mx, track_top + track_height + 3.0,
                                PALETTE["ink"], 0.8, dash=(2.0, 2.0)))
        shapes.append(TextShape(mx, track_top - 5.5, f"{marker:.2f}", 6.8, "bold",
                                PALETTE["muted"], "center"))

    # The score itself: a filled bar to the value, plus a pointer above it.
    px = x_of(clamped)
    shapes.append(RectShape(track_left, track_top + track_height * 0.28,
                            px - track_left, track_height * 0.44,
                            fill=PALETTE["accent"]))
    shapes.append(PolygonShape(
        ((px, track_top - 1.0), (px - 5.0, track_top - 9.0), (px + 5.0, track_top - 9.0)),
        fill=PALETTE["ink"],
    ))

    band_name = next((name for low, high, name, _ in GAUGE_BANDS if low <= clamped < high),
                     GAUGE_BANDS[-1][2])
    shapes.append(TextShape(width, track_top + 9.0, f"{clamped:.2f}", 15.0, "bold",
                            PALETTE["ink"], "right"))
    shapes.append(TextShape(width, track_top + track_height + 10.0, band_name, 7.4, "bold",
                            PALETTE["muted"], "right"))

    y = track_top + track_height + 14.0
    shapes.append(TextShape(x_of(0.0), y + 8.0, "0.00", TICK_SIZE, "helvetica",
                            PALETTE["muted"], "left"))
    shapes.append(TextShape(x_of(1.0), y + 8.0, "1.00", TICK_SIZE, "helvetica",
                            PALETTE["muted"], "right"))
    shapes.append(TextShape(track_width / 2.0, y + 8.0, "absorption-readiness score",
                            TICK_SIZE, "helvetica", PALETTE["muted"], "center"))
    y += 12.0

    if value != clamped:
        y += 9.0
        shapes.append(TextShape(0, y, f"raw score {value:.3f} clamped to the 0-1 range.",
                                6.9, "oblique", PALETTE["warn"]))

    return Chart(title=title, width=width, height=y + 5.0, shapes=shapes,
                 description=f"absorption readiness {clamped:.2f} ({band_name})")


# ---------------------------------------------------------------------------
# 6. Word-count distribution
# ---------------------------------------------------------------------------

def word_count_distribution(
    word_counts,
    *,
    width: float = DEFAULT_WIDTH,
    height: float = 196.0,
    title: str = "Page length distribution vs. expected influence",
    subtitle: str = "Bars: your pages per bin. Line: the paper's mean influence for that bin (Fig. 6).",
) -> Chart:
    """Pages bucketed into the paper's word-count bins, with its influence curve.

    The two series share an x axis but nothing else, so the influence curve is
    drawn against its own right-hand scale and in a contrasting colour. The
    point of overlaying them is to show where a site's mass sits relative to
    where influence is highest, which no single-series chart can express.
    """
    values: list[int] = []
    for entry in word_counts or []:
        try:
            number = int(entry)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            values.append(number)

    if not values:
        return _placeholder(title, width, 128.0,
                            "insufficient data - no page word counts collected")

    buckets = [0] * len(PAPER_WORD_COUNT_BINS)
    for number in values:
        for index, (_, low, high, _) in enumerate(PAPER_WORD_COUNT_BINS):
            if low <= number <= high:
                buckets[index] += 1
                break

    shapes: list = []
    top = _title_block(shapes, title, subtitle, width)

    left_gutter = 26.0
    right_gutter = 34.0
    plot_left = left_gutter
    plot_right = width - right_gutter
    plot_width = plot_right - plot_left
    plot_top = top + 4.0
    plot_bottom = height - 34.0
    plot_height = max(40.0, plot_bottom - plot_top)

    ticks, axis_top = _nice_ticks(max(buckets), target=4)
    influence_top = 0.16     # comfortably above the paper's 0.146 maximum

    for tick in ticks:
        ty = plot_bottom - (tick / axis_top) * plot_height if axis_top else plot_bottom
        shapes.append(LineShape(plot_left, ty, plot_right, ty, PALETTE["grid"], 0.5))
        shapes.append(TextShape(plot_left - 4.0, ty + 2.4, _fmt_count(tick), TICK_SIZE,
                                "helvetica", PALETTE["muted"], "right"))
    shapes.append(LineShape(plot_left, plot_top, plot_left, plot_bottom, PALETTE["axis"], 0.9))
    shapes.append(LineShape(plot_left, plot_bottom, plot_right, plot_bottom, PALETTE["axis"], 0.9))

    slot = plot_width / len(PAPER_WORD_COUNT_BINS)
    bar_width = slot * 0.56
    points: list[tuple[float, float]] = []

    for index, (label, _, _, influence) in enumerate(PAPER_WORD_COUNT_BINS):
        centre = plot_left + slot * (index + 0.5)
        count = buckets[index]
        bar_height = (count / axis_top) * plot_height if axis_top else 0.0
        # The bins the paper associates with the highest influence are tinted
        # green, so "where should my pages be" is legible without the legend.
        color = PALETTE["good"] if influence >= 0.113 else PALETTE["positive"]
        if count > 0:
            shapes.append(RectShape(centre - bar_width / 2.0, plot_bottom - bar_height,
                                    bar_width, bar_height, fill=color))
        else:
            shapes.append(RectShape(centre - bar_width / 2.0, plot_bottom - 1.4,
                                    bar_width, 1.4, fill=PALETTE["grid"]))
        shapes.append(TextShape(centre, plot_bottom - bar_height - 3.5, _fmt_count(count),
                                VALUE_SIZE, "bold",
                                PALETTE["ink"] if count else PALETTE["faint"], "center"))
        shapes.append(TextShape(centre, plot_bottom + 11.0, label, TICK_SIZE,
                                "helvetica", PALETTE["muted"], "center"))
        points.append((centre, plot_bottom - (influence / influence_top) * plot_height))

    # Secondary series: the paper's expected mean influence per bin.
    line_segments = [("m", points[0][0], points[0][1])]
    line_segments += [("l", px, py) for px, py in points[1:]]
    shapes.append(PathShape(tuple(line_segments), stroke=PALETTE["warn"],
                            line_width=1.5, close=False))
    for (px, py), (_, _, _, influence) in zip(points, PAPER_WORD_COUNT_BINS):
        shapes.append(RectShape(px - 2.6, py - 2.6, 5.2, 5.2,
                                fill=PALETTE["paper"], stroke=PALETTE["warn"], line_width=1.2))
        shapes.append(TextShape(px, py - 6.0, f"{influence:.3f}", 6.4, "bold",
                                PALETTE["warn"], "center"))

    for fraction, label in ((0.0, "0.00"), (0.5, "0.08"), (1.0, "0.16")):
        ty = plot_bottom - fraction * plot_height
        shapes.append(TextShape(plot_right + 4.0, ty + 2.4, label, 6.4, "helvetica",
                                PALETTE["warn"], "left"))

    shapes.append(TextShape(plot_left + plot_width / 2.0, plot_bottom + 22.0,
                            "page word count (paper's bins)", TICK_SIZE,
                            "helvetica", PALETTE["muted"], "center"))
    shapes.append(TextShape(0, plot_top - 4.0, "pages", TICK_SIZE, "helvetica", PALETTE["muted"]))
    shapes.append(TextShape(width, plot_top - 4.0, "influence", TICK_SIZE, "helvetica",
                            PALETTE["warn"], "right"))

    return Chart(title=title, width=width, height=height, shapes=shapes,
                 description=subtitle, empty=False)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def severity_donut(counts: dict, **kwargs) -> Chart:
    """A donut of the same severity counts :func:`severity_bar` renders."""
    counts = counts or {}
    segments = [
        (name, _as_number(counts.get(name)), PALETTE[name])
        for name in SEVERITY_ORDER
    ]
    kwargs.setdefault("title", "Finding mix")
    kwargs.setdefault("subtitle", "share of all findings, by severity")
    return donut(segments, **kwargs)


def render_svg_bundle(charts: dict) -> dict:
    """Render a mapping of ``name -> Chart`` to a mapping of ``name -> SVG``."""
    return {name: chart.to_svg() for name, chart in charts.items()}
