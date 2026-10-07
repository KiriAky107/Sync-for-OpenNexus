import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SyncApi } from '../src/api.ts'
import { execute, quotaBytes, validateReceipt } from '../src/adminIntent.ts'

const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status })
const session = { access_token: 'fixture-access', refresh_token: 'fixture-refresh', device_id: 'fixture-device', expires_in: 900 }
const intent = Object.freeze({ kind: 'vault_quota', user: 'a'.repeat(32), vault: 'b'.repeat(32), label: 'Fixture vault', body: Object.freeze({ operation_id: 'c'.repeat(32), expected_quota: 1024, quota: 2048 }) })
const completed = { schema_version: 1, operation_id: intent.body.operation_id, state: 'completed', confirmed_at: 100, kind: intent.kind, user_id: intent.user, vault_id: intent.vault, quota: intent.body.quota }

test('quota entry preserves exact bytes, including safe integer boundaries', () => {
  assert.equal(quotaBytes('0.5', 1024**3), 536870912)
  assert.equal(quotaBytes('.25', 1024), 256)
  assert.equal(quotaBytes('1.5e2', 1), 150)
  assert.equal(quotaBytes('9007199254740991', 1), Number.MAX_SAFE_INTEGER)
  for (const text of ['-1', 'NaN', 'Infinity', '9007199254740990.8', '9007199254740992', '1e100']) assert.throws(() => quotaBytes(text, 1), /INVALID_QUOTA/)
  assert.throws(() => quotaBytes('0.1', 1024**2), /INVALID_QUOTA/)
})

test('a receipt for a different operation, account, vault or amount cannot finish the reviewed action', () => {
  validateReceipt(completed, intent)
  validateReceipt({ schema_version: 1, operation_id: intent.body.operation_id, confirmed_at: 101, state: 'not_found' }, intent)
  for (const changes of [{ operation_id: 'd'.repeat(32) }, { user_id: 'd'.repeat(32) }, { vault_id: 'd'.repeat(32) }, { quota: 1 }, { kind: 'default_quota' }, { state: 'unknown' }, { confirmed_at: -1 }]) assert.throws(() => validateReceipt({ ...completed, ...changes }, intent), /INVALID_RESPONSE/)
})

test('unknown management response is queried without replay; explicit retry retains the frozen operation', async t => {
  const requests = []
  let writes = 0
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    requests.push({ path, method: init.method ?? 'GET', body: init.body })
    assert.equal(init.cache, 'no-store')
    if (init.method === 'PUT') {
      writes++
      assert.deepEqual(JSON.parse(init.body), intent.body)
      if (writes === 1) throw new TypeError('Controlled lost response')
      return response(completed)
    }
    return response({ schema_version: 1, operation_id: intent.body.operation_id, state: 'not_found', confirmed_at: 100 })
  })
  const api = new SyncApi()
  await api.login('fixture', 'controlled-password', 'console')
  await assert.rejects(execute(api, intent), /Controlled lost response/)
  validateReceipt(await api.adminOperation(intent.body.operation_id), intent)
  assert.equal(writes, 1)
  validateReceipt(await execute(api, intent), intent)
  assert.deepEqual(requests.map(item => item.method), ['PUT', 'GET', 'PUT'])
  assert.equal(requests[0].body, requests[2].body)
})

test('authentication rotation keeps the reviewed management payload unchanged', async t => {
  const bodies = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    if (path === '/sync/v1/auth/refresh') return response({ ...session, access_token: 'rotated' })
    bodies.push(init.body)
    if (init.headers.get('Authorization') !== 'Bearer rotated') return response({ error: { code: 'SESSION_EXPIRED' } }, 401)
    return response(completed)
  })
  const api = new SyncApi()
  await api.login('fixture', 'controlled-password', 'console')
  validateReceipt(await execute(api, intent), intent)
  assert.deepEqual(bodies, [JSON.stringify(intent.body), JSON.stringify(intent.body)])
})

test('a timed-out management request unlocks reconciliation without a later authenticated replay', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let finishRefresh, writes = 0
  const refresh = new Promise(resolve => { finishRefresh = resolve })
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    if (path === '/sync/v1/auth/refresh') return refresh
    if (init.method === 'PUT') { writes++; return response({ error: { code: 'SESSION_EXPIRED' } }, 401) }
    return response({ schema_version: 1, operation_id: intent.body.operation_id, state: 'not_found', confirmed_at: 100 })
  })
  const api = new SyncApi()
  await api.login('fixture', 'controlled-password', 'console')
  const pending = execute(api, intent)
  const rejected = assert.rejects(pending, /REQUEST_TIMEOUT/)
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(30_000)
  await rejected
  finishRefresh(response({ ...session, access_token: 'late-refresh' }))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(writes, 1)
  validateReceipt(await api.adminOperation(intent.body.operation_id), intent)
  assert.equal(api.signedIn, true)
})
