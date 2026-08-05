# homelab-mcp

A small MCP server that exposes homelab APIs (cooklang recipes, gatus uptime monitoring, grocy household management) as
tools that Claude can call. Deployed on forge. Runs its own OAuth 2.1 Authorization Server
that federates user logins to PocketID.

**Status:** v0.2 — embedded OAuth provider (replaces v0.1's Cloudflare Access dependency).

## What this is

An [MCP](https://modelcontextprotocol.io) server speaking the
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports).
It runs on the homelab, exposes a handful of tools wrapping internal APIs, mints its own
RS256 JWTs via an embedded OAuth 2.1 Authorization Server, and validates every request
against those JWTs before dispatching.

**One server, many tool categories.** Each category lives in its own module under
`src/homelab_mcp/tools/`. Adding a new category is dropping a file there; the registry
auto-discovers it. No central wiring file to update.

## Why a custom OAuth provider?

The MCP custom-connector spec (RFC 9728 + RFC 8414 + RFC 7591) requires the resource server
to advertise an authorization server that supports:

  - **Dynamic Client Registration (DCR)** — Claude registers itself without operator action.
  - **PKCE-protected authorization-code grant** — standard OAuth 2.1.
  - **Refresh tokens** — the token endpoint also supports the `refresh_token` grant and hands
    out a (rotating) refresh token with every access token, so clients renew expired access
    tokens silently instead of re-running the interactive login. Access tokens default to a
    24h lifetime (`HOMELAB_MCP_OAUTH_ACCESS_TOKEN_LIFETIME_SECONDS`); refresh tokens default
    to 30 days (`HOMELAB_MCP_OAUTH_REFRESH_TOKEN_LIFETIME_SECONDS`).
  - **Restart-survivable sessions** — registered clients and refresh tokens are persisted to a
    small SQLite store (`HOMELAB_MCP_OAUTH_STATE_DB_PATH`, default `/var/lib/homelab-mcp/state.db`),
    so a service restart or redeploy doesn't force a re-login. Refresh tokens are stored as
    SHA-256 hashes (never plaintext) and are revocable by deleting their row. Set the path to
    `:memory:` to opt out (clients re-register and users re-authenticate on every restart).
  - **Bounded DCR growth** — abandoned clients (no live refresh token, older than
    `HOMELAB_MCP_OAUTH_CLIENT_RETENTION_SECONDS`, default 90d) are pruned at startup and on each
    registration, and the unauthenticated `/oauth/register` endpoint is rate-limited per source IP
    (`HOMELAB_MCP_OAUTH_REGISTER_RATE_LIMIT_MAX` per `…_RATE_WINDOW_SECONDS`, default 30/hour) so
    the persisted client table can't grow without bound.
  - **Spec-compliant metadata** — field names like `grant_types_supported = ["authorization_code"]`,
    not Cloudflare's `["authorization_code_with_pkce"]`.

Neither of the obvious off-the-shelf options work:

  - **PocketID** doesn't implement DCR.
  - **Cloudflare Access for SaaS (OIDC)** returns non-standard field names in its discovery
    doc, which Claude silently rejects.

So we run our own spec-clean OAuth AS in-process and federate the actual user login (passkey)
upstream to PocketID. Claude never touches PocketID directly.

## Contract conformance

This server **conforms to [pocketid-mcp-as](https://github.com/carpenike/mcp-as-contract)
v1.2, profile `jwt-refresh`, scope `mcp-only`, MCP path `/mcp`.**

`pocketid-mcp-as` is the shared contract for the self-hosted MCP OAuth 2.1
Authorization Servers that federate login to PocketID across several
carpenike apps (`replog`, `whiskey-whiskey-whiskey`, `marginalia`, and this
one). It standardizes the discovery field names, OAuth wire behavior, and
discovery documents — not the token storage model, and (since v1.1) not the
MCP resource path, which is app-declared. This app uses the `jwt-refresh`
profile (RS256 access tokens + rotating refresh tokens, publishes
`jwks_uri`), the `mcp-only` scope posture (the minted token is accepted only
on the `/mcp` resource path), and keeps its original `/mcp` transport path.
The path-suffixed RFC 9728 §3.3 PRM, its `resource`, and the §1.7
`WWW-Authenticate` hint are all derived from the single `mcp_path` setting.
(v1.2 added a redirect-URI hardening rule — parsed scheme+host+port match
plus mandatory userinfo rejection — that this server originally reported and
already satisfies.)

Run the upstream conformance harness against a live AS. It's cloned fresh at
the pinned ref (`contract/PINNED.json`) and run **unpatched** with the path
flag — the v1.2.0 harness adds the redirect-URI userinfo-bypass probe and the
§1.7 challenge check, and we no longer vendor or patch it:

```bash
make conformance ORIGIN=http://127.0.0.1:9200     # local dev
make conformance ORIGIN=https://mcp.holthome.net  # production
# which clones the pinned tag and runs:
#   conformance/check.sh <origin> jwt-refresh mcp-only --mcp-path /mcp
```

CI boots the server and runs both the upstream harness and the drift guard
on every push (`make conformance-ci`).

### Hosting the contract (mcp.holthome.net is its public home)

This host serves the canonical public copy of the contract so other repos'
build/CI harnesses can fetch the spec at runtime. Both routes are
unauthenticated, GET-only, CORS-open (`Access-Control-Allow-Origin: *`), and
live entirely outside the OAuth/bearer path:

| Route | Serves | Headers |
|-------|--------|---------|
| `/.well-known/mcp-as-contract.json` | machine-readable `contract.json` | `application/json`, `Cache-Control: public, max-age=300`, `X-Contract-Version` |
| `/contract` | human-readable `CONTRACT.md` (raw) | `text/markdown`, same cache + version headers |

**GitHub is the single source of truth — we don't commit the contract into
this tree.** The content is fetched at wheel-build time
([`hatch_build.py`](hatch_build.py)) from the ref pinned in
[`contract/PINNED.json`](contract/PINNED.json) and force-included into the
wheel as package data, so the running server is self-contained (no runtime
GitHub dependency) but the source carries no copy (the fetched files under
`contract/` are gitignored). Bumping the pin is a deliberate, reviewable
step:

```bash
make contract-pull REF=v1.2.0   # update the pinned ref; review PINNED.json diff
```

The drift guard is **upstream-aware**: CI fetches `contract.json` from the
pinned tag on GitHub and asserts the live-served bytes deep-equal it
(served == upstream@pinned), so a serving bug *or* a stale pin is caught.

## Tools

Tool names follow `<category>_<verb>_<object>`. Every tool returns a
structured `{"error": {code, message, hint}}` payload on failure (never
raises), and list-shaped tools report `{returned, total, truncated}`.

| Category | Tool name | What it does |
|----------|-----------|--------------|
| Cooklang | `cooklang_list_recipes` | Browse/search your canonical cookbook (cook.holthome.net) with optional course/cuisine/tag/free-text filters and opt-in `match_ingredients` ranking (absorbs the old separate search tool) |
| Cooklang | `cooklang_get_recipe` | Fetch one recipe's metadata + ingredients/cookware/steps AND its raw `.cook` `source`, by slug or path |
| Cooklang | `cooklang_create_recipe` | Author a NEW `.cook` (frontmatter + body); `derived_from` is first-class; fails on collision |
| Cooklang | `cooklang_update_recipe` | Amend an existing recipe (parser-validated before overwrite); `body` optional for metadata-only edits; can move/rename via `new_folder`/`new_slug` |
| Cooklang | `cooklang_delete_recipe` | Permanently delete a recipe; previews the target unless `confirm=true` |
| Cooklang | `cooklang_search_federation` | Search the federated index (your repo + ~60 community feeds) |
| Cooklang | `cooklang_build_shopping_list` | Combine ingredients across multiple of YOUR recipes, grouped by store aisle |
| Homelab | `homelab_list_status` | Snapshot of all monitored endpoints via gatus |
| Homelab | `homelab_get_endpoint_history` | Recent check history for one specific endpoint |
| School | `school_list_children` | Children tracked by the Schoology sync, with course counts + last sync |
| School | `school_list_courses` | Active courses with teacher, period and current grade |
| School | `school_get_upcoming_work` | Assignments due in the next N days ("who has what due this week") |
| School | `school_get_missing_work` | Missing/overdue work, separating Schoology-reported from inferred-past-due |
| School | `school_get_grades` | Current grades, or the full trajectory with `since` |
| School | `school_get_assignment` | One assignment plus its state history and teacher-edit trail |
| School | `school_get_announcements` | Recent teacher/school activity-feed posts |
| School | `school_list_staff` | Faculty lookup by name, with the courses they teach these children |
| School | `school_get_sync_status` | Per-source sync health + staleness (call before saying "nothing is due") |
| Amazon | `amazon_match_charges` | Batch: bank charges in, the orders and line items behind them out, with a confidence and a reason per charge |
| Amazon | `amazon_get_order` | One order in full: items, totals breakdown, shipments, recipient |
| Amazon | `amazon_search_items` | Full-text over purchased item titles ("when did we last buy furnace filters") |
| Amazon | `amazon_list_orders` | Browse a date window without one call per order |
| Amazon | `amazon_get_sync_status` | Per-account/source sync health, staleness, and which months were actually fetched |
| Grocy | `grocy_stock_item` | Keystone walkthrough tool: find-or-create a product then `set`/`add`/`consume`/`open` in one call (by name, id, or `barcode`); price + store on `add`; `create_new` forces a new product past disambiguation |
| Grocy | `grocy_find_products` | Find products by name across ALL master data ("do we have X?") |
| Grocy | `grocy_attention` | Planning feed: `kind='expiring'` (due soon / overdue / expired) or `kind='below_minimum'` (quantity-driven restock), summarized (absorbs the old expiring + restock tools) |
| Grocy | `grocy_stock_by_location` | On-hand stock grouped by storage location, or all stock when no location given (absorbs the old list-stock tool) |
| Grocy | `grocy_product_card` | Enriched product detail: on-hand, min/below-min, price, shelf life, locations |
| Grocy | `grocy_consumption_history` | Burn rate from the stock log (purchased/consumed/spoiled + rates); flags truncation |
| Grocy | `grocy_stock_value` | Total inventory value, optionally by location + top-N products |
| Grocy | `grocy_convert_units` | Convert an amount between units (product-specific → global → identity); lists defined conversions when no path exists |
| Grocy | `grocy_set_unit_conversion` | Upsert a unit conversion (product-specific or global); write one direction |
| Grocy | `grocy_ensure` | Idempotently create a `kind='location'`/`'unit'`/`'store'` (store takes an optional `address` userfield) — absorbs the three old ensure tools |
| Grocy | `grocy_seed_defaults` | One-shot bootstrap of default locations + units (idempotent) |
| Grocy | `grocy_health` | Connectivity + Grocy version check |
| Home Assistant | `ha_list_entities` | Find entities by domain and/or free-text search (id + friendly name) — the "never guess an entity_id" tool |
| Home Assistant | `ha_get_state` | One entity's full state + attributes + availability; the re-check tool when a device "didn't respond" |
| Home Assistant | `ha_get_history` | One entity's recent state changes over the last N hours |
| Home Assistant | `ha_call_service` | Closed-loop actuation: allowlisted domains only, `confirm=true` gate on high-impact domains, and an observed before/after read-back — `confirmed` means the entity actually transitioned, never just "HA accepted the call" |
| Home Assistant | `ha_list_automations` | Automations with enabled state, last-triggered, and whether they're editable via the config API (id) or YAML/git-managed |
| Home Assistant | `ha_get_automation` | One automation's full config via HA's config API (admin token required) |
| Home Assistant | `ha_upsert_automation` | Create/update an automation through HA's validated + hot-reloading config API; previews the diff unless `confirm=true` |
| Home Assistant | `ha_check_config` | HA's own full configuration check (Developer Tools → Check configuration) |
| Home Assistant | `ha_health` | Connectivity + HA version check |
| ARC Raiders | `arc_search_items` | Search the item database (weapons, ammo, gear, materials) by name — type, rarity, value, workbench, non-zero stats (MetaForge) |
| ARC Raiders | `arc_search_quests` | Search quests by name — giver, objectives, XP, required turn-in items, rewards, guide link (MetaForge) |
| ARC Raiders | `arc_check_item_keep` | Keep/sell/recycle verdict: quest + hideout + expedition-project demand (summed `keep_quantity`), recycle/salvage outputs with value delta, damaged/intact variant flags, trader offers, and a `coverage` honesty field (MetaForge + RaidTheory) |
| ARC Raiders | `arc_plan_upgrades` | Hideout upgrade planner: per-module shortfalls vs your stash (shared-pool contention, no silent allocation), nearest-completion ranking, deduped shopping list (RaidTheory + MetaForge) |
| ARC Raiders | `arc_get_enemy` | ARC bestiary: threat, weakness/kill tactics, maps, drops, XP (RaidTheory) |
| ARC Raiders | `arc_who_drops` | Inverse drop index: which enemies drop an item, with threat + maps (RaidTheory + ardb.app) |
| ARC Raiders | `arc_compare_weapons` | Side-by-side weapon stats incl. armor_penetration — the ARC-effectiveness stat (ardb.app) |
| ARC Raiders | `arc_log_raid` | Append a raid to the personal log: map, outcome, loadout, intent, death spot, loot value (local SQLite) |
| ARC Raiders | `arc_list_raids` | Recent raids from the personal log, newest first |
| ARC Raiders | `arc_delete_raid` | Remove a mislogged raid; previews unless `confirm=true` |
| ARC Raiders | `arc_raid_stats` | Personal analytics: extraction rate overall/per-map/per-loadout, death spots, loot totals |
| ARC Raiders | `arc_patch_diff` | Item/weapon changes since N days ago, from auto-accumulated local snapshots |
| ARC Raiders | `arc_get_state` | Cross-device player state (modules, progression, loadout, stash, notes) with per-section age + staleness |
| ARC Raiders | `arc_set_state` | Update state with merge semantics (stash replaces by default), name validation, a returned diff, CAS, and season resets |
| ARC Raiders | `arc_get_trader_stock` | Live trader inventories with prices, optionally one trader; 15-min cache (MetaForge) |
| ARC Raiders | `arc_get_event_schedule` | Rotating in-raid event schedule with UTC times + active/upcoming status, optional map filter; 15-min cache (MetaForge) |
| ARC Raiders | `arc_list_maps` | Playable maps with canonical ids + images; 6-h cache (RaidTheory/arcraiders-data) |
| ARC Raiders | `arc_search_wiki` | Full-text search of the Embark-supported arcraiders.wiki |
| ARC Raiders | `arc_get_wiki_page` | One wiki page as plain text + raw wikitext (infobox weapon stats); CC BY-SA 4.0 |
| Finances | `finances_sync_status` | Feed health from each account's `last_sync` (not transaction age), reported separately from activity, with manual accounts excluded and an overall fresh/stale/dead verdict; optional `trigger_sync` — **call this first**, every other number depends on it |
| Finances | `finances_monthly_summary` | One month's income, spend by category, total, and gap vs. the configured floor; excludes transfers/CC payments/off-budget, plus `floor_excluded_categories` (allocation/obligation — savings, taxes — reported separately as `savings_contributions`/`taxes`/`excluded_from_floor`, never dropped); pro-rated pace mid-month; reports `uncategorized` plus rolling-seven-day/MTD Amazon spend |
| Finances | `finances_recurring` | Expected fixed obligations vs. what posted — MATCHED / CHANGED / MISSING / PENDING_STATEMENT / ENDED, on proportional (±%) tolerance bands, plus notable variances and genuinely new payees over the configured review threshold |
| Finances | `finances_trend` | Per-category monthly spend series + income series over the last N months; flags the current partial month |
| Finances | `finances_debt_status` | Every liability including off-budget loans, each with rate, 7/30-day deltas and an accelerate/ride classification against a configured hurdle rate; accelerate/ride/unknown totals, total debt, home equity, and a loud flag when a debt's class changes between runs |
| Finances | `finances_transactions` | List individual transactions with filters (uncategorized_only, account, date range, payee substring, amount range, category); reports `total` so a partial page is visible. The read half of the categorization loop |
| Finances | `finances_categorize` | Batch-assign category and/or notes to existing transactions, one sync per batch. **Category and notes are the only fields it can reach** — no parameter exists for amount, payee, account or date |
| Finances | `finances_rules_list` | Actual's auto-categorization rules: what each matches, which category it sets |
| Finances | `finances_rule_create` | Create a set-category rule matching payee ids OR an imported-payee regex; the action is built server-side so it cannot rewrite payees or amounts |
| Finances | `finances_rule_delete` | Delete a rule; refuses any rule that does more than set a category |
| Finances | `finances_payees` / `finances_payee_merge` | Payees with transaction counts to spot duplicate variants; merge folds them into one. Merge is **irreversible** and refuses transfer payees |
| Finances | `finances_buffer` | PLAN.md's fear metric: **bank-available** checking − card debt − next mortgage, plus the 14-day look-ahead. Falls back to the cleared register with a stated caveat if the bank feed is down. Components and totals, so it can be checked by hand |
| Finances | `finances_breaches` | Non-income deposits into checking — money arriving from HYSA/brokerage/HELOC to fund operations. Detection only |
| Finances | `finances_room` | The "can we spend this?" arithmetic, **net of fixed bills not yet posted**: room, itemized `remaining_committed`, `variable_floor`/`variable_mtd`, `savings_mtd`/`taxes_mtd`/`excluded_mtd` (the same `floor_excluded_categories` list `finances_monthly_summary` uses, so the two can never disagree), and pace measured on variable spend only, plus recovery math (`required_remaining_pace`, `typical_remaining_pace`, `recovery_delta`). Present as "$X after $Y of upcoming fixed bills" — never the naive figure |
| Finances | `finances_reconcile` | Register vs the **bank's own reported balance** per account; drift classified exact / settlement_window / structural / market_movement / manual. Catches a register that drifted from reality while fully "cleared". Degrades to cleared-only and says so if the bank feed is unavailable |
| Finances | `finances_subscriptions` | Recurring-merchant scan with FIRST-SEEN, to surface subscription creep |
| Finances | `finances_net_worth` | Full rollup incl. off-budget; investable total; home equity (display-only); employer-stock concentration with components itemized — the 401(k) is a target-date fund and is explicitly excluded |
| Finances | `finances_payoff_projection` | Month-by-month amortization from the live balance, vs a minimum-only baseline |
| Finances docs | `finances_docs_get` | Read one governance doc (PLAN/DECISIONS/PULSE/REVIEW/OPERATIONS/PLANNED/ARCHITECTURE/TICKLERS) from the finances repo; also served as `finances://` **resources**. Flags `stale` if the checkout couldn't refresh |
| Finances docs | `finances_decision_append` | Append a dated entry to DECISIONS.md (newest-first), commit and push. Append-only |
| Finances docs | `finances_planned_append` | Append a row to PLANNED.md's spending queue, commit and push. Append-only |
| Finances docs | `finances_ticklers` | Future-dated reminders from TICKLERS.md. `due_only=true` (default) returns only rows that are `open` and due today or earlier in America/New_York — normally an empty list. Unparseable rows come back under `malformed` and must be surfaced: a tickler nobody can read is a reminder that will never fire. Nothing marks one done — that stays a deliberate file edit |
| Finances docs | `finances_tickler_append` | Schedule a reminder against a date rather than trusting it to memory (a rate that reprices, a loan that rolls off, a token that expires). Appends a row with status `open`, commits and pushes. Append-only; advisor scope |
| Finances context | `finances_context_add` | Record what someone said about a transaction, verbatim. `txn_ref` is a **hint**, not an Actual id — usable before the purchase posts. Rate-limited per author/day |
| Finances context | `finances_context_list` | Open / consumed / aged-out context. Read before asking anyone about a transaction |
| Finances context | `finances_context_consume` | Close entries after the categorization they informed actually happened. **Advisor only** |
| Finances context | `finances_clarify_candidates` | Deterministic pick of transactions worth asking about — uncategorized, over threshold, settled but recent, minus anything with open context |
| Paperless | `paperless_search` | Find documents by full-text query, tags, correspondent, date range; metadata only |
| Paperless | `paperless_get` | One document's full OCR text + metadata — where *terms* (rates, escrow, tax figures) live |
| Paperless | `paperless_link` | Set a document's `actual_txn` custom field and return its ASN so the caller can stamp `[doc:<ASN>]` into the Actual transaction's notes |
| Messaging | `signal_send` | Send a message to the ONE configured family Signal group; recipient is config-fixed and not a parameter |

### Finances posture

The `finances_*` tools are **read-only** and deterministic: they compute
numbers, never prose or advice. Two deployment facts matter:

- **They need the Actual sidecar** (`sidecar/`, `services.homelab-mcp.actualSidecar`).
  Actual has no HTTP query API and no API keys, so a small Node service owns
  the `@actual-app/api` client on loopback. Without it the finances tools
  return a configuration error; nothing else is affected.
- **`@actual-app/api` is pinned exactly and must never exceed the sync
  server's version.** The client applies its bundled migrations to the budget
  file, so a newer client migrates the file to a schema the server's own web
  UI cannot read. This happened on 2026-07-30 via an unpinned
  `npx -y actual-budget-mcp`. Check the server's `/info` before bumping.

`finances_monthly_summary` returns `gap_vs_floor: null` until
`HOMELAB_MCP_FINANCES_FLOOR` is set — that number is a household decision and
the tool refuses to invent one.

Its Amazon aggregate always returns rolling-seven-day and month-to-date spend.
`HOMELAB_MCP_FINANCES_AMAZON_BASELINE` optionally supplies the private monthly
comparison baseline; the tool returns `monthly_baseline: null` rather than
guessing when it is unset.

`finances_debt_status` reads every balance from Actual — mortgage, HELOC, car
loan and cards, on-budget or off. The single hand-maintained figure left in the
system is the off-budget `House` valuation, updated quarterly at review. Equity
is **display-only** per the finances repo's PLAN.md — it feeds the net-worth
view and the HELOC scoreboard, never an affordability or spending decision. If
the `House` or mortgage account can't be uniquely identified by name, equity is
reported `null` with the reason rather than silently understating the debt side.

Three deliberate refusals to guess, all in `tools/finances_config.json`:

- **An account is debt only if it is listed in `debts` AND carries a negative
  balance**, so the house and investment accounts can never be swept in. A
  negative balance that *isn't* listed still appears under
  `unlisted_negative_accounts` and still counts toward `total_debt` — silently
  omitting a liability is how a $43k car loan stayed invisible.
- **A debt with no configured rate is classified `unknown`, never guessed.**
  Defaulting it to "ride" would call a carried card balance cheap money.
- **Sync health comes from `last_sync`, not transaction age.** A dormant card
  with a healthy feed reports `fresh` with `activity: "none"` and appears in
  `quiet_but_healthy`; an account whose feed hasn't run reports `dead` even if
  a transaction posted yesterday. Conflating the two kept the overall verdict
  permanently red, which trains the alert away.

### Paperless credential

Use a **dedicated** paperless user + token (`HOMELAB_MCP_PAPERLESS_TOKEN`), not
the admin's and not paperless-ai's — per-consumer tokens keep revocation
surgical, and a superuser token would bypass paperless's object-level
permissions entirely. Minimum grants for the three tools as implemented:

| Permission | Needed by |
|---|---|
| `view_document` | `paperless_search`, `paperless_get` |
| `change_document` | `paperless_link` (PATCHes `custom_fields`) |
| `view_customfield` | `paperless_link` resolves field ids; documents return their fields |
| `view_tag` | `paperless_search` resolves tag *names* via `/api/tags/` |

`view_correspondent` is **not** required: correspondent filtering is a
`correspondent__name__iexact` query parameter on `/api/documents/`, not a
lookup against the correspondents endpoint.

Two caveats worth knowing before provisioning:

- `change_document` is a Django **model** permission, so it permits editing any
  metadata on any accessible document — not just the one custom field. Django
  has no field-level grants; this is the floor for a tool that writes.
- Documents with an `owner` are invisible to a non-superuser unless explicitly
  shared. Unowned documents are visible to any user with `view_document`. If
  `paperless_search` returns zero results for something you can see as admin,
  that's ownership, and it fails *silently* — no error, just an empty list.

The `actual_txn` and `actual_account` custom fields must be **created once in
paperless**. This service deliberately holds no `add_customfield` permission
and reports a missing field as a configuration error rather than creating it.
Only `actual_txn` is written here (by `paperless_link`); `actual_account` is
the statement-level half of the join and is expected to be stamped by
paperless-ai per the finances repo's ARCHITECTURE.md.

### The advisor write layer

`finances_categorize` and the rule tools exist so an interactive Claude session
can work a categorization queue: `finances_sync_status(trigger_sync=true)` →
`finances_transactions(uncategorized_only=true)` → `finances_categorize(...)` →
`finances_rule_create(...)` when a payee recurs.

The finances repo's ARCHITECTURE.md sets the trust boundary: the advisor writes
**categorizations, notes and rules** — never amounts, payees, accounts, or
anything that moves money. That is enforced structurally, in three places:

- **The `Assignment` model is `extra="forbid"`** and has exactly three fields.
  A caller sending `amount` gets a validation error, and the tool re-validates
  at its own boundary rather than trusting the transport to have done it.
- **The sidecar builds its update payload key by key** — never a spread of the
  request — so `updateTransaction` can only ever receive `category` and `notes`.
- **Rule actions are constructed server-side** as a single set-category. No
  action parameter is exposed, so a payee-rewriting rule cannot be built through
  this path, and `finances_rule_delete` refuses any rule that does more than set
  a category.

`addTransactions`, `importTransactions` and `deleteTransaction` are deliberately
not wired into the sidecar at all.

Passing `category: null` clears a categorization — without it a mis-categorized
transaction would be unfixable through this interface. "Starting Balance" rows
(Actual's opening-balance entries) are kept out of the uncategorized worklist by
default: they are not decisions to make, and categorizing one on an on-budget
account would inject a phantom five-figure "spend".

### Governance docs (MCP resources)

The finances repo's reasoning — PLAN.md's targets and guardrails, DECISIONS.md's
dated log of *why* — is served as `finances://` resources from a private clone
the server keeps lazily fresh. A session that can read the ledger but not the
plan gives advice the household has already considered and rejected.

- **Resource access is scoped separately from tools.** Resources are
  URI-addressed, so `restricted_scope_resources` maps a scope to allowed URI
  prefixes. `advisor` gets `finances://`; **hermes gets nothing** — a
  restricted scope with no entry is denied, fail-closed. Before this the
  middleware ignored `resources/*` entirely and any authenticated token could
  have read every document.
- **A failed `git pull` never becomes an error.** The cached copy is served
  with a leading `> **STALE:**` warning (and `stale: true` from the tool), so
  the caller can say so rather than quoting a possibly-outdated figure.
- **Append-only, one shape per file.** There is no doc-editing tool and no
  path that writes PLAN.md: restructuring a governance document is
  session-with-git work where a human sees the diff.
- **TICKLERS.md is read as data, not prose.** `finances_ticklers` parses its
  table so the morning sentinel can ask what has come due. It is the one
  finances-repo read hermes has, and it reaches it through the *tool*, not the
  resource — hermes still gets no `finances://` prefix, so the scoping rule
  above is unchanged. Nothing marks a tickler done: acknowledging one costs a
  deliberate human edit, which is what stops the nag being dismissed in
  passing.
- The token needs `contents: write` for the append tools; a read-only token
  serves the docs and fails those three explicitly.

### Transaction context (the first data-shaped store)

The ledger records that $266.14 went to Bavarian Inn; only a person knows it
was a birthday dinner, and that sentence is available for about a day. This
captures it verbatim, in SQLite (arcraiders precedent — data-shaped and
append-heavy, unlike the governance docs which are prose).

- **`txn_ref` is a hint, not a foreign key** — date, amount, payee fragment.
  The point is capturing a statement *before* the purchase posts, so there may
  be no transaction to reference yet.
- **Aging is lazy and non-destructive.** Open rows past 45 days become
  `aged_out` when something next reads them — no background job. Never
  deleted, so silence can mean "nobody remembers" without the queue growing
  forever.
- **hermes may write here and nowhere else.** `add` / `list` /
  `clarify_candidates` are its only writable and context-reading surface;
  `consume` is advisor-only, because consuming asserts a human-judged
  categorization already happened in the ledger — and hermes never touches
  the ledger.
- **`clarify_candidates` is deterministic** (largest first, fixed filters) so
  a model never chooses what to ask, and excludes anything already covered by
  open context — re-asking is how a channel trains people to ignore it.

### Amazon purchases (the second store-backed category)

`amazon_*` reads a Postgres database owned by
[lading](https://github.com/carpenike/lading), which logs into amazon.com on a
daily timer and parses the order and transaction pages. Same shape as
`school_*` / schoolhouse, and for sharper reasons: lading's credential can
place orders and change shipping addresses, there is no read-only Amazon
account, and a model that retries a failing tool is precisely how an Amazon
account gets challenge-locked. None of that belongs in a network-facing OAuth
resource server, so this process only ever `SELECT`s through a role with
`readonly` membership.

Three things about the data are worth knowing before reading a response:

**A charge is not an order.** One order splits across shipments into several
charges. Prime, AWS, Kindle and Audible post as Amazon charges with nothing
shipped behind them. A `none` match is common and usually not an error —
which is why `amazon_match_charges` always says *why*, and why
`outside_coverage` ("we have never synced that month") is a different answer
from `no_amount_match` ("we looked, there is no such charge").

**Amazon balance is invisible to the ledger.** Purchases marked
`funding: "balance"` never posted to a card, so no bank transaction exists for
them. That balance is usually credit from a return — Amazon refunds returns to
your balance rather than the paying card — but it can also be a received gift
card, and the data cannot tell the two apart. A fully balance-funded order
also reports `grand_total` of `$0.00`, so `gift_card` always travels beside it;
without that an $18 order reads as free.

**Every row carries an `account`.** The household may run more than one Amazon
account against one shared card, so the matcher searches all of them and says
which one a purchase came from.

These tools describe purchases and never categorize. Deciding a budget
category is `finances_categorize`'s job, and the two categories are
deliberately not wired together — `amazon_*` knows nothing about Actual, and
the caller hands charges over in a batch.

**Reading the confidence, from a real run.** Against 14 live ledger charges:
3 `exact`, 2 `ambiguous`, 9 `none`.

`probable` is a normal, actionable result and not a warning. Amazon authorises
at order and captures at ship, so a bank charge posts one to three days AFTER
Amazon's completed date; that gap is expected and costs no confidence. `exact`
additionally requires the card last-4 to be verified, which needs the calling
account to appear in `HOMELAB_MCP_AMAZON_ACCOUNT_LAST4`. **With that map
unset, `exact` is unreachable and `probable` is the ceiling** — which looked
exactly like an algorithmic limit until the setting was wired.

**Always check `oversubscribed` on the batch.** Per-charge confidence cannot
see that two charges claimed the same Amazon transaction, and both come back
looking healthy. That is why charges are handed over in a batch: the check
lives across them. It flags only genuine over-subscription — k charges against
m distinct transaction rows where k > m — because one order really can split
into several equal charges, which happens in this household's data.

**A `none` is usually coverage, not failure.** In that run all 9 misses were
`no_amount_match` and every one was correct: those amounts existed nowhere in
the store, because only one of the household's two Amazon accounts is synced.
`outside_coverage` (the month was never fetched) is a different answer and
must never be reported as "there is no such order".

### Restricted credentials (tool allowlisting)

`HOMELAB_MCP_RESTRICTED_SCOPES` maps an OAuth scope to the exact tools a token
carrying it may call (`src/homelab_mcp/scopes.py`). The shipped default is
`hermes`, for the unattended weekly-pulse agent:

```
finances_sync_status · finances_monthly_summary · finances_recurring
finances_debt_status
```

A `hermes` token is refused at `tools/call` for anything else (fails closed),
including `signal_send` because Hermes delivers through its native gateway,
and those tools are filtered out of its `tools/list` for both JSON and
Streamable HTTP's SSE framing. Tokens without a matching scope — interactive
advisor sessions — keep full access. This makes the agent's compose-and-send-only
remit structural rather than a prompt instruction. Configured restricted-scope
names are also advertised in OAuth discovery so standards-compliant clients can
request them explicitly.

A second scope, `advisor`, covers Ryan's interactive sessions: every finances
tool (reads plus the write layer above), all three `paperless_*` tools, and
`signal_send`. It **narrows** rather than grants — an advisor token cannot reach
Home Assistant, the recipe writers, or anything else outside the financial
surface. `hermes` is unchanged by its addition, and a test asserts hermes is
refused on every one of the new tools.

### Home Assistant posture

HA is a **physical control plane**, so its category is stricter than the
data-shaped ones (see AGENTS.md security non-negotiable #8):

  - **Domain allowlist** (`HOMELAB_MCP_HA_DOMAIN_ALLOWLIST`, JSON array):
    `ha_call_service` checks BOTH the service domain and the target entity's
    domain. High-impact domains (lock, alarm_control_panel, cover, siren,
    valve) are excluded by default; adding one also arms the confirm gate
    (`HOMELAB_MCP_HA_CONFIRM_DOMAINS`), which returns a non-destructive
    preview unless `confirm=true`.
  - **Closed-loop actuation:** HA acks a service call when it's *dispatched*,
    not when the device changed. Every actuation re-reads the entity (polling
    up to `HOMELAB_MCP_HA_CONFIRM_TIMEOUT_SECONDS`, default 3s) and returns
    `{before, after, confirmed, assumed_state}` — so the assistant can say
    "HA accepted it but the light still reports off" instead of a false "Done".
  - **Automations via API, never the filesystem:** edits go through
    `/api/config/automation/config/<id>` (the HA UI editor's own endpoints —
    validated, atomic, hot-reloaded). This service gets no access to HA's
    config directory; hand-written YAML automations stay owned by the config
    repo and are flagged read-only in `ha_list_automations`.
  - **Audit trail:** every executed/previewed/denied write logs one line on
    the `homelab_mcp.audit` logger (tool, target, args, outcome), because the
    request log only ever sees `POST /mcp`.
  - **Token custody:** `HOMELAB_MCP_HA_TOKEN` is a long-lived token from a
    dedicated HA user, sops-managed, never logged. The automation config-API
    tools require that user to be an HA administrator; if you skip those
    tools, use a non-admin user.

## Architecture

```
┌─────────────────────┐
│  Claude (mobile)    │
└──────────┬──────────┘
           │ 1. DCR + 2. /authorize
           ▼
┌──────────────────────────────────────────────────────────┐
│  homelab-mcp  (mcp.holthome.net, via Cloudflare Tunnel)  │
│                                                          │
│   ├─ /.well-known/oauth-protected-resource (RFC 9728)    │
│   ├─ /.well-known/oauth-protected-resource/mcp (RFC 9728 §3.3, VS Code) │
│   ├─ /.well-known/oauth-authorization-server (RFC 8414)  │
│   ├─ /.well-known/mcp-as-contract.json (hosted contract, public) │
│   ├─ /contract              (hosted CONTRACT.md, public)  │
│   ├─ /oauth/jwks.json     (public verifier key)          │
│   ├─ /oauth/register      (RFC 7591 DCR)                 │
│   ├─ /oauth/authorize ────► 302 to PocketID              │
│   ├─ /oauth/callback ◄──── PocketID returns code         │
│   ├─ /oauth/token         (PKCE-verified, mints RS256)   │
│   └─ /mcp                 (FastMCP transport + JWT)      │
└──────────┬──────────┬────────────────────────────────────┘
           │          │
           │          └─► PocketID (id.holthome.net) — passkey login
           │
           ├──► fedcook.holthome.net  (federation search)
           ├──► cook.holthome.net     (CookLang recipes: read + author + shopping list)
           ├──► gatus.holthome.net    (uptime monitoring)
           ├──► grocy.holthome.net    (food inventory: stock + master data)
           └──► hass.holthome.net     (Home Assistant: states + services + automations)
```

JWTs are RS256, signed by a 2048-bit RSA key resident on the host. The key comes from one of:

  1. `HOMELAB_MCP_OAUTH_SIGNING_KEY` env var (sops-managed; preferred — key never touches disk)
  2. `HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH` file (sops-mounted secret)
  3. auto-generated and persisted to `/var/lib/homelab-mcp/signing-key.pem` (0600) on first run

The matching public key is published at `/oauth/jwks.json` so any external verifier (or our
own middleware) can validate offline.

## Local development

Requires Nix with flakes and direnv:

```bash
cd ~/src/mcp
direnv allow
# devshell loads python313 + all deps + ruff + mypy + pytest
```

Or manually:

```bash
nix develop
```

Run the server locally (OAuth disabled — local loopback only):

```bash
HOMELAB_MCP_OAUTH_REQUIRED=false \
HOMELAB_MCP_BIND_ADDRESS=127.0.0.1 \
HOMELAB_MCP_PORT=9200 \
homelab-mcp
```

Probe it:

```bash
curl -s http://127.0.0.1:9200/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tests

```bash
pytest -v
```

Coverage focuses on the security-critical bits:
  - `tests/test_auth.py` — JWT validation rejection paths (real RSA keypair).
  - `tests/test_oauth_flow.py` — end-to-end OAuth dance with PocketID mocked.
  - `tests/test_tools_cooklang.py` — recipe CRUD against a mocked CookLang wire + slug/path-traversal hardening.
  - `tests/test_tools_ha.py` — HA domain allowlist + confirm gate + the closed-loop `confirmed` contract (a 200 on the service call must never read as "device changed").
  - `tests/test_app.py` — discovery + allowlist + middleware wiring.

## Deployment

This repo ships a NixOS module at `flake.nixosModules.default`. Consumer pattern (in
`carpenike/nix-config`):

```nix
# flake.nix
inputs.homelab-mcp = {
  url = "github:carpenike/mcp";
  inputs.nixpkgs.follows = "nixpkgs";
};

# hosts/forge/services/homelab-mcp.nix
{ config, inputs, pkgs, ... }: {
  imports = [ inputs.homelab-mcp.nixosModules.default ];

  services.homelab-mcp = {
    enable = true;
    package = inputs.homelab-mcp.packages.${pkgs.system}.default;

    publicBaseUrl = "https://mcp.holthome.net";

    settings = {
      HOMELAB_MCP_POCKETID_ISSUER     = "https://id.holthome.net";
      HOMELAB_MCP_POCKETID_CLIENT_ID  = "<from PocketID admin UI>";
      HOMELAB_MCP_COOKLANG_BASE_URL   = "https://cook.holthome.net";
      HOMELAB_MCP_FEDERATION_BASE_URL = "https://fedcook.holthome.net";
      HOMELAB_MCP_GATUS_BASE_URL      = "https://gatus.holthome.net";
      HOMELAB_MCP_HA_BASE_URL         = "https://hass.holthome.net";
      # Recommended once the physical-control (ha_*) category is enabled:
      # tighten who can log in and how long a bearer token lives. Refresh
      # rotation makes the shorter access-token lifetime invisible to clients.
      HOMELAB_MCP_OAUTH_USER_ALLOWLIST = ''["ryan@ryanholt.net"]'';
      HOMELAB_MCP_OAUTH_ACCESS_TOKEN_LIFETIME_SECONDS = "14400"; # 4h
      # Optional overrides (shown with their defaults):
      # HOMELAB_MCP_HA_DOMAIN_ALLOWLIST = ''["light","switch","fan","scene","script","media_player","climate","vacuum","humidifier","input_boolean","automation"]'';
      # HOMELAB_MCP_HA_CONFIRM_DOMAINS  = ''["lock","alarm_control_panel","cover","siren","valve"]'';
    };

    # sops-managed env file with at minimum:
    #   HOMELAB_MCP_POCKETID_CLIENT_SECRET=...
    #   HOMELAB_MCP_HA_TOKEN=<HA long-lived access token, dedicated user>
    # Optionally:
    #   HOMELAB_MCP_OAUTH_SIGNING_KEY=<RSA PEM, PKCS#8, escaped \n>
    environmentFile = config.sops.secrets."homelab-mcp/env".path;
  };
}
```

### PocketID client setup (one-time)

In PocketID admin UI, create an OIDC client with:

  - **Callback URL:** `https://mcp.holthome.net/oauth/callback`
  - **Scopes:** `openid email profile`

Copy the client ID into `HOMELAB_MCP_POCKETID_CLIENT_ID` and the client secret into the
sops env file as `HOMELAB_MCP_POCKETID_CLIENT_SECRET`.

See [`AGENTS.md`](AGENTS.md) for the conventions an AI coding agent (or human) should follow
when extending this.

## License

MIT
