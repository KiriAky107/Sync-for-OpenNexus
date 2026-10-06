import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SyncApi } from '../src/api.ts'

const session = suffix => ({ access_token: 'access-' + suffix, refresh_token: 'refresh-' + suffix, device_id: 'device-' + suffix, expires_in: 900, must_change_credentials: false })
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

test('concurrent account and operation requests rotate the single-use refresh token once', async t => {
  const refresh = deferred(), entered = deferred()
  let rotations = 0
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session('old'))
    if (path === '/sync/v1/auth/refresh') {
      rotations++
      assert.equal(JSON.parse(init.body).refresh_token, 'refresh-old')
      entered.resolve()
      return refresh.promise
    }
    if (init.headers.get('Authorization') === 'Bearer access-old') return response({ error: { code: 'SESSION_EXPIRED' } }, 401)
    assert.equal(init.headers.get('Authorization'), 'Bearer access-new')
    return response({ items: [] })
  })
  const api = new SyncApi()
  await api.login('fixture', 'controlled-password', 'console')
  const requests = [api.vaults(), api.devices(), api.operations()]
  await entered.promise
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(rotations, 1)
  refresh.resolve(response(session('new')))
  await Promise.all(requests)
  assert.equal(api.deviceId, 'device-new')
})

for (const next of ['clear', 'new-login']) test('late refresh cannot restore the previous session after ' + next, async t => {
  const refresh = deferred(), entered = deferred()
  let logins = 0
  t.mock.method(globalThis, 'fetch', async (path) => {
    if (path === '/sync/v1/auth/sessions') return response(session(++logins === 1 ? 'old' : 'current'))
    if (path === '/sync/v1/auth/refresh') { entered.resolve(); return refresh.promise }
    return response({ error: { code: 'SESSION_EXPIRED' } }, 401)
  })
  const api = new SyncApi()
  await api.login('fixture', 'controlled-password', 'console')
  const pending = api.operations()
  const rejected = assert.rejects(pending, /SESSION_CHANGED/)
  await entered.promise
  if (next === 'clear') api.clear()
  else await api.login('other', 'controlled-other-password', 'other-console')
  refresh.resolve(response(session('stale')))
  await rejected
  assert.equal(api.signedIn, next !== 'clear')
  assert.equal(api.deviceId, next === 'clear' ? '' : 'device-current')
})

test('operation replies from an earlier account never become the next account data', async t => {
  const pendingResponse = deferred()
  let logins = 0
  t.mock.method(globalThis, 'fetch', async (path) => path === '/sync/v1/auth/sessions' ? response(session(String(++logins))) : pendingResponse.promise)
  const api = new SyncApi()
  await api.login('first', 'controlled-password', 'console')
  const pending = api.operations(10, 42)
  const rejected = assert.rejects(pending, /SESSION_CHANGED/)
  await api.login('second', 'controlled-password', 'console')
  pendingResponse.resolve(response({ items: [{ code: 'old-account' }] }))
  await rejected
  assert.equal(api.deviceId, 'device-2')
})

test('late login response cannot sign in after its caller cleared the session', async t => {
  const pendingResponse = deferred()
  t.mock.method(globalThis, 'fetch', async () => pendingResponse.promise)
  const api = new SyncApi()
  const pending = api.login('fixture', 'controlled-password', 'console')
  const rejected = assert.rejects(pending, /SESSION_CHANGED/)
  api.clear()
  pendingResponse.resolve(response(session('stale')))
  await rejected
  assert.equal(api.signedIn, false)
})
