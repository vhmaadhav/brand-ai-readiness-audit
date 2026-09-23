# Threat model for the preflight gate

The audit takes a URL from an untrusted source and fetches it. That makes the
auditing agent a potential confused deputy: it runs somewhere with network
access a caller may not have, and it will follow redirects. This document
states what the gate defends against and what it does not.

## Assets

- The network position of the machine running the audit (often a CI runner or a
  developer laptop with VPN access to internal services).
- Cloud instance credentials reachable from link-local metadata endpoints.
- The report itself, which is rendered and may be shared.

## In scope

### 1. Server-side request forgery to internal services

*Vector.* `http://10.0.0.5/admin`, `http://localhost:8080/`, `http://[fd00::1]/`.

*Defence.* Scheme allowlist; a classification of every literal and every
resolved address that refuses anything not unambiguously public unicast —
loopback, RFC 1918, link-local, multicast, reserved, unspecified, CGNAT
(100.64.0.0/10) and IPv6 ULA.

### 2. Cloud metadata exfiltration

*Vector.* `http://169.254.169.254/latest/meta-data/iam/security-credentials/`,
`http://metadata.google.internal/`, `http://100.100.100.200/`.

*Defence.* Caught by the link-local rule, and additionally named explicitly so
the diagnostic says what was attempted. **These stay blocked even under the
test-only private-address opt-in**, because no fixture legitimately lives there.

### 3. Address obfuscation

*Vector.* Resolvers accept spellings that naive parsers miss:
`http://2130706433/` (decimal), `http://0x7f000001/` (hex),
`http://0177.0.0.1/` (octal) all reach 127.0.0.1.

*Defence.* Bare integers, hex literals and dotted-octal forms are refused
before resolution rather than normalised.

### 4. IPv6 tunnelling of an IPv4 address

*Vector.* `http://[::ffff:127.0.0.1]/`, 6to4 and Teredo forms wrapping a
private IPv4 address inside an IPv6 literal.

*Defence.* IPv4-mapped, 6to4 and Teredo addresses are unwrapped to their
embedded IPv4 address *before* classification.

### 5. Redirect-based bypass and DNS rebinding

*Vector.* A public host returns `302 Location: http://169.254.169.254/`. Or the
hostname resolves to a public address at validation time and a private one at
fetch time.

*Defence.* Redirects are never followed automatically. Every hop is passed back
through the full validator — scheme, host, port, address classification and
scope — before the next request. The chain is capped at 5 hops. This is the
single most important control here, because a first-hop-only check is the
common implementation mistake.

### 6. Credential leakage

*Vector.* `http://user:token@example.com/` causes the fetcher to transmit
credentials, which then land in the report.

*Defence.* Any URL carrying userinfo is refused outright rather than stripped,
so the credential is never transmitted and never appears in output.

### 7. Scope escape

*Vector.* A site links or redirects off-domain and the audit follows, producing
findings about a third party and consuming budget on it.

*Defence.* All fetching is confined to the registrable domain of the final
URL. Off-scope URLs are recorded as evidence (a sitemap listing them is a
finding) but never fetched.

### 8. Resource exhaustion

*Vector.* An endless redirect loop, a multi-gigabyte response, a tarpit that
accepts connections and never responds.

*Defence.* Hard caps on redirect hops, per-page bytes, total bytes, per-request
timeout, page count and total wall-clock. Each is enforced independently.

### 9. Non-HTTP schemes

*Vector.* `file:///etc/passwd`, `gopher://` for protocol smuggling,
`javascript:` where a URL reaches a browser context.

*Defence.* Scheme allowlist of `http` and `https` only.

## Out of scope

- **Malicious content in fetched pages.** Page content is data, never
  instructions. HTML is parsed, never executed; no JavaScript runs; nothing in
  a fetched page can influence what the audit does next. Text extracted from a
  page appears in the report as quoted evidence.
- **Denial of service against the audited site.** Mitigated by rate limiting,
  crawl-delay compliance and small page budgets, but the gate is not a defence
  against a caller who runs many audits deliberately.
- **Authenticated-area testing.** Out of scope by design; the audit never
  authenticates.
- **Public Suffix List accuracy.** Scope confinement uses a best-effort eTLD+1
  without a PSL dependency. Errors are toward a *narrower* scope, which is the
  safe direction.

## The test-only opt-in

`GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES=1` relaxes the private-address rules so the
test fixture on `127.0.0.1` can be audited. Properties that make this
acceptable:

1. Off by default, requiring an environment variable whose name states exactly
   what it does.
2. Cloud metadata endpoints remain blocked regardless.
3. Every `ValidatedUrl` produced under it carries a loud note that travels into
   the report, so a run made this way is self-identifying.
4. `TestNetguard` runs without it and asserts all 25 attack vectors are still
   refused.
