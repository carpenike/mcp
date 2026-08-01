/**
 * homelab-mcp Actual sidecar.
 *
 * Why this exists: Actual has no HTTP query API and no API keys. The only
 * supported programmatic client is the Node package `@actual-app/api`, which
 * downloads the (end-to-end encrypted) budget file locally and answers queries
 * against it. The Python `finances_*` tools call this process over loopback
 * instead of reimplementing Actual's CRDT sync in Python.
 *
 * Two non-negotiables, both learned the hard way:
 *
 *   1. VERSION PINNING. The client's bundled migrations are applied to the
 *      budget file. A client NEWER than the sync server migrates the file to a
 *      schema the server (and its web UI) cannot read, which is how the
 *      2026-07-30 lockout happened: an unpinned `npx -y actual-budget-mcp`
 *      pulled 26.7.0 against a 26.6.0 server and applied
 *      `1780606215000_add_bank_sync_status.sql`. `@actual-app/api` is pinned
 *      exactly in package.json — never widen it to a caret range, and never
 *      raise it above the running sync server.
 *
 *   2. ONE LOGIN. `POST /account/login` on the Actual server rate-limits at
 *      5 FAILED attempts per 15 minutes. `api.init()` performs the single
 *      login for the life of this process; the loaded budget is reused for
 *      every request. There is no retry loop anywhere in this file — a failed
 *      init exits non-zero and lets systemd back off.
 *
 * Exposure: binds loopback only and requires a shared token, so a local
 * unprivileged process can't read the household's finances by curling a port.
 *
 * WRITE BOUNDARY. Most endpoints are reads. The three that write are
 * deliberately narrow, because the advisor's remit (see the finances repo's
 * ARCHITECTURE.md) is to categorize and annotate — never to move money:
 *
 *   POST /transactions/categorize  sets ONLY `category` and `notes`, on
 *                                  transactions that already exist. The update
 *                                  payload is built field-by-field here, so
 *                                  there is no path by which an amount, payee,
 *                                  account or date can be altered even if the
 *                                  caller sends one.
 *   POST /rules                    creates a rule whose actions are
 *                                  constructed server-side as a single
 *                                  set-category. Caller-supplied actions are
 *                                  never forwarded, so no payee-rewriting or
 *                                  amount-modifying rule can be built.
 *   DELETE /rules/:id              removes a rule. Rules are derived
 *                                  automation, not financial records.
 *
 * Nothing here creates or deletes a transaction; `addTransactions`,
 * `importTransactions` and `deleteTransaction` are deliberately not wired up.
 */

'use strict';

const fs = require('node:fs');
const http = require('node:http');
const { URL } = require('node:url');
const api = require('@actual-app/api');

const HOST = process.env.SIDECAR_HOST || '127.0.0.1';
const PORT = Number(process.env.SIDECAR_PORT || 9210);
const TOKEN = process.env.SIDECAR_TOKEN || '';
const SERVER_URL = process.env.ACTUAL_SERVER_URL || '';
const PASSWORD = process.env.ACTUAL_PASSWORD || '';
const SYNC_ID = process.env.ACTUAL_BUDGET_SYNC_ID || '';
const ENCRYPTION_PASSWORD = process.env.ACTUAL_ENCRYPTION_PASSWORD || '';
const DATA_DIR = process.env.ACTUAL_DATA_DIR || '/var/lib/homelab-mcp/actual';
// How long a loaded budget is served before the next read re-syncs it.
const SYNC_TTL_MS = Number(process.env.SIDECAR_SYNC_TTL_SECONDS || 300) * 1000;

let ready = false;
let lastSyncMs = 0;
let startupError = null;
let serverVersion = null;

// The exact pin from our own package.json — the version whose migrations this
// process will apply to the budget file.
const CLIENT_VERSION = require('./package.json').dependencies['@actual-app/api'];

/** Parse "26.7.0" / "26.8.0-nightly.20260730" into comparable numbers. */
function parseVersion(v) {
  return String(v || '')
    .split('-')[0]
    .split('.')
    .map((n) => Number.parseInt(n, 10) || 0);
}

/** -1 if a < b, 0 if equal, 1 if a > b. */
function compareVersions(a, b) {
  const [x, y] = [parseVersion(a), parseVersion(b)];
  for (let i = 0; i < 3; i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0) ? -1 : 1;
  }
  return 0;
}

function log(msg, extra) {
  // stdout only; journald captures it. Never log secrets or transaction data.
  console.log(`[sidecar] ${msg}${extra ? ' ' + JSON.stringify(extra) : ''}`);
}

/** Single login + single budget download for the life of the process. */
async function boot() {
  const missing = ['ACTUAL_SERVER_URL', 'ACTUAL_PASSWORD', 'ACTUAL_BUDGET_SYNC_ID'].filter(
    (k) => !process.env[k],
  );
  if (missing.length) throw new Error(`missing required env: ${missing.join(', ')}`);

  // ── Version interlock ──────────────────────────────────────────────
  // Refuse to touch the budget if this client is NEWER than the sync server.
  // `downloadBudget` applies the client's bundled migrations, so a newer
  // client rewrites the file to a schema the server (and its web UI) cannot
  // read — the 2026-07-30 lockout. Checked BEFORE init/download so the damage
  // is prevented rather than detected.
  //
  // Both the server package and this pin are pinned, so they cannot drift on
  // their own. What this guards is the manual case: bumping one side and
  // forgetting the other. Bumping this pin ahead of the server is exactly what
  // caused the 2026-07-30 lockout, and a pinned nixpkgs does not prevent it.
  const info = await (await fetch(`${SERVER_URL.replace(/\/$/, '')}/info`)).json();
  serverVersion = info && info.build ? info.build.version : null;
  const cmp = compareVersions(CLIENT_VERSION, serverVersion);
  if (serverVersion && cmp > 0) {
    throw new Error(
      `REFUSING TO START: client @actual-app/api ${CLIENT_VERSION} is NEWER than ` +
        `sync server ${serverVersion}. Downloading the budget would migrate the ` +
        `file to a schema the server cannot read. Upgrade the server first, or ` +
        `lower the pin in sidecar/package.json.`,
    );
  }
  if (serverVersion && cmp < 0) {
    // Safe for the file, but the server may migrate it past what this client
    // understands, which surfaces as "no such column" on sync. Visible in
    // /health so it alerts instead of silently breaking the finances tools.
    log('WARNING: client older than server — bump the pin', {
      client: CLIENT_VERSION,
      server: serverVersion,
    });
  }

  // api.init() does NOT create dataDir and fails with a bare ENOENT if it is
  // absent — which is the normal state on a fresh host, since systemd's
  // StateDirectory creates /var/lib/homelab-mcp but not this subdirectory.
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });

  await api.init({ dataDir: DATA_DIR, serverURL: SERVER_URL, password: PASSWORD });
  log('init ok (single login)');

  // `password` is the file's end-to-end encryption password, distinct from
  // the server password above. Omitted when the budget isn't encrypted.
  const opts = ENCRYPTION_PASSWORD ? { password: ENCRYPTION_PASSWORD } : undefined;
  await api.downloadBudget(SYNC_ID, opts);
  lastSyncMs = Date.now();
  ready = true;
  log('budget loaded', { encrypted: Boolean(ENCRYPTION_PASSWORD) });
}

/**
 * Pull remote changes if the cached budget is older than SYNC_TTL_MS.
 *
 * A sync failure is deliberately non-fatal: stale-but-served beats a hard
 * error, and `finances_sync_status` is the tool that surfaces staleness. The
 * age is reported on every response so callers can judge for themselves.
 */
async function freshen(force = false) {
  if (!force && Date.now() - lastSyncMs < SYNC_TTL_MS) return;
  try {
    await api.sync();
    lastSyncMs = Date.now();
  } catch (e) {
    log('sync failed (serving cached budget)', { error: String(e && e.message) });
  }
}

// ── data accessors ───────────────────────────────────────────────────

async function getAccounts() {
  await freshen();
  // `last_sync` is the bank feed's last successful fetch — the only true
  // sync-health signal. Transaction age is a proxy that misreads a dormant
  // account as a broken feed. Queried separately because getAccounts() does
  // not project it; tolerated as null if the schema ever drops it.
  let lastSyncById = new Map();
  try {
    const { data } = await api.runQuery(
      api.q('accounts').select(['id', 'last_sync']).filter({ closed: false }),
    );
    lastSyncById = new Map(data.map((r) => [r.id, r.last_sync ?? null]));
  } catch (e) {
    log('last_sync unavailable, falling back to transaction age', {
      error: String(e && e.message),
    });
  }

  // Cleared balance = the sum of transactions the bank has CONFIRMED. It is
  // what "cash you actually have" means, and it is the basis of the Buffer;
  // the register total additionally includes anything still in flight.
  let clearedById = new Map();
  let statusById = new Map();
  try {
    const { data } = await api.runQuery(
      api.q('transactions').filter({ cleared: true }).groupBy('account')
        .select(['account', { cleared: { $sum: '$amount' } }]),
    );
    clearedById = new Map(data.map((r) => [r.account, r.cleared ?? 0]));
  } catch (e) {
    log('cleared balances unavailable', { error: String(e && e.message) });
  }
  try {
    const { data } = await api.runQuery(
      api.q('accounts').select(['id', 'bank_sync_status']).filter({ closed: false }),
    );
    statusById = new Map(data.map((r) => [r.id, r.bank_sync_status ?? null]));
  } catch (e) {
    log('bank_sync_status unavailable', { error: String(e && e.message) });
  }

  const accounts = await api.getAccounts();
  const out = [];
  for (const a of accounts) {
    out.push({
      id: a.id,
      name: a.name,
      offbudget: Boolean(a.offbudget),
      closed: Boolean(a.closed),
      // Actual stores money as integer cents everywhere. Keep it that way on
      // the wire; the Python side converts once, at the tool boundary.
      balance_cents: await api.getAccountBalance(a.id),
      last_sync: lastSyncById.get(a.id) ?? null,
      cleared_balance_cents: clearedById.get(a.id) ?? 0,
      bank_sync_status: statusById.get(a.id) ?? null,
    });
  }
  return out;
}

async function getCategories() {
  await freshen();
  const [cats, groups] = await Promise.all([api.getCategories(), api.getCategoryGroups()]);
  const groupById = new Map(groups.map((g) => [g.id, g]));
  return cats.map((c) => {
    const g = groupById.get(c.group_id);
    return {
      id: c.id,
      name: c.name,
      group_id: c.group_id,
      group_name: g ? g.name : null,
      // `is_income` lives on the GROUP in Actual, not the category.
      is_income: Boolean((g && g.is_income) || c.is_income),
    };
  });
}

async function getTransactions(start, end) {
  await freshen();
  // splits:'inline' replaces each split parent with its children, so summing
  // by category can't double-count a split transaction against its parent.
  const { data } = await api.runQuery(
    api
      .q('transactions')
      .filter({ date: [{ $gte: start }, { $lte: end }] })
      .select([
        'id',
        'date',
        'amount',
        'notes',
        'transfer_id',
        'cleared',
        'account',
        'account.name',
        'account.offbudget',
        'category',
        'category.name',
        'payee.name',
      ])
      .options({ splits: 'inline' }),
  );
  return data.map((t) => ({
    id: t.id,
    date: t.date,
    amount_cents: t.amount,
    notes: t.notes || null,
    // Non-null transfer_id marks the leg of an account-to-account transfer:
    // real money movement, but not household spend.
    is_transfer: Boolean(t.transfer_id),
    cleared: Boolean(t.cleared),
    account_id: t.account,
    account_name: t['account.name'] || null,
    account_offbudget: Boolean(t['account.offbudget']),
    category_id: t.category || null,
    category_name: t['category.name'] || null,
    payee_name: t['payee.name'] || null,
  }));
}

/**
 * Apply category/notes to existing transactions, then push once.
 *
 * The payload is assembled key-by-key rather than spread from the request, so
 * an unexpected field cannot reach `updateTransaction`. One `sync()` for the
 * whole batch: per-item syncing would multiply round-trips to the Actual
 * server for no benefit.
 */
async function categorize(assignments) {
  const results = [];
  let changed = 0;
  for (const a of assignments) {
    const id = a && a.transaction_id;
    if (!id) {
      results.push({ transaction_id: null, ok: false, error: 'missing transaction_id' });
      continue;
    }
    // Whitelist, explicitly. Never `...a`.
    const fields = {};
    if (a.category_id !== undefined) fields.category = a.category_id;
    if (a.notes !== undefined) fields.notes = a.notes;
    if (Object.keys(fields).length === 0) {
      results.push({ transaction_id: id, ok: false, error: 'nothing to set' });
      continue;
    }
    try {
      await api.updateTransaction(id, fields);
      changed += 1;
      results.push({ transaction_id: id, ok: true, set: Object.keys(fields) });
    } catch (e) {
      results.push({ transaction_id: id, ok: false, error: String(e && e.message) });
    }
  }
  if (changed > 0) {
    await api.sync();
    lastSyncMs = Date.now();
  }
  return { results, changed };
}

/** Rules, projected to the shape the Python side reasons about. */
async function listRules() {
  await freshen();
  const rules = await api.getRules();
  return rules.map((r) => ({
    id: r.id,
    stage: r.stage ?? null,
    conditions_op: r.conditionsOp,
    conditions: r.conditions,
    actions: r.actions,
    // Surfaced so the Python layer can refuse to delete a rule that does
    // anything other than set a category.
    sets_category_only:
      Array.isArray(r.actions) &&
      r.actions.length > 0 &&
      r.actions.every((x) => x.op === 'set' && x.field === 'category'),
  }));
}

// ── HTTP plumbing ────────────────────────────────────────────────────

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Read and parse a JSON request body, bounded so a bad caller can't OOM us. */
async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2 * 1024 * 1024) throw new Error('request body too large');
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function send(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(payload);
}

async function handle(req, res) {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const path = url.pathname;

  // /health is the liveness probe (gatus): unauthenticated, and answers even
  // before the budget finishes loading so a slow boot is visible, not silent.
  if (path === '/health') {
    // `version_ok` is false when the pin no longer matches the server — i.e.
    // one side was bumped without the other. Surfaced here for gatus rather
    // than left to be discovered when the weekly pulse reports no data.
    const versionOk = !serverVersion || compareVersions(CLIENT_VERSION, serverVersion) === 0;
    return send(res, ready ? 200 : 503, {
      ok: ready,
      budget_loaded: ready,
      last_sync_age_seconds: ready ? Math.floor((Date.now() - lastSyncMs) / 1000) : null,
      client_version: CLIENT_VERSION,
      server_version: serverVersion,
      version_ok: versionOk,
      error: startupError ? String(startupError) : null,
    });
  }

  if (TOKEN) {
    const supplied = req.headers['x-sidecar-token'];
    if (supplied !== TOKEN) return send(res, 401, { error: 'unauthorized' });
  }
  if (!ready) return send(res, 503, { error: 'budget_not_loaded' });

  try {
    if (path === '/accounts' && req.method === 'GET') {
      return send(res, 200, { accounts: await getAccounts() });
    }
    if (path === '/categories' && req.method === 'GET') {
      return send(res, 200, { categories: await getCategories() });
    }
    if (path === '/transactions' && req.method === 'GET') {
      const start = url.searchParams.get('start');
      const end = url.searchParams.get('end');
      if (!DATE_RE.test(start || '') || !DATE_RE.test(end || '')) {
        return send(res, 400, { error: 'start and end must be YYYY-MM-DD' });
      }
      return send(res, 200, { transactions: await getTransactions(start, end) });
    }
    if (path === '/payees' && req.method === 'GET') {
      await freshen();
      const payees = await api.getPayees();
      // Counts make near-duplicate variants ("Monrovia" x4) obvious and let
      // the caller judge which spelling to keep.
      const counts = new Map();
      try {
        const { data } = await api.runQuery(
          api.q('transactions').groupBy('payee').select(['payee', { n: { $count: '*' } }]),
        );
        for (const r of data) counts.set(r.payee, r.n ?? 0);
      } catch (e) {
        log('payee counts unavailable', { error: String(e && e.message) });
      }
      return send(res, 200, {
        payees: payees.map((p) => ({
          id: p.id,
          name: p.name,
          // A transfer payee is the other side of an account-to-account move,
          // not a merchant. Merging one would corrupt transfer wiring.
          transfer_acct: p.transfer_acct ?? null,
          transaction_count: counts.get(p.id) ?? 0,
        })),
      });
    }
    if (path === '/payees/merge' && req.method === 'POST') {
      const body = await readJson(req);
      const keep = body.keep_id;
      const merge = Array.isArray(body.merge_ids) ? body.merge_ids : [];
      if (!keep || merge.length === 0) {
        return send(res, 400, { error: 'keep_id and a non-empty merge_ids[] are required' });
      }
      if (merge.includes(keep)) {
        return send(res, 400, { error: 'keep_id cannot also appear in merge_ids' });
      }
      // IRREVERSIBLE: Actual repoints every transaction at `keep` and deletes
      // the merged payees. There is no undo short of restoring a backup.
      await api.mergePayees(keep, merge);
      await api.sync();
      lastSyncMs = Date.now();
      return send(res, 200, { merged: true, keep_id: keep, merged_ids: merge });
    }
    if (path === '/rules' && req.method === 'GET') {
      return send(res, 200, { rules: await listRules() });
    }
    if (path === '/rules' && req.method === 'POST') {
      const body = await readJson(req);
      const conditions = Array.isArray(body.conditions) ? body.conditions : null;
      const categoryId = body.category_id;
      if (!conditions || !conditions.length || !categoryId) {
        return send(res, 400, { error: 'conditions[] and category_id are required' });
      }
      // Actions are built HERE, never taken from the request: this is what
      // makes "set-category only" a property of the service rather than a
      // convention the caller is trusted to follow.
      const created = await api.createRule({
        stage: body.stage ?? null,
        conditionsOp: body.conditions_op === 'or' ? 'or' : 'and',
        conditions,
        actions: [{ op: 'set', field: 'category', value: categoryId }],
      });
      await api.sync();
      lastSyncMs = Date.now();
      return send(res, 200, { rule: created });
    }
    if (path.startsWith('/rules/') && req.method === 'DELETE') {
      const id = decodeURIComponent(path.slice('/rules/'.length));
      if (!id) return send(res, 400, { error: 'rule id required' });
      await api.deleteRule(id);
      await api.sync();
      lastSyncMs = Date.now();
      return send(res, 200, { deleted: true, id });
    }
    if (path === '/transactions/categorize' && req.method === 'POST') {
      const body = await readJson(req);
      if (!Array.isArray(body.assignments) || body.assignments.length === 0) {
        return send(res, 400, { error: 'assignments[] is required' });
      }
      return send(res, 200, await categorize(body.assignments));
    }
    if (path === '/bank-sync' && req.method === 'POST') {
      // Asks Actual to pull from the banks (SimpleFin). Slow; the Python tool
      // only calls it when explicitly asked via trigger_sync=true.
      await api.runBankSync();
      await freshen(true);
      return send(res, 200, { ok: true, synced_at_age_seconds: 0 });
    }
    return send(res, 404, { error: 'not_found' });
  } catch (e) {
    log('request failed', { path, error: String(e && e.message) });
    return send(res, 502, { error: 'actual_error', message: String(e && e.message) });
  }
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((e) => {
    log('handler crashed', { error: String(e && e.message) });
    if (!res.headersSent) send(res, 500, { error: 'internal' });
  });
});

async function main() {
  // Listen first so /health can report a boot failure instead of the service
  // just being an unexplained connection-refused.
  server.listen(PORT, HOST, () => log(`listening on ${HOST}:${PORT}`));
  try {
    await boot();
  } catch (e) {
    startupError = e && e.message ? e.message : e;
    log('BOOT FAILED — not retrying (login is rate-limited)', { error: String(startupError) });
    // Exit so systemd applies its own backoff. Retrying in-process would burn
    // the 5-failures-per-15-minutes login budget.
    process.exitCode = 1;
    server.close();
  }
}

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    log(`${sig} — shutting down`);
    server.close();
    try {
      await api.shutdown();
    } catch {
      /* best effort */
    }
    process.exit(0);
  });
}

main();
