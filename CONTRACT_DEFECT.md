# Upstream contract defect: redirect-URI prefix allowlist is bypassable

**Status:** open — needs to be raised against
[`carpenike/mcp-as-contract`](https://github.com/carpenike/mcp-as-contract)
(pinned here at `v1.1.0`). This file documents the defect and the local
mitigation so the fix can be lifted upstream and applied to the sibling
conformers (`replog`, `whiskey-whiskey-whiskey`, `marginalia`).

## What the contract specifies

`contract.json` → `dcr.redirect_uri_policy = "allowlist-filter"` with
`dcr.redirect_uri_allowlist_prefixes`, and `CONTRACT.md` §1.4, mandate a
**prefix** allowlist containing, among others:

```
http://127.0.0.1:
http://localhost:
```

`CONTRACT.md` §1.4 justifies the trailing punctuation with:

> loopback entries keep the trailing `:` or `/` so `http://127.0.0.1.evil.com/`
> can't pass a naive prefix test

## Why that justification is incomplete

The reasoning defends only against the **subdomain** trick
(`127.0.0.1.evil.com`). It does not defend against the URL **userinfo**
trick. A bare `startswith()` against a prefix that ends in `:` is bypassable:

```python
"http://localhost:1234@evil.com/cb".startswith("http://localhost:")  # True
"http://127.0.0.1:9@evil.com/cb".startswith("http://127.0.0.1:")     # True
```

In both URLs the real host is `evil.com` — `localhost:1234` / `127.0.0.1:9`
parse as the URL's *userinfo* (`user:password@host`), not the host. The
prefixes that end in `/` (`https://claude.ai/`, `http://localhost/`, …) are
**not** affected, because the required `/` cannot be satisfied by a userinfo
segment. Only the two `:`-terminated loopback prefixes are exploitable.

## Impact

Any AS that implements `allowlist-filter` with a literal `startswith` prefix
match (the natural reading of the contract) has an open redirect on its
authorization endpoint, which escalates to **victim-identity token
exfiltration**:

1. Attacker self-registers a client via the unauthenticated `/oauth/register`
   with `redirect_uris: ["http://127.0.0.1:0@evil.com/cb"]` (stored verbatim —
   it passes the prefix filter) and an attacker-chosen PKCE challenge.
2. Attacker sends the victim an `/oauth/authorize?...redirect_uri=<that URI>`
   link. The victim authenticates to PocketID.
3. The AS mints an authorization code bound to the **victim's** identity and
   302-redirects it to `http://127.0.0.1:0@evil.com/cb?code=…` — delivering
   the code to `evil.com`.
4. Attacker redeems the code at `/oauth/token` (they hold the client_id,
   client_secret, redirect_uri, and PKCE verifier) and receives a token
   asserting the victim's `sub`/`email`.

Because all four `carpenike` apps derive their allowlist from this contract,
all four are potentially exposed.

## Local mitigation (this repo) — contract-compatible

`src/homelab_mcp/oauth_provider.py::_redirect_allowed` no longer does a bare
`startswith`. It first parses the candidate URL and **rejects any target
carrying userinfo** (`parts.username`/`parts.password`/`@` in netloc) or a
malformed scheme/host, then applies the prefix check. No legitimate client
redirect_uri carries userinfo, so:

- every allowlisted prefix the contract intends still matches, and
- the conformance harness (`conformance/check.sh`) still passes — it only
  drives a DCR round-trip with a valid Claude URI and never exercises the
  malicious case.

So the local server is hardened **without diverging from conformance**. See
`tests/test_oauth_security.py::test_redirect_allowed_*` and
`test_dcr_drops_userinfo_bypass_uri`.

## Recommended upstream fix

Amend `pocketid-mcp-as` so the redirect-URI policy is specified as a
**parsed** match, not a string prefix:

- reject any redirect_uri containing userinfo;
- match on `scheme` + `host` + `port` (+ path prefix where one is given),
  not raw `startswith`;
- add a conformance case asserting `http://localhost:1@evil.com/cb` is
  rejected at DCR.

Then bump the pin here and drop this note.
