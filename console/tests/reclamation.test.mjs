import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SyncApi, parseReclamationPage, parseReclamationDetail } from '../src/api.ts'

const response = (body, status = 200) => new Response(JSON.stringify(body), { status })
const session = { access_token: 'owned-access', refresh_token: 'owned-refresh', device_id: 'owned-device', expires_in: 900 }
const plan = { plan_id: 'f'.repeat(32), vault_id: 'a'.repeat(32), actor_id: 'operator-cli', state: 'preview', code: 'GC_PREVIEW_READY', created: 1000000, expires: 2000010, finished: null, expired: false, sequence: 2, charged_bytes: 20, selected_objects: 1, candidate_bytes: 3, eligible_objects: 1, eligible_bytes: 3, has_more: false }
const page = { schema_version: 1, snapshot_only: true, observed_at: 2000000, items: [plan], next_before: null }
const detail = { ...plan, schema_version: 1, snapshot_only: true, observed_at: page.observed_at, backup_manifest_sha256: 'b'.repeat(64), preview: { sequence: 2, charged_bytes: 20, grace_hours: 168, cutoff: 395200, policy: { history_retention: 'indefinite', automatic_reclamation: false, default_grace_hours: 168, minimum_grace_hours: 24, protection: ['all_revisions', 'verified_backup'], completion_after_reclamation: 'new_upload_required' }, candidates: [{ hash: 'c'.repeat(64), size: 3, created: 1 }], candidate_bytes: 3, eligible_objects: 1, eligible_bytes: 3, has_more: false, protected_counts: { revision: 2 } }, items: [], reclaimed_bytes: 0, reclaimed_objects: 0, protected_objects: 0, pending_objects: 0 }

test('operator pagination uses exact filters and only reads an original plan', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    calls.push({ path, method: init.method ?? 'GET' })
    assert.equal(init.cache, 'no-store')
    return response(path.includes('?') ? page : detail)
  })
  const api = new SyncApi(); await api.login('operator', 'controlled-password', 'test')
  assert.deepEqual(await api.reclamationPlans({ vault: plan.vault_id, state: 'preview' }), page)
  assert.deepEqual(await api.reclamationPlan(plan.plan_id), detail)
  assert.deepEqual(calls.map(c => c.method), ['GET', 'GET'])
  assert.match(calls[0].path, /vault_id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&state=preview/)
  await assert.rejects(api.reclamationPlans({ vault: '../escape' }), /INVALID_REQUEST/)
  assert.equal(calls.length, 2)
})

test('cursor and ordering cannot replay a page or change the original plan identity', () => {
  const second = { ...plan, plan_id: 'e'.repeat(32) }
  const next = `${plan.created}:${plan.plan_id}`
  assert.deepEqual(parseReclamationPage({ ...page, items: [second] }, next).items, [second])
  for (const changed of [{ items: [plan, plan] }, { items: [second, plan] }, { next_before: `${plan.created}:${second.plan_id}` }, { snapshot_only: false }, { items: [{ ...plan, charged_bytes: Number.MAX_SAFE_INTEGER + 1 }] }]) assert.throws(() => parseReclamationPage({ ...page, ...changed }), /INVALID_RESPONSE/)
  assert.throws(() => parseReclamationPage(page, next), /INVALID_RESPONSE/)
  assert.throws(() => parseReclamationDetail({ ...detail, plan_id: second.plan_id }, plan.plan_id), /INVALID_RESPONSE/)
})

test('pending or absent receipts cannot be displayed as successful reclamation', () => {
  const pending = { ...detail, state: 'running', code: 'GC_RUNNING', items: [{ hash: 'c'.repeat(64), size: 3, state: 'prepared', code: 'GC_STORAGE_OR_DATABASE_UNAVAILABLE', confirmed_at: 2000000 }], pending_objects: 1 }
  assert.equal(parseReclamationDetail(pending, plan.plan_id).reclaimed_objects, 0)
  for (const changed of [{ reclaimed_objects: 1 }, { reclaimed_bytes: 3 }, { pending_objects: 0 }, { items: [{ ...pending.items[0], hash: 'd'.repeat(64) }] }, { items: [pending.items[0], pending.items[0]] }]) assert.throws(() => parseReclamationDetail({ ...pending, ...changed }, plan.plan_id), /INVALID_RESPONSE/)
  assert.equal(parseReclamationDetail(detail, plan.plan_id).items.length, 0)
})

test('unsafe snapshots and changed permanent-retention policy are rejected', () => {
  for (const preview of [{ ...detail.preview, candidates: Array(101).fill(detail.preview.candidates[0]) }, { ...detail.preview, protected_counts: { revision: -1 } }, { ...detail.preview, sequence: 3 }, { ...detail.preview, policy: { ...detail.preview.policy, automatic_reclamation: true } }, { ...detail.preview, candidate_bytes: 2 }]) assert.throws(() => parseReclamationDetail({ ...detail, preview }, plan.plan_id), /INVALID_RESPONSE/)
  assert.throws(() => parseReclamationPage({ ...page, items: [{ ...plan, expired: true }] }), /INVALID_RESPONSE/)
  assert.equal(parseReclamationPage({ ...page, observed_at: 2000011, items: [{ ...plan, expired: true }] }).items[0].expired, true)
})

test('a late original-plan read is isolated after the session is cleared', async t => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  t.mock.method(globalThis, 'fetch', async path => path === '/sync/v1/auth/sessions' ? response(session) : pending)
  const api = new SyncApi(); await api.login('operator', 'controlled-password', 'test')
  const read = api.reclamationPlan(plan.plan_id), rejected = assert.rejects(read, /SESSION_CHANGED/)
  api.clear(); finish(response(detail)); await rejected
  assert.equal(api.signedIn, false)
})

test('ordinary roles stay forbidden and results from another filter are rejected', async t => {
  let forbidden = true
  t.mock.method(globalThis, 'fetch', async path => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    return forbidden ? response({ error: { code: 'OPERATIONS_FORBIDDEN' } }, 403) : response(page)
  })
  const api = new SyncApi(); await api.login('member', 'controlled-password', 'test')
  await assert.rejects(api.reclamationPlans(), /OPERATIONS_FORBIDDEN/)
  forbidden = false
  await assert.rejects(api.reclamationPlans({ state: 'complete' }), /INVALID_RESPONSE/)
})
