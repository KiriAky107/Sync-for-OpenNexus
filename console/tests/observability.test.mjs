import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseObservability, parseRedactedExport } from '../src/observability.ts'
import { SyncApi } from '../src/api.ts'
const cleanup = { kind: 'uploads', started_at: 100, finished_at: 101, duration_ms: 1000,
  selected: 1, removed: 1, released_bytes: 7, filesystem_failures: 0, metadata_failures: 0 }
const item = { ...cleanup, id: 'f'.repeat(32) }
const page = { schema_version: 1, snapshot_only: true, observed_at: 200, cache_seconds: 0, history_retention: 'indefinite',
  history: { items: [item], next_before: null, retention_limit: 1000, idle_checks_recorded: false },
  latest_cleanup: { ...cleanup, total_removed: 1, total_failures: 0 }, capacity: { vaults: 3, blocked: 1, near_quota: 1, warning_percent: 80, basis: 'charged_objects_and_active_upload_reservations' },
  accounting_mismatches: 0, pending_reclamation_objects: 1,
  backup_receipts: { configured: true, threshold_seconds: 86400, archive_association_known: false,
    backup: null, verify: null, unfinished_operations: 1, failed_operations: 0 } }
const reminder = { finished_utc: '2026-10-11T01:00:00.000Z', seconds_ago: 50, state: 'recent_receipt', recorded_backup_age_seconds: null }
const exportData = { ...page, redacted: true, cleanup_retention_limit: 1000, cleanup_page: [cleanup] }
const session = { access_token: 'owned-access', refresh_token: 'owned-refresh', device_id: 'owned-device', expires_in: 900 }
const response = (body, status = 200) => new Response(JSON.stringify(body), { status })

test('backup, verify, missing and future-clock observations remain independent', () => {
  assert.equal(parseObservability(page).backup_receipts.backup, null)
  const receipts = { ...page.backup_receipts, backup: reminder, verify: { ...reminder, seconds_ago: 90000, state: 'stale_receipt', recorded_backup_age_seconds: 30 } }
  assert.equal(parseObservability({ ...page, backup_receipts: receipts }).backup_receipts.archive_association_known, false)
  assert.equal(parseObservability({ ...page, backup_receipts: { ...receipts, backup: { ...reminder, seconds_ago: 0, state: 'clock_ahead' } } }).backup_receipts.backup.state, 'clock_ahead')
  for (const changes of [{ archive_association_known: true }, { threshold_seconds: 10 }, { configured: false }, { backup: { ...reminder, state: 'stale_receipt' } }]) {
    assert.throws(() => parseObservability({ ...page, backup_receipts: { ...receipts, ...changes } }), /INVALID_RESPONSE/)
  }
})

test('cleanup pages enforce bounds, order and exact cursor and preserve idle summary', () => {
  const earlier = { ...item, id: 'e'.repeat(32) }, cursor = `101:${item.id}`
  assert.equal(parseObservability({ ...page, history: { ...page.history, items: [earlier] } }, cursor).history.items.length, 1)
  const idle = { ...page.latest_cleanup, selected: 0, removed: 0, released_bytes: 0 }
  assert.equal(parseObservability({ ...page, latest_cleanup: idle }).latest_cleanup.total_removed, 1)
  for (const changes of [{ items: [item, item] }, { items: [earlier, item] }, { items: Array(21).fill(item) },
    { next_before: `101:${earlier.id}` }, { retention_limit: 0 }, { idle_checks_recorded: true }]) {
    assert.throws(() => parseObservability({ ...page, history: { ...page.history, ...changes } }), /INVALID_RESPONSE/)
  }
  assert.throws(() => parseObservability(page, cursor), /INVALID_RESPONSE/)
})

test('unsafe counts and false capacity/history/cache claims are rejected', () => {
  for (const changes of [{ observed_at: -1 }, { cache_seconds: 30 }, { history_retention: 'thirty_days' }, { snapshot_only: false },
    { accounting_mismatches: Number.MAX_SAFE_INTEGER + 1 }, { capacity: { ...page.capacity, blocked: 3 } },
    { latest_cleanup: { ...page.latest_cleanup, removed: 2 } }]) assert.throws(() => parseObservability({ ...page, ...changes }), /INVALID_RESPONSE/)
})

test('export projects a nested whitelist even if future responses contain private data', () => {
  const secret = { vault_id: 'sensitive-vault', operation_id: 'sensitive-operation', path: 'sensitive-path', hash: 'sensitive-hash', token: 'sensitive-token' }
  const raw = { ...exportData, ...secret, capacity: { ...page.capacity, ...secret }, cleanup_page: [{ ...cleanup, ...secret }],
    backup_receipts: { ...page.backup_receipts, ...secret, backup: { ...reminder, ...secret } } }
  const exported = parseRedactedExport(raw), serialized = JSON.stringify(exported)
  assert.equal(serialized.includes('sensitive-'), false)
  assert.equal('history' in exported, false)
  assert.equal('latest_cleanup' in exported, false)
  assert.equal(exported.cleanup_page[0].released_bytes, 7)
  assert.throws(() => parseRedactedExport({ ...raw, redacted: false }), /INVALID_RESPONSE/)
})

test('observation and export use readonly no-cache queries and validate cursors before dispatch', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    calls.push(path); assert.equal(init.method ?? 'GET', 'GET'); assert.equal(init.cache, 'no-store')
    return response(path.endsWith('/export') ? exportData : { ...page, history: { ...page.history, items: [] } })
  })
  const api = new SyncApi(); await api.login('operator', 'controlled-password', 'owned-test')
  await api.observability(`101:${item.id}`); await api.diagnosticExport()
  assert.deepEqual(calls, [`/sync/v1/admin/observability?limit=20&before=101%3A${item.id}`, '/sync/v1/admin/observability/export'])
  await assert.rejects(api.observability('../escape'), /INVALID_REQUEST/); assert.equal(calls.length, 2)
})

test('late exports and readonly observations cannot cross an identity change', async t => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  t.mock.method(globalThis, 'fetch', async path => path === '/sync/v1/auth/sessions' ? response(session) : pending)
  const api = new SyncApi(); await api.login('first', 'controlled-password', 'owned-test')
  const observation = assert.rejects(api.observability(), /SESSION_CHANGED/), exportRead = assert.rejects(api.diagnosticExport(), /SESSION_CHANGED/)
  await api.login('second', 'controlled-password', 'owned-test'); finish(response(exportData))
  await Promise.all([observation, exportRead]); assert.equal(api.signedIn, true)
})

test('ordinary role and unavailable original receipt store fail visibly', async t => {
  let code = 'OPERATIONS_FORBIDDEN', status = 403
  t.mock.method(globalThis, 'fetch', async path => path === '/sync/v1/auth/sessions' ? response(session) : response({ error: { code } }, status))
  const api = new SyncApi(); await api.login('member', 'controlled-password', 'owned-test')
  await assert.rejects(api.observability(), /OPERATIONS_FORBIDDEN/)
  code = 'OPERATIONS_STATUS_UNAVAILABLE'; status = 503; await assert.rejects(api.diagnosticExport(), /OPERATIONS_STATUS_UNAVAILABLE/)
})
