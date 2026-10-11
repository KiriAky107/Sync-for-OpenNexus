import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'
import { watchEffect, nextTick } from 'vue'
import { messages } from '../src/messages.ts'
import { localizedAdminLabel, localizedDate, localizedNumber, preferredLocale, preferenceKey, translate } from '../src/localeCore.ts'
import { locale, setLocale, t } from '../src/locale.ts'
import { SyncApi } from '../src/api.ts'
import { execute, validateReceipt } from '../src/adminIntent.ts'

test('language preference accepts only non-sensitive language values and tolerates disabled storage', () => {
  assert.equal(preferredLocale(() => 'en', 'zh-CN'), 'en')
  assert.equal(preferredLocale(() => 'zh-CN', 'en-US'), 'zh-CN')
  assert.equal(preferredLocale(() => 'invalid', 'zh-Hant'), 'zh-CN')
  assert.equal(preferredLocale(() => { throw Error('blocked') }, 'en-GB'), 'en')
  assert.equal(preferredLocale(() => null, 'fr-FR'), 'en')
  assert.equal(preferenceKey, 'opennexus-sync-language')
})

test('all console surfaces localize UI labels while original names, paths, content and receipts remain literal', () => {
  const chinese = /[\u3400-\u9fff]/
  for (const [key, pair] of Object.entries(messages)) {
    assert.equal(pair.length, 2, key)
    assert.equal(chinese.test(pair[1]), false, key)
  }
  for (const name of ['App.vue','VaultHistory.vue','VaultStorage.vue','AdminPanel.vue','ReclamationPanel.vue','DeviceProgressPanel.vue','OperationsOverview.vue']) {
    const source = fs.readFileSync(new URL('../src/' + name, import.meta.url), 'utf8')
    function inspect(node) {
      if (node.type === 2) assert.equal(chinese.test(node.content), false, `${name}: ${node.content}`)
      for (const prop of node.props || []) if (prop.type === 6 && prop.value) assert.equal(chinese.test(prop.value.content), false, `${name}: ${prop.name}`)
      for (const child of node.children || []) inspect(child)
    }
    inspect(parse(source).descriptor.template.ast)
    for (const match of source.matchAll(/\bt\('([^'\n]+)'/g)) assert.ok(Object.hasOwn(messages, match[1]), `${name}: ${match[1]}`)
    assert.doesNotMatch(source, /\{\{\s*t\((?:vault|device|file|revision|head|item|detail)\.(?:name|path|text|hash|code|actor_id|operation_id)/)
    assert.doesNotMatch(source, /toLocaleString\('zh-CN'/)
    const script = parse(source).descriptor.scriptSetup.content
    for (const match of script.matchAll(/([A-Z_0-9]+): '([^']+)'/g)) if (chinese.test(match[2])) assert.ok(Object.hasOwn(messages, match[2]), `${name}: ${match[1]}`)
  }
})

test('locale formats dates and counts without changing timestamps or original parameter text', () => {
  const value = '2026-10-11T14:10:11Z'
  assert.equal(localizedDate(value, 'en'), new Date(value).toLocaleString('en-US', { hour12: false }))
  assert.equal(localizedDate(value, 'zh-CN'), new Date(value).toLocaleString('zh-CN', { hour12: false }))
  assert.equal(localizedNumber(1000, 'en'), (1000).toLocaleString('en-US'))
  assert.equal(localizedDate('original invalid date', 'en'), 'original invalid date')
  const raw = '刷新 <script>{name}</script>'
  assert.equal(translate('已创建知识库', 'en', { name: raw }), 'Created vault: ' + raw)
  assert.equal(translate(raw, 'en'), raw)
  assert.equal(translate('用量总述', 'en', { charged: '128 B', quota: '1 GiB', available: '1024 MiB', files: 1, logical: '64 B' }), 'Charged 128 B / 1 GiB · Available 1024 MiB. Current files: 1 · Logical size: 64 B. Identical objects are charged once. History is retained indefinitely.')
  assert.equal(translate('上传总述', 'en', { pending: 1, expired: 0 }), 'Pending uploads: 1 · Expired: 0. Expired uploads no longer reserve quota. Records remain for retries when staging deletion fails.')
  for (const key of ['constructor','__proto__','toString']) assert.equal(translate(key, 'en'), key)
})

test('management labels localize only their typed UI suffix and preserve user-controlled names', () => {
  const name = '新知识库默认配额 中文 {date}'
  const label = name + ' · 新知识库默认配额'
  assert.equal(localizedAdminLabel('default_quota', label, 'en'), name + ' · Default quota for new vaults')
  assert.equal(localizedAdminLabel('default_quota', label, 'zh-CN'), label)
  assert.equal(localizedAdminLabel('vault_quota', label, 'en'), label)
  assert.equal(localizedAdminLabel('create_account', name, 'en'), name)
  assert.equal(localizedAdminLabel('default_quota', name + ' original unknown label', 'en'), name + ' original unknown label')
})

test('switching language during a frozen CAS operation sends no request and keeps exact original reconciliation', async context => {
  const stored = [], calls = []
  globalThis.document = { documentElement: { lang: '' } }
  globalThis.localStorage = { setItem: (key, value) => stored.push([key, value]) }
  const intent = Object.freeze({ kind: 'vault_quota', user: 'a'.repeat(32), vault: 'b'.repeat(32), label: '刷新 中文 {date}', body: Object.freeze({ operation_id: 'c'.repeat(32), expected_quota: 1024, quota: 2048 }) })
  const serialized = JSON.stringify(intent)
  const receipt = { schema_version: 1, operation_id: intent.body.operation_id, state: 'completed', confirmed_at: 100, kind: intent.kind, user_id: intent.user, vault_id: intent.vault, quota: intent.body.quota }
  context.mock.method(globalThis, 'fetch', async (path, init) => {
    calls.push({ path, method: init?.method ?? 'GET', body: init?.body })
    if (path === '/sync/v1/auth/sessions') return new Response(JSON.stringify({session:{access_token:'owned-access',refresh_token:'owned-refresh',device_id:'owned-device',expires_in:900}}))
    if (init?.method === 'PUT') throw new TypeError('lost response after commit')
    return new Response(JSON.stringify(receipt))
  })
  const api = new SyncApi()
  await api.login('owned', 'owned-existing-secret', 'owned-console')
  let displayed = '', failure = ''
  const stop = watchEffect(() => { displayed = t('确认执行管理操作'); failure = t('请求超时，请查询原操作结果；超时不代表操作没有执行。') })
  const count = calls.length
  setLocale('en'); await nextTick()
  assert.equal(displayed, 'Confirm administration action')
  assert.match(failure, /timed out/)
  assert.equal(document.documentElement.lang, 'en')
  assert.equal(calls.length, count); assert.equal(JSON.stringify(intent), serialized)
  await assert.rejects(execute(api, intent), /lost response/)
  assert.equal(calls.at(-1).body, JSON.stringify(intent.body))
  setLocale('zh-CN'); await nextTick()
  assert.equal(displayed, '确认执行管理操作'); assert.equal(JSON.stringify(intent), serialized)
  validateReceipt(await api.adminOperation(intent.body.operation_id), intent)
  assert.equal(calls.filter(call => call.method === 'PUT').length, 1)
  assert.deepEqual(stored, [[preferenceKey,'en'],[preferenceKey,'zh-CN']])
  globalThis.localStorage.setItem = () => { throw Error('blocked') }
  setLocale('en'); await nextTick(); assert.equal(locale.value, 'en')
  setLocale('invalid'); assert.equal(locale.value, 'en')
  stop(); api.clear(); delete globalThis.document; delete globalThis.localStorage
})
