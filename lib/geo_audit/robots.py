"""robots.txt parsing with per-agent group visibility.

``urllib.robotparser`` answers "may I fetch this URL", which is what the crawler
needs. The audit additionally needs to know *which* agent groups exist and what
each one allows, because "this site blocks GPTBot but allows Googlebot" is a
finding in its own right — and a materially different one from "this site
blocks everything".

Grammar follows RFC 9309 (Robots Exclusion Protocol).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Crawlers that feed AI assistants. Split by function, because blocking a
# training crawler and blocking a live-retrieval crawler have very different
# consequences: the first affects what a model knows, the second affects
# whether the brand can be cited in an answer being generated right now.
AI_CRAWLERS: dict[str, dict[str, str]] = {
    "GPTBot":            {"operator": "OpenAI",     "purpose": "training",   "impact": "medium"},
    "OAI-SearchBot":     {"operator": "OpenAI",     "purpose": "retrieval",  "impact": "critical"},
    "ChatGPT-User":      {"operator": "OpenAI",     "purpose": "user-fetch", "impact": "critical"},
    "ClaudeBot":         {"operator": "Anthropic",  "purpose": "training",   "impact": "medium"},
    "Claude-User":       {"operator": "Anthropic",  "purpose": "user-fetch", "impact": "critical"},
    "Claude-SearchBot":  {"operator": "Anthropic",  "purpose": "retrieval",  "impact": "critical"},
    "PerplexityBot":     {"operator": "Perplexity", "purpose": "retrieval",  "impact": "critical"},
    "Perplexity-User":   {"operator": "Perplexity", "purpose": "user-fetch", "impact": "critical"},
    "Google-Extended":   {"operator": "Google",     "purpose": "training",   "impact": "medium"},
    "Googlebot":         {"operator": "Google",     "purpose": "retrieval",  "impact": "critical"},
    "Bingbot":           {"operator": "Microsoft",  "purpose": "retrieval",  "impact": "critical"},
    "Applebot":          {"operator": "Apple",      "purpose": "retrieval",  "impact": "high"},
    "Applebot-Extended": {"operator": "Apple",      "purpose": "training",   "impact": "medium"},
    "CCBot":             {"operator": "Common Crawl", "purpose": "training", "impact": "medium"},
    "meta-externalagent": {"operator": "Meta",      "purpose": "training",   "impact": "medium"},
    "Amazonbot":         {"operator": "Amazon",     "purpose": "retrieval",  "impact": "medium"},
}

# Retrieval and user-fetch agents are the ones that decide whether a brand can
# be cited in a live answer, which is what the audit is about.
LIVE_RETRIEVAL_AGENTS = [
    name for name, meta in AI_CRAWLERS.items()
    if meta["purpose"] in ("retrieval", "user-fetch")
]

# The product token this crawler answers to. It must match the token in
# fetcher.USER_AGENT, because the only honest way to read a robots.txt is as
# the agent you actually present yourself as. Asking "may Googlebot fetch this"
# while sending a different User-Agent reads a permission that was granted to
# somebody else -- and "block everyone, allow Googlebot" is a common enough
# configuration that the difference is not academic.
OWN_USER_AGENT_TOKEN = "BrandAIReadinessAudit"

_DIRECTIVE_RE = re.compile(r"^\s*([A-Za-z-]+)\s*:\s*(.*?)\s*$")


@dataclass
class AgentGroup:
    """One ``User-agent`` group and its rules."""

    agents: list[str] = field(default_factory=list)
    allows: list[str] = field(default_factory=list)
    disallows: list[str] = field(default_factory=list)
    crawl_delay: float | None = None

    def blocks_everything(self) -> bool:
        return any(rule == "/" for rule in self.disallows) and not self.allows


@dataclass
class RobotsPolicy:
    """A parsed robots.txt, or the absence of one."""

    exists: bool
    status: int | None = None
    url: str = ""
    raw: str = ""
    groups: list[AgentGroup] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    fetch_error: str | None = None

    # ------------------------------------------------------------------
    # Rule matching
    # ------------------------------------------------------------------

    def group_for(self, agent: str) -> AgentGroup | None:
        """Return the group governing *agent*, per RFC 9309 precedence.

        The most specific matching user-agent wins; ``*`` is the fallback and
        is used only when no named group matches.
        """
        agent_l = agent.lower()
        best: AgentGroup | None = None
        best_len = -1
        wildcard: AgentGroup | None = None

        for group in self.groups:
            for token in group.agents:
                token_l = token.lower()
                if token_l == "*":
                    if wildcard is None:
                        wildcard = group
                    continue
                # A robots token matches if it is a prefix of the agent name.
                if agent_l.startswith(token_l) and len(token_l) > best_len:
                    best, best_len = group, len(token_l)
        return best or wildcard

    def can_fetch(self, agent: str, path: str) -> bool:
        """Longest-match wins; on an exact tie, Allow beats Disallow."""
        group = self.group_for(agent)
        if group is None:
            return True

        best_len, verdict = -1, True
        for rule in group.disallows:
            if _path_matches(rule, path) and len(rule) > best_len:
                best_len, verdict = len(rule), False
        for rule in group.allows:
            if _path_matches(rule, path) and len(rule) >= best_len:
                best_len, verdict = len(rule), True
        return verdict

    def crawl_delay_for(self, agent: str) -> float | None:
        group = self.group_for(agent)
        return group.crawl_delay if group else None

    def ai_crawler_report(self, path: str = "/") -> dict[str, dict]:
        """How each known AI crawler is treated at *path*."""
        report: dict[str, dict] = {}
        for name, meta in AI_CRAWLERS.items():
            group = self.group_for(name)
            matched = None
            if group:
                for token in group.agents:
                    if token.lower() != "*" and name.lower().startswith(token.lower()):
                        matched = token
                        break
                if matched is None and any(t == "*" for t in group.agents):
                    matched = "*"
            report[name] = {
                **meta,
                "allowed": self.can_fetch(name, path),
                "matched_group": matched,
                "explicitly_named": matched is not None and matched != "*",
                "crawl_delay": self.crawl_delay_for(name),
            }
        return report


def _glob_matches(pattern: str, text: str) -> bool:
    """Whether ``*``-wildcard *pattern* matches the whole of *text*.

    Deliberately not a regex. Compiling a rule into a chain of ``.*`` groups and
    handing it to :mod:`re` backtracks catastrophically: a thirty-character
    ``Disallow`` line of the form ``/*a*a*a...*b$`` runs for minutes against an
    ordinary URL. robots.txt is fetched from the site under audit, so that line
    is attacker-controlled, and the audit's budgets only bound network time --
    not CPU spent inside the matcher.

    This walks the literal segments between wildcards with ``str.find``, which
    is linear per segment and runs in C. Taking the leftmost occurrence of each
    segment is optimal for pure ``*`` globs -- an earlier match never leaves
    less room for the segments that follow -- so greedy scanning is exact here,
    not an approximation.
    """
    segments = pattern.split("*")
    if not text.startswith(segments[0]):
        return False
    position = len(segments[0])

    for segment in segments[1:-1]:
        if not segment:
            continue
        found = text.find(segment, position)
        if found < 0:
            return False
        position = found + len(segment)

    tail = segments[-1]
    if not tail:
        return True
    # The final segment has to land at the very end, and must not overlap what
    # earlier segments already consumed.
    return len(text) - len(tail) >= position and text.endswith(tail)


def _path_matches(rule: str, path: str) -> bool:
    """RFC 9309 path matching with ``*`` wildcards and ``$`` end-anchor."""
    if rule == "":
        return False
    anchored = rule.endswith("$")
    if anchored:
        rule = rule[:-1]
    if "*" not in rule:
        return path == rule if anchored else path.startswith(rule)
    # An unanchored rule matches any path it is a *prefix* pattern of, which is
    # the same as an anchored match once a trailing wildcard is appended.
    return _glob_matches(rule if anchored else rule + "*", path)


def parse_robots(text: str, *, url: str = "", status: int | None = 200) -> RobotsPolicy:
    """Parse robots.txt content into groups, sitemaps and errors."""
    policy = RobotsPolicy(exists=True, status=status, url=url, raw=text)
    current: AgentGroup | None = None
    # Consecutive User-agent lines form one group; a rule line closes the
    # agent list so the next User-agent starts a fresh group.
    accepting_agents = False

    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = _DIRECTIVE_RE.match(line)
        if not match:
            policy.parse_errors.append(f"line {lineno}: unparseable {line.strip()!r}")
            continue

        field_name, value = match.group(1).lower(), match.group(2)

        if field_name == "user-agent":
            if current is None or not accepting_agents:
                current = AgentGroup()
                policy.groups.append(current)
                accepting_agents = True
            current.agents.append(value)
            continue

        if field_name == "sitemap":
            if value:
                policy.sitemaps.append(value)
            continue

        if current is None:
            policy.parse_errors.append(
                f"line {lineno}: {field_name!r} appears before any User-agent group"
            )
            continue

        accepting_agents = False
        if field_name == "disallow":
            current.disallows.append(value)
        elif field_name == "allow":
            current.allows.append(value)
        elif field_name in ("crawl-delay", "crawldelay"):
            try:
                current.crawl_delay = float(value)
            except ValueError:
                policy.parse_errors.append(f"line {lineno}: non-numeric crawl-delay {value!r}")

    return policy


def robots_url_for(origin: str) -> str:
    parts = urlsplit(origin)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def missing_policy(url: str, status: int | None, error: str | None = None) -> RobotsPolicy:
    """A robots.txt that was absent or unreadable.

    Per RFC 9309 an unavailable robots.txt means unrestricted crawling, so the
    audit proceeds — but a 5xx is treated as restrictive by well-behaved
    crawlers, and that distinction is recorded for the reachability layer.
    """
    return RobotsPolicy(exists=False, status=status, url=url, fetch_error=error)
