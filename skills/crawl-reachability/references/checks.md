# Reachability checks in detail

## Discovery

Ordered by reliability, stopping when enough candidates are found:

1. `Sitemap:` directives in `robots.txt` — authoritative, since the site
   nominated them.
2. Conventional paths — `/sitemap.xml`, `/sitemap_index.xml`,
   `/sitemap-index.xml`, `/wp-sitemap.xml` and variants.
3. Sitemap index expansion — at most three indexes, five children each, to
   bound the cost on very large sites.
4. Bounded homepage link crawl — only when the above yields fewer than 12 URLs.

`/llms.txt` is probed but never required; its absence is a proactive
suggestion, not a defect.

Skipped without spending budget: non-HTML extensions, and paths matching
`wp-admin`, `cdn-cgi`, `_next/static`, `assets`, `static`, `login`, `cart`,
`checkout`, `account`, `admin`, `api`.

## Sampling

Candidates are bucketed by URL shape into homepage, product, pricing, article,
docs, about, service, legal, category and other, then selected **round-robin
across buckets**.

This matters more than it looks. The audit makes population claims ("product
pages lack structured data"), and a naive first-N sample from a sitemap ordered
by publication date returns twenty blog posts and no product page. The sample
must span page *types* for a type-level claim to mean anything. The homepage is
always included first.

## Sitemap audit

Against the sitemaps.org 0.9 protocol:

| Check | Rule |
|---|---|
| Namespace | Must be `http://www.sitemaps.org/schemas/sitemap/0.9` |
| Structure | `<loc>` must be wrapped in `<url>` |
| Size | ≤50,000 URLs and ≤50 MB uncompressed per file |
| `lastmod` | Must be a valid W3C datetime |
| Scheme | Must match the scheme the site actually serves |
| Scope | Entries must be under the host serving the sitemap |
| Fragments | Never meaningful; fragments are not sent to a server |
| Liveness | A sample of entries is checked for 4xx/5xx and redirects |
| Orphans | Crawled pages absent from the sitemap |
| Freshness | Median `lastmod` age, future dates, all-identical values |

Two of these deserve comment.

**Future-dated `lastmod`** is treated as `medium` rather than `low` because
crawlers commonly respond by ignoring the field across the entire file, so one
timezone bug can disable the signal site-wide.

**All-identical `lastmod`** (requiring ≥10 dated entries) means the field is
generated at build time rather than derived from content changes. It is
technically valid and carries no information, so it is `low` — worth fixing,
not worth alarm.

The staleness check requires **≥5 dated entries** before calling a sitemap
stale. Below that the sample cannot support the claim.

## Render faults

No JavaScript is executed. Faults are diagnosed from the served HTML, which is
the point: that is what a non-rendering crawler receives.

| Fault | Signal | Recoverable | Fix |
|---|---|---|---|
| `empty_shell` | Empty mount point, <60 visible words, nothing in payload | no | Server-side render or prerender |
| `state_blob` | <120 words but ≥500 chars of readable text in a state payload | **yes** | Hydrate the payload into markup |
| `script_heavy_shell` | <120 words, inline script >4× text, nothing recoverable | no | Server-side render |
| `thin_served_html` | <120 words and text <5% of served bytes | no | Ensure content is in the response |
| `meta_only` | Metadata renders, zero body paragraphs | no | Render body alongside metadata |
| `lazy_content` | ≥3 skeleton markers, <200 words | no | Render above-the-fold content directly |
| `client_routing` | Hash-fragment routes plus a client router | no | Move to path-based URLs |
| `noscript_gap` | Framework present, no `<noscript>`, <150 words | no | Provide a fallback |

### Why `state_blob` is diagnosed before `empty_shell`

Both look identical to a browser-free reader, and a naive classifier reports
whichever it checks first. But the fixes differ by an order of magnitude of
effort:

- `empty_shell` — the content never left the server. Fixing it means changing
  the rendering strategy for that route.
- `state_blob` — the content *already ships with the page*, inside
  `__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__` or similar. It simply is not
  addressable as markup. Fixing it means emitting text that is already present.

So the classifier measures how much readable text is recoverable from inline
JSON payloads first, and prefers `state_blob` when that text is substantial.
The finding then reports the recoverable character count, so the reader can see
the fix is cheaper than it looks.

`meta_only` is suppressed when a shell fault already fired, since it would be a
weaker restatement of the same problem.

## Indexability

`page_unreachable`, `noindex_on_public_page` (both `<meta name="robots">` and
the `X-Robots-Tag` header), `canonical_mismatch`, `soft_404` (200 status with
not-found content), and `slow_response` (median over 3s, a level-2 contrast
since a single slow sample proves nothing).
