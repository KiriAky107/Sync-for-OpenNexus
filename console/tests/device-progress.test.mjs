import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SyncApi, parseDeviceProgress } from '../src/api.ts'

const vault = 'a'.repeat(32), account = 'b'.repeat(32)
const device = { device_id: 'f'.repeat(32), name: '<owned device>', revoked: 0, authenticated_at: 100,
  committed_sequence: 3, committed_at: 100, offered_sequence: null, offered_at: null }
const page = { schema_version: 1, observed_at: 200, activity_sample_seconds: 60, client_application_confirmed: false,
  offered_semantics: 'server_generated_page', vault_id: vault, vault_sequence: 3, items: [device], next_before: null }
const session = { access_token: 'owned-access', refresh_token: 'owned-refresh', device_id: 'owned-device', expires_in: 900 }
const response = (body, status = 200) => new Response(JSON.stringify(body), { status })

test('unknown and generated empty pages remain distinct from confirmed commits or client application', () => {
  const unknown = { ...device, authenticated_at: null, committed_sequence: null, committed_at: null }
  assert.equal(parseDeviceProgress({ ...page, items: [unknown] }, vault).items[0].committed_sequence, null)
  const offered = { ...unknown, offered_sequence: 0, offered_at: 150 }
  assert.equal(parseDeviceProgress({ ...page, items: [offered] }, vault).items[0].offered_sequence, 0)
  for (const changes of [{ client_application_confirmed: true }, { offered_semantics: 'client_applied' }, { activity_sample_seconds: 0 }, { vault_id: account }]) {
    assert.throws(() => parseDeviceProgress({ ...page, ...changes }, vault), /INVALID_RESPONSE/)
  }
})

test('incomplete, unsafe or impossible observation pairs cannot imply progress', () => {
  for (const changes of [{ committed_sequence: 4 }, { committed_sequence: 0 }, { committed_at: null },
    { committed_sequence: null }, { offered_at: 10 }, { authenticated_at: -1 },
    { offered_sequence: Number.MAX_SAFE_INTEGER + 1, offered_at: 10 }, { committed_at: 8640000000001 },
    { revoked: 2 }, { device_id: '../escape' }]) {
    assert.throws(() => parseDeviceProgress({ ...page, items: [{ ...device, ...changes }] }, vault), /INVALID_RESPONSE/)
  }
})

test('pagination rejects duplicate, reordered and replayed devices and unrelated cursors', () => {
  const second = { ...device, device_id: 'e'.repeat(32) }
  assert.deepEqual(parseDeviceProgress({ ...page, items: [second] }, vault, device.device_id).items, [second])
  for (const changes of [{ items: [device, device] }, { items: [second, device] }, { next_before: second.device_id }, { items: Array(21).fill(device) }]) {
    assert.throws(() => parseDeviceProgress({ ...page, ...changes }, vault), /INVALID_RESPONSE/)
  }
  assert.throws(() => parseDeviceProgress(page, vault, device.device_id), /INVALID_RESPONSE/)
})

test('own and administrator vault queries use exact scope, GET and no browser cache', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    assert.equal(init.cache, 'no-store'); assert.equal(init.method ?? 'GET', 'GET')
    calls.push(path); return response(page)
  })
  const api = new SyncApi(); await api.login('operator', 'controlled-password', 'test')
  await api.deviceProgress(vault); await api.deviceProgress(vault, null, account)
  assert.deepEqual(calls, [`/sync/v1/vaults/${vault}/devices/progress?limit=20`, `/sync/v1/admin/accounts/${account}/vaults/${vault}/devices/progress?limit=20`])
  for (const args of [['../escape'], [vault, '../escape'], [vault, null, '../escape']]) await assert.rejects(api.deviceProgress(...args), /INVALID_REQUEST/)
  assert.equal(calls.length, 2)
})

test('a late observation cannot return after a different identity signs in', async t => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  t.mock.method(globalThis, 'fetch', async path => path === '/sync/v1/auth/sessions' ? response(session) : pending)
  const api = new SyncApi(); await api.login('first', 'controlled-password', 'test')
  const read = api.deviceProgress(vault), rejected = assert.rejects(read, /SESSION_CHANGED/)
  await api.login('second', 'controlled-password', 'test'); finish(response(page)); await rejected
  assert.equal(api.signedIn, true)
})

test('ordinary administrator queries remain forbidden and old services fail visibly', async t => {
  let status = 403
  t.mock.method(globalThis, 'fetch', async path => path === '/sync/v1/auth/sessions'
    ? response(session) : response({ error: { code: status === 403 ? 'OPERATIONS_FORBIDDEN' : 'HTTP_404' } }, status))
  const api = new SyncApi(); await api.login('member', 'controlled-password', 'test')
  await assert.rejects(api.deviceProgress(vault, null, account), /OPERATIONS_FORBIDDEN/)
  status = 404; await assert.rejects(api.deviceProgress(vault), /HTTP_404/)
})
