# Schema requirements by page type

Type appropriateness is checked against what a page type is *expected* to
declare, never against a universal list. Flagging a privacy policy for lacking
`Product` schema is the most common false positive in structured-data auditing,
and it teaches the reader to ignore the report.

| Page type | Expected types | Enforced? |
|---|---|---|
| homepage | `Organization`, `WebSite`, `LocalBusiness`, `Corporation` | yes |
| product | `Product`, `Offer`, `AggregateOffer`, `ItemList` | yes |
| pricing | `Product`, `Offer`, `PriceSpecification`, `Service` | yes |
| article | `Article`, `BlogPosting`, `NewsArticle`, `TechArticle` | yes |
| docs | `TechArticle`, `HowTo`, `Article`, `APIReference` | yes |
| service | `Service`, `Product`, `Offer` | yes |
| about | `Organization`, `AboutPage`, `Corporation` | soft |
| category | `CollectionPage`, `ItemList`, `BreadcrumbList` | soft |
| legal | none | **exempt** |
| other | `WebPage` | soft |

"Soft" means a missing type is reported at low severity or not at all; "exempt"
means the check does not run.

## Required properties for common types

Checked only for types the page actually declares. A missing required property
on a declared type is a stronger finding than a missing type, because the site
has already decided the type applies.

| Type | Required | Strongly recommended |
|---|---|---|
| `Organization` | `name` | `url`, `logo`, `description`, `sameAs`, `contactPoint` |
| `Product` | `name` | `offers` or `aggregateRating`, `description`, `image`, `brand`, `sku` |
| `Offer` | `price`, `priceCurrency` | `availability`, `url`, `priceValidUntil` |
| `Article` | `headline` | `datePublished`, `author`, `image`, `dateModified` |
| `HowTo` | `name`, `step` | `totalTime`, `supply`, `tool` |
| `FAQPage` | `mainEntity` | — (see the note below) |
| `BreadcrumbList` | `itemListElement` | — |
| `LocalBusiness` | `name`, `address` | `telephone`, `openingHours`, `geo` |

### A note on `FAQPage`

Valid `FAQPage` markup is checked for structural correctness like any other
type. It is **never recommended as a fix**, because arXiv:2604.25707 §8.2
measures Q&A formatting at −5.74% mean influence — the only evidence genre in
the study with a negative effect. If a site already has `FAQPage` markup, the
audit checks it parses; it does not suggest adding it, and `evidence-density`
separately flags Q&A pages carrying no evidence underneath.

## Why parse failures outrank absence

`SD002` (malformed JSON-LD) is rated `high`, above `SD001` (no structured data
at all). The reasoning:

- A page with **no** JSON-LD is *ambiguous*. A consumer falls back to reading
  the text, which may work.
- A page with **malformed** JSON-LD is *silently discarded*. Every consumer
  drops the block, and the site believes it has structured data when it has
  none. Nobody is looking for the problem because the markup is right there in
  the source.

The second failure is worse precisely because it is invisible from the inside.

## Graph traversal

JSON-LD is walked through `@graph` containers and nested objects, so an
`Organization` declared inside a `WebPage`'s `publisher` property is found.
Traversal is capped at 500 nodes to bound cost on pathological documents.

Microdata (`itemtype`) and RDFa (`typeof`) are collected as a fallback signal.
A site using only microdata is reported at `low` severity and claim level 3 —
microdata is valid and widely consumed; JSON-LD is simply easier to parse
reliably and is the format search engines document a preference for. Calling it
a defect would overstate what is known.
