"""A deliberately flawed fixture site, served locally for end-to-end tests.

Every page carries known defects so detection can be checked against ground
truth rather than against "did it produce some findings". Serving over real
HTTP (rather than stubbing the fetcher) exercises the whole stack: netguard,
robots, the fetcher, discovery, parsing and scoring.

Ground truth is declared in EXPECTED_FINDINGS at the bottom.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROBOTS = """\
User-agent: GPTBot
Disallow: /

User-agent: PerplexityBot
Disallow: /

User-agent: *
Disallow: /admin
Crawl-delay: 1

Sitemap: http://127.0.0.1:{port}/sitemap.xml
"""

SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://127.0.0.1:{port}/</loc><lastmod>2019-01-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/pricing</loc><lastmod>2019-02-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/about</loc><lastmod>2019-03-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/products</loc><lastmod>2019-04-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/faq</loc><lastmod>2019-05-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/privacy</loc><lastmod>2019-06-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/docs/what-is-a-widget</loc><lastmod>2019-07-01</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/products/widget</loc><lastmod>not-a-date</lastmod></url>
  <url><loc>http://127.0.0.1:{port}/gone</loc><lastmod>2019-08-01</lastmod></url>
  <url><loc>https://elsewhere.invalid/x</loc><lastmod>2019-09-01</lastmod></url>
</urlset>
"""

# Rich, well-built page: definitions, numbers, comparison, structure.
GOOD_PAGE = """<!doctype html><html lang="en"><head>
<title>What is a Widget — Acme</title>
<meta name="description" content="A widget is a modular fastening component.">
<link rel="canonical" href="http://127.0.0.1:{port}/docs/what-is-a-widget">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"TechArticle","headline":"What is a Widget",
"datePublished":"2026-06-01","dateModified":"2026-08-15",
"author":{"@type":"Organization","name":"Acme Fastening"}}
</script></head><body>
<nav><a href="/">Home</a><a href="/pricing">Pricing</a><a href="/products/widget">Widget</a>
<a href="/docs/what-is-a-widget">Docs</a><a href="/about">About</a><a href="/privacy">Privacy</a>
<a href="/blog/post-1">Blog</a></nav>
<main>
<h1>What is a widget</h1>
<p>A widget is defined as a modular fastening component that joins two load-bearing
surfaces without welding. In other words, it refers to a reusable mechanical joint.</p>
<h2>Specifications</h2>
<p>The standard widget holds 450 kg at 20 mm thickness and costs $49 per unit.
Installation takes 12 minutes on average across 1,200 recorded installations.</p>
<ul><li>Load rating: 450 kg</li><li>Thickness: 20 mm</li><li>Price: $49</li>
<li>Install time: 12 minutes</li></ul>
<h2>Widgets compared to welding</h2>
<p>Compared to welding, widgets are reversible and require no certification.
In contrast, welded joints are permanent. The difference between the two is
primarily reversibility. Unlike welding, a widget can be removed.</p>
<h2>How to install</h2>
<p>First, align the surfaces. Second, insert the widget. Third, torque to 40 Nm.
Finally, verify the seal. Follow these instructions in order.</p>
<h2>Reference</h2>
<p>According to ISO 8992, fastening components must be rated. Source:
<a href="https://www.iso.org/standard/example">ISO standard</a>.</p>
<time datetime="2026-08-15">15 August 2026</time>
</main><footer>© Acme Fastening</footer>
<!-- A deliberately malformed block, so SD002 has something real to catch. -->
<script type="application/ld+json">{"@type":"FAQPage","mainEntity":[,]}</script>
</body></html>"""

# Thin, marketing-heavy, no evidence, no schema.
THIN_PAGE = """<!doctype html><html lang="en"><head><title>Pricing — Acme</title></head>
<body><nav><a href="/">Home</a><a href="/pricing">Pricing</a><a href="/about">About</a></nav>
<div><h1>Pricing</h1>
<p>Our industry-leading, world-class pricing is seamless and best-in-class.
This revolutionary, cutting-edge platform delivers game-changing synergy.</p>
</div><footer>© Acme</footer></body></html>"""

# JavaScript shell: content only in a state blob.
JS_PAGE = """<!doctype html><html lang="en"><head><title>Widget — Acme</title>
<meta name="description" content="The Acme widget product page.">
<link rel="stylesheet" href="/a.css"><link rel="stylesheet" href="/b.css">
<script src="/vendor.js"></script><script src="/app.js"></script>
</head><body><div id="root"></div>
<script>window.__NEXT_DATA__={"props":{"pageProps":{"product":{
"name":"Acme Widget","description":"The Acme widget is a modular fastening component rated to 450 kg that installs in twelve minutes without welding equipment or certification.",
"price":"49.00","specs":"Load rating 450 kg, thickness 20 mm, reversible installation, no certification required for operators.",
"comparison":"Compared to welded joints the widget is fully reversible and needs no certified operator.",
"overview":"A widget is defined as a modular fastening component that joins two load-bearing surfaces without welding, and it can be removed and reused without damaging either surface.",
"installation":"First align the two surfaces to be joined, then insert the widget through the pre-drilled aperture, torque the retaining collar to forty newton metres, and finally verify the seal with the supplied gauge.",
"materials":"The body is machined from 316 stainless steel and the collar from anodised aluminium, giving a service temperature range from minus forty to two hundred degrees celsius.",
"warranty":"Every widget carries a ten year structural warranty covering material defects and load failure under rated conditions, with replacement shipped within three business days.",
"certification":"Widgets are tested to ISO 8992 and carry CE marking for use in load bearing applications across the European Economic Area without further certification.",
"support":"Technical support is available by telephone and electronic mail during business hours, and installation training is provided at no additional cost for orders above one hundred units.",
"shipping":"Orders dispatch within one business day and arrive within five working days to most destinations, with expedited options available at checkout for time critical projects.",
"compatibility":"The widget is compatible with all standard twenty millimetre apertures and can be adapted to twenty five millimetre apertures using the supplied reducer collar."}}}}</script>
<script src="/_next/static/chunks/main.js"></script></body></html>"""

# Q&A wrapper with no evidence underneath — the paper's negative case.
QA_PAGE = """<!doctype html><html lang="en"><head><title>FAQ — Acme</title></head>
<body><nav><a href="/">Home</a></nav><main><h1>Frequently asked questions</h1>
<p>Q: What makes Acme different? A: We are industry-leading.</p>
<p>Q: Why choose us? A: Our world-class team delivers seamless results.</p>
<p>Q: How do we help? A: Through cutting-edge, best-in-class solutions.</p>
<p>Q: Is it good? A: Yes, it is revolutionary.</p>
</main></body></html>"""

# Legal page: short and evidence-free by nature. MUST NOT be flagged.
LEGAL_PAGE = """<!doctype html><html lang="en"><head><title>Privacy Policy — Acme</title></head>
<body><nav><a href="/">Home</a></nav><main><h1>Privacy policy</h1>
<p>We collect the information you provide when you contact us. We do not sell it.
We retain it for as long as your account is active. Contact privacy@acme.invalid
to request deletion of your data.</p>
<time datetime="2026-01-10">10 January 2026</time></main></body></html>"""

# Category hub: mostly links by design. MUST NOT be flagged as thin.
HUB_PAGE = """<!doctype html><html lang="en"><head><title>Products — Acme</title></head>
<body><nav><a href="/">Home</a></nav><main><h1>Products</h1>
<ul><li><a href="/products/widget">Widget</a></li>
<li><a href="/products/bracket">Bracket</a></li>
<li><a href="/products/clamp">Clamp</a></li></ul>
</main></body></html>"""

# Homepage: no Organization schema, no h1.
HOME_PAGE = """<!doctype html><html lang="en"><head><title>Acme</title>
<meta name="description" content="Acme makes fastening components."></head>
<body><nav><a href="/">Home</a><a href="/pricing">Pricing</a>
<a href="/products/widget">Widget</a><a href="/docs/what-is-a-widget">Docs</a>
<a href="/about">About</a><a href="/privacy">Privacy</a><a href="/products">Products</a>
<a href="/faq">FAQ</a><a href="/blog/post-1">Blog</a></nav>
<main><h2>Welcome to Acme</h2>
<p>We build industry-leading fastening components for demanding environments.</p>
</main><footer>© Acme</footer></body></html>"""

# Stale, undated article.
STALE_ARTICLE = """<!doctype html><html lang="en"><head><title>Our 2019 results — Acme</title>
<meta name="robots" content="noindex"></head>
<body><nav><a href="/">Home</a></nav><main><h1>Our 2019 results</h1>
<p>Acme grew 300% last year, making us the number one provider in the market.
Studies show our approach is 40% more effective than alternatives.</p>
</main></body></html>"""

ROUTES: dict[str, tuple[int, str, str]] = {
    "/": (200, "text/html", HOME_PAGE),
    "/pricing": (200, "text/html", THIN_PAGE),
    "/products": (200, "text/html", HUB_PAGE),
    "/products/widget": (200, "text/html", JS_PAGE),
    "/docs/what-is-a-widget": (200, "text/html", GOOD_PAGE),
    "/faq": (200, "text/html", QA_PAGE),
    "/privacy": (200, "text/html", LEGAL_PAGE),
    "/blog/post-1": (200, "text/html", STALE_ARTICLE),
    "/about": (200, "text/html", THIN_PAGE),
    "/gone": (404, "text/html", "<html><body><h1>Not found</h1></body></html>"),
    "/robots.txt": (200, "text/plain", ROBOTS),
    "/sitemap.xml": (200, "application/xml", SITEMAP),
}


class _Handler(BaseHTTPRequestHandler):
    port = 0

    def _send(self, status: int, ctype: str, body: str) -> None:
        payload = body.replace("{port}", str(self.port)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ROUTES:
            self._send(*ROUTES[path])
        else:
            self._send(404, "text/html", "<html><body><h1>Not found</h1></body></html>")

    do_HEAD = do_GET

    def log_message(self, *args) -> None:  # noqa: D102 - silence the test output
        pass


class FixtureSite:
    """Context manager that serves the fixture on an ephemeral port."""

    def __init__(self) -> None:
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> "FixtureSite":
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        _Handler.port = self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# Ground truth. Keys are check ids the audit MUST surface, and MUST NOT surface.
EXPECTED_FINDINGS = {
    "must_detect": {
        "robots_blocks_ai_retrieval",   # PerplexityBot is disallowed
        "no_https",                     # fixture is served over http
        "sitemap_off_scope",            # elsewhere.invalid entry
        "sitemap_bad_lastmod",          # "not-a-date"
        "sitemap_stale",                # 2019 lastmod values
        "state_blob",                   # /products/widget ships a __NEXT_DATA__ blob
        "noindex_on_public_page",       # /blog/post-1
        "thin_content",                 # /pricing and /about
    },
    "must_not_flag_page": {
        # (check_id prefix, url substring) pairs that would be false positives.
        ("thin_content", "/privacy"),
        ("thin_content", "/products"),
        ("no_evidence_genre", "/privacy"),
        ("unstructured_page", "/privacy"),
    },
}
