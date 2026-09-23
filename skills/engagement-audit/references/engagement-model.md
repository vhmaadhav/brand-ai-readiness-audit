# The engagement model

## The arrival this layer is about

A visitor from an AI answer does not arrive like a search visitor:

- **Mid-funnel.** They land on the deep page that answered the question, not
  the homepage. The assistant cited the page with the answer.
- **Partially informed.** They already have an answer. They are here to verify
  it, or to act on it.
- **Without accumulated context.** They did not pass through the navigation,
  the homepage positioning, or the category page on the way in.

So a deep page that reads as a *fragment of a journey* — assuming you know what
the product is, who the company is, and what the previous page said — fails
this visitor even when the site works fine for someone who entered at the top.

Handout appendix E is relevant here too: assistants tailor answers to who is
asking, so a page cannot rely on a scripted arrival. It has to stand on its own
for a visitor it did not choose.

## Checks and what each is really asking

### `EN001` — does the opening answer, or sell?

Whether the page states its core claim in roughly the first 150 words, or
buries it under hero copy. A visitor arriving to *verify* an answer needs to
confirm it in seconds; a hero that repeats the brand promise does not confirm
anything.

### `EN002` — does a deep page stand alone?

The central check of this layer. Signals: does the page name what the product
or company *is*, or assume it? Does it define its own terms? Does it provide
orientation without requiring a trip to the homepage?

### `EN003` — interstitials

Cookie walls, newsletter modals and app prompts present in the served HTML
before any interaction. For a visitor who arrived to check one fact, an
interstitial is friction placed in front of the only thing they came for.

### `EN004` — heading hierarchy

Missing `h1`, or skipped levels. A scannability proxy, and a direct level-1
observation: a visitor scanning for the part that answers their question needs
the page to be navigable at a glance.

### `EN005` — substance versus chrome

Handout appendix F: when the real substance is surrounded by low-value filler,
a summariser has little to work with and the important part disappears. The
same dilution affects a human skimming for one fact.

### `EN006` — substance carried only in images

A claim, a price or a spec that exists only inside an image with no alt text is
invisible to a screen reader, a summariser, and anyone on a slow connection.
Appendix F's failure mode, in its most literal form.

### `EN007` — form friction

How much a primary conversion form asks for before giving anything back. A
visitor who arrived from an answer has low investment in the brand; a ten-field
form is priced for someone who has already decided.

### `EN008` — dead ends

Pages offering no next step beyond site-wide navigation. Needs at least three
pages to separate site chrome from page-specific links, and **skips itself with
a stated note below that threshold** rather than guessing.

### `EN009` — perceived speed and stability

Render-blocking resources in `<head>`, images without intrinsic dimensions.

**This is explicitly a structural proxy, not a measurement**, and is labelled as
such in the finding. Core Web Vitals cannot be computed from raw HTML: LCP, INP
and CLS all require a rendering engine, and field data requires real users.
What *is* observable is the structural conditions associated with poor LCP and
CLS. Reporting a fabricated performance score from static HTML would be exactly
the confident-and-wrong finding this marketplace is built to avoid.

## Why claim levels run high here

Most engagement checks are level 3, capped at `medium`. That is honest rather
than timid.

The upstream layers observe machine-checkable facts: a status code is 404 or it
is not; a JSON-LD block parses or it does not. Engagement checks observe
*proxies for human behaviour*. "This page sells rather than answers" is a
defensible reading of the opening 150 words, not a measurement of what visitors
did — the audit has no analytics, no session recordings and no bounce rate.

`EN004` (missing `h1`) and `EN007` (form field count) are level 1, because
those are direct counts.

The report should read as "here is a structural reason a visitor might not
stay", not "here is why your bounce rate is high". The claim-level cap is what
enforces that distinction mechanically instead of relying on careful wording.
