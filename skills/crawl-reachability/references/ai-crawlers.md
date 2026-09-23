# AI crawlers and what blocking each one costs

The single most useful distinction in `robots.txt` policy is between crawlers
that build a **training corpus** and crawlers that **fetch pages while an
answer is being generated**. They are different decisions with different
consequences, and most sites make them by accident.

| Agent | Operator | Purpose | Blocking it costs |
|---|---|---|---|
| `OAI-SearchBot` | OpenAI | retrieval | Cannot be cited in ChatGPT search answers |
| `ChatGPT-User` | OpenAI | user-fetch | Cannot be fetched when a user follows a link |
| `GPTBot` | OpenAI | training | Absent from future training corpora |
| `Claude-SearchBot` | Anthropic | retrieval | Cannot be cited in Claude search answers |
| `Claude-User` | Anthropic | user-fetch | Cannot be fetched on user request |
| `ClaudeBot` | Anthropic | training | Absent from future training corpora |
| `PerplexityBot` | Perplexity | retrieval | Cannot be cited by the broadest-citing platform |
| `Perplexity-User` | Perplexity | user-fetch | Cannot be fetched on user request |
| `Googlebot` | Google | retrieval | Absent from Search *and* AI Overviews |
| `Google-Extended` | Google | training | Excluded from Gemini training; Search unaffected |
| `Bingbot` | Microsoft | retrieval | Absent from Bing and Copilot |
| `Applebot` | Apple | retrieval | Absent from Siri and Spotlight |
| `Applebot-Extended` | Apple | training | Excluded from Apple model training |
| `CCBot` | Common Crawl | training | Absent from the corpus many models derive from |
| `meta-externalagent` | Meta | training | Absent from Meta model training |
| `Amazonbot` | Amazon | retrieval | Absent from Alexa and Amazon surfaces |

## Why the severity differs

The audit reports a retrieval block as `critical` and a training block as
`medium`, and that gap is deliberate.

arXiv:2604.25707 §6 measures search-trigger rates of **98.64%** (ChatGPT),
**99.67%** (Google AI Overview) and **100.00%** (Perplexity). Retrieval is
attempted on essentially every prompt. A brand that blocks retrieval agents is
unreachable at precisely the moment an assistant is looking for it — the block
takes effect on every single query.

A training block has a slower, more diffuse effect: the model knows less about
the brand unprompted, but the brand can still be found and cited when the
assistant searches. It is also a legitimate policy position that many
organisations hold deliberately.

So the audit treats a training block as a **decision to confirm**, not a defect
to fix, and says so in the finding.

## The common accident

A site adds `Disallow: /` under `GPTBot` intending to opt out of AI training,
then later adds `PerplexityBot` and `ClaudeBot` to the same group. Two of those
three are retrieval agents. The site has now forfeited citation while believing
it only declined training.

`robots_blocks_ai_retrieval` exists to name that specific mistake, which is why
its finding text separates the two purposes explicitly rather than listing
blocked agents undifferentiated.

## Matching semantics

Per RFC 9309, a `User-agent` token matches when it is a **prefix** of the
agent's name, and the *most specific* matching group wins with `*` as fallback.
Consequences worth knowing:

- A group naming `GPTBot` does **not** match `OAI-SearchBot` — different
  product, different token.
- A group naming `Claude` matches `ClaudeBot`, `Claude-User` *and*
  `Claude-SearchBot`, so a training opt-out written that way silently blocks
  retrieval too.
- Consecutive `User-agent` lines form one group sharing one rule set; a rule
  line closes the agent list.

The audit reports which group matched each agent (`matched_group`) and whether
it was named explicitly or fell through to `*` (`explicitly_named`), because a
site that never mentions AI crawlers is in a different situation from one that
named them and chose to block.
