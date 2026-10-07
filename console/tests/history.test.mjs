import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SyncApi, SyncApiError } from '../src/api.ts'
import { compareText } from '../src/textDiff.ts'

const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
const session = { access_token: 'access-fixture', refresh_token: 'refresh-fixture', device_id: 'device-fixture', expires_in: 900, must_change_credentials: false }

test('history pagination keeps the snapshot and encodes a literal path filter', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    calls.push(new URL(path, 'https://fixture.invalid'))
    assert.equal(init.headers.get('Authorization'), 'Bearer access-fixture')
    return response({ items: [], boundary: 32, next_before: null, has_more: false })
  })
  const api = new SyncApi(); await api.login('fixture', 'controlled-password', 'fixture')
  await api.files('vault/encoded', { q: '笔记/%_ &.md', includeDeleted: true, boundary: 32, before: 21 })
  await api.history('vault', 'file/id', { boundary: 32, before: 9 })
  assert.equal(calls[0].pathname, '/sync/v1/vaults/vault%2Fencoded/files')
  assert.equal(calls[0].searchParams.get('q'), '笔记/%_ &.md')
  assert.equal(calls[0].searchParams.get('include_deleted'), 'true')
  assert.equal(calls[0].searchParams.get('boundary'), '32')
  assert.equal(calls[1].pathname, '/sync/v1/vaults/vault/history/file%2Fid')
  assert.equal(calls[1].searchParams.get('before'), '9')
})

test('restore retains the exact operation and reviewed revision through token rotation', async t => {
  const bodies = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    if (path === '/sync/v1/auth/refresh') return response({ ...session, access_token: 'next-access', refresh_token: 'next-refresh' })
    bodies.push(init.body)
    if (bodies.length === 1) return response({ error: { code: 'SESSION_EXPIRED' } }, 401)
    return response({ sequence: 8, operation_id: JSON.parse(init.body).operation_id })
  })
  const api = new SyncApi(); await api.login('fixture', 'controlled-password', 'fixture')
  const intent = Object.freeze({ operation_id: 'original-operation-identity', source_revision: 2, base_revision: 7, path: 'renamed.md' })
  const restored = await api.restore('vault', 'file', intent)
  assert.equal(restored.sequence, 8)
  assert.deepEqual(bodies.map(JSON.parse), [intent, intent])
})

test('conflict details survive API handling and receipt reconciliation is read only', async t => {
  const methods = []
  t.mock.method(globalThis, 'fetch', async (path, init) => {
    if (path === '/sync/v1/auth/sessions') return response(session)
    methods.push(init.method ?? 'GET')
    if (path.endsWith('/restore')) return response({ error: { code: 'REVISION_CONFLICT', details: { current: { sequence: 20, path: 'later.md' } } } }, 409)
    return response({ sequence: 12, operation_id: 'original-operation-identity' })
  })
  const api = new SyncApi(); await api.login('fixture', 'controlled-password', 'fixture')
  await assert.rejects(api.restore('vault', 'file', { operation_id: 'original-operation-identity', source_revision: 2, base_revision: 7, path: null }), error => {
    assert.ok(error instanceof SyncApiError); assert.equal(error.status, 409); assert.equal(error.details.current.sequence, 20); return true
  })
  assert.equal((await api.restoreResult('vault', 'file', 'original-operation-identity')).sequence, 12)
  assert.deepEqual(methods, ['POST', 'GET'])
})

test('comparison preserves whitespace, trailing newlines and literal HTML through both projections', () => {
  const current = 'unchanged\n\t<tag>old</tag>\r\nline with spaces  \n'
  const source = 'unchanged\n\t<tag>new</tag>\nline with spaces\nextra'
  const compared = compareText(current, source)
  assert.equal(compared.kind, 'diff')
  assert.equal(compared.lines.filter(line => line.kind !== 'add').map(line => line.text).join('\n'), current)
  assert.equal(compared.lines.filter(line => line.kind !== 'remove').map(line => line.text).join('\n'), source)
  assert.ok(compared.lines.some(line => line.kind === 'add' && line.text.includes('<tag>')))
})

test('large unequal comparisons bound work; byte-identical text has no synthetic diff', () => {
  assert.deepEqual(compareText('x\n'.repeat(20000), 'y\n'.repeat(20000)), { kind: 'large' })
  assert.deepEqual(compareText('a'.repeat(200000), 'b'), { kind: 'large' })
  assert.deepEqual(compareText('a'.repeat(200000), 'a'.repeat(200000)), { kind: 'equal' })
})
