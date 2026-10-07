<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { SyncApiError, type SyncApi, type AdminAccounts, type AdminDetails, type AdminDiagnostics, type AdminReceipt, type AdminVault, type Device } from './api'
import { execute, operationId, quotaBytes, validateReceipt, type AdminIntent } from './adminIntent'

const props = defineProps<{ api: SyncApi }>()
const emit = defineEmits<{ pending: [value: boolean]; expired: []; forbidden: []; refreshed: [] }>()
const accounts = shallowRef<AdminAccounts | null>(null), details = shallowRef<AdminDetails | null>(null), diagnostics = shallowRef<AdminDiagnostics | null>(null)
const selectedId = ref(''), readBusy = ref(false), mutationBusy = ref(false), diagnosticsBusy = ref(false), stale = ref(false), diagnosticsStale = ref(false)
const failure = ref(''), diagnosticsFailure = ref(''), notice = ref('')
const cursor = ref<string | null>(null), previous = ref<(string | null)[]>([])
const vaultCursor = ref<string | null>(null), deviceCursor = ref<string | null>(null)
const vaultPrevious = ref<(string | null)[]>([]), devicePrevious = ref<(string | null)[]>([])
const newUsername = ref(''), password = ref(''), confirmPassword = ref(''), newQuota = ref('1024'), newUnit = ref(1024**2)
const review = shallowRef<{ kind: 'default_quota'; label: string } | { kind: 'vault_quota'; label: string; vault: AdminVault } | { kind: 'revoke_device'; label: string; device: Device } | null>(null)
const amount = ref(''), unit = ref(1), acknowledged = ref(false)
const intent = shallowRef<AdminIntent | null>(null), receipt = shallowRef<AdminReceipt | null>(null)
const attempted = ref(false)
const reviewPanel = ref<HTMLElement | null>(null), createPanel = ref<HTMLElement | null>(null)
async function focusPanel(panel: typeof reviewPanel) { await nextTick(); panel.value?.scrollIntoView({ block: 'nearest' }); panel.value?.focus({ preventScroll: true }) }
function openCreate() { void focusPanel(createPanel) }
let alive = true, version = 0, diagnosticVersion = 0
const locked = computed(() => readBusy.value || mutationBusy.value || !!intent.value)
watch(() => mutationBusy.value || !!intent.value, value => emit('pending', value))
const units = [{ value: 1, name: 'B' }, { value: 1024, name: 'KiB' }, { value: 1024**2, name: 'MiB' }, { value: 1024**3, name: 'GiB' }]
function bytes(value: number) { let index = 0; while (value >= 1024 && index < 3) { value /= 1024; index++ }; return `${value.toFixed(index ? 1 : 0)} ${units[index]!.name}` }
const time = (value: number | null, millis = false) => value == null ? '尚未确认' : new Date(value * (millis ? 1 : 1000)).toLocaleString('zh-CN', { hour12: false })
function message(error: unknown): string {
  if (!props.api.signedIn) { emit('expired'); return '会话已失效，请重新登录。' }
  const code = error instanceof Error ? error.message : ''
  if (code === 'OPERATIONS_FORBIDDEN') { intent.value = null; emit('forbidden'); return '管理员权限已不可用。' }
  const known: Record<string, string> = {
    ACCOUNT_EXISTS: '账户名已存在，请重新核对。', ACCOUNT_NOT_FOUND: '账户已不可访问，请刷新列表。',
    QUOTA_CHANGED: '知识库配额已变化，请刷新后重新确认。', POLICY_CHANGED: '默认配额策略已变化，请重新确认。',
    QUOTA_IN_USE: '新配额不足以覆盖已登记对象和有效上传预留，请刷新账目。',
    DEVICE_NOT_FOUND: '设备已不可访问，请核对所属账户。', VAULT_NOT_FOUND: '知识库已不可访问，请核对所属账户。',
    IDEMPOTENCY_REUSED: '原操作编号与本次输入不一致，请结束核对后重新审核。',
    INVALID_QUOTA: '请填写非负、精确到字节且不超过安全整数范围的配额。',
    INVALID_REQUEST: '输入未通过服务器校验，请重新核对。', INVALID_RESPONSE: '返回结果无法匹配原操作，请保留编号继续核对。',
    REQUEST_TIMEOUT: '请求超时，请查询原操作结果；超时不代表操作没有执行。',
    DATABASE_UNAVAILABLE: '数据库暂时不可用。', STORAGE_UNAVAILABLE: '存储暂时不可用。',
  }
  return known[code] ?? '请求未完成，请检查连接后核对原结果。'
}
async function load(reset = false): Promise<void> {
  if (!alive || mutationBusy.value || intent.value || readBusy.value) return
  const ticket = ++version; readBusy.value = true; failure.value = ''; stale.value = !!details.value
  if (reset) { cursor.value = null; previous.value = [] }
  try {
    const page = await props.api.adminAccounts(cursor.value)
    if (!alive || ticket !== version) return
    accounts.value = page
    if (!selectedId.value) selectedId.value = page.items[0]?.id ?? ''
    if (selectedId.value) await readDetails(ticket)
    else { details.value = null; stale.value = false }
  } catch (error) { if (alive && ticket === version) failure.value = message(error) }
  finally { if (alive && ticket === version) readBusy.value = false }
}
async function readDetails(ticket: number): Promise<void> {
  const next = await props.api.adminAccount(selectedId.value, { vaultBefore: vaultCursor.value, deviceBefore: deviceCursor.value })
  if (alive && ticket === version) { details.value = next; stale.value = false }
}
async function selectAccount(id: string): Promise<void> {
  if (locked.value) return
  selectedId.value = id; details.value = null; review.value = null; acknowledged.value = false
  vaultCursor.value = deviceCursor.value = null; vaultPrevious.value = []; devicePrevious.value = []
  await load()
}
async function pageAccounts(earlier: boolean): Promise<void> {
  if (locked.value) return
  if (earlier) { if (!accounts.value?.next_before) return; previous.value.push(cursor.value); cursor.value = accounts.value.next_before }
  else { if (!previous.value.length) return; cursor.value = previous.value.pop() ?? null }
  selectedId.value = ''; details.value = null; review.value = null
  vaultCursor.value = deviceCursor.value = null; vaultPrevious.value = []; devicePrevious.value = []
  await load()
}
async function pageResources(kind: 'vault' | 'device', earlier: boolean): Promise<void> {
  if (locked.value || !details.value) return
  const history = kind === 'vault' ? vaultPrevious : devicePrevious, pointer = kind === 'vault' ? vaultCursor : deviceCursor
  const next = kind === 'vault' ? details.value.vaults_next_before : details.value.devices_next_before
  if (earlier) { if (!next) return; history.value.push(pointer.value); pointer.value = next }
  else { if (!history.value.length) return; pointer.value = history.value.pop() ?? null }
  review.value = null; acknowledged.value = false; await load()
}
async function readDiagnostics(): Promise<void> {
  if (!alive || diagnosticsBusy.value) return
  const ticket = ++diagnosticVersion; diagnosticsBusy.value = true; diagnosticsFailure.value = ''; diagnosticsStale.value = !!diagnostics.value
  try { const next = await props.api.adminDiagnostics(); if (alive && ticket === diagnosticVersion) { diagnostics.value = next; diagnosticsStale.value = false } }
  catch (error) { if (alive && ticket === diagnosticVersion) diagnosticsFailure.value = message(error) }
  finally { if (alive && ticket === diagnosticVersion) diagnosticsBusy.value = false }
}
function chooseDefault(): void {
  if (locked.value || stale.value || !details.value) return
  review.value = { kind: 'default_quota', label: `${details.value.account.username} · 新知识库默认配额` }
  amount.value = String(details.value.policy.default_quota); unit.value = 1; acknowledged.value = false; notice.value = ''
  void focusPanel(reviewPanel)
}
function chooseVault(vault: AdminVault): void {
  if (locked.value || stale.value) return
  review.value = { kind: 'vault_quota', label: `${details.value?.account.username} · ${vault.name}`, vault }
  amount.value = String(vault.quota); unit.value = 1; acknowledged.value = false; notice.value = ''
  void focusPanel(reviewPanel)
}
function chooseDevice(device: Device): void {
  if (locked.value || stale.value) return
  review.value = { kind: 'revoke_device', label: `${details.value?.account.username} · ${device.name}`, device }
  acknowledged.value = false; notice.value = ''
  void focusPanel(reviewPanel)
}
async function prepareCreate(): Promise<void> {
  if (locked.value) return
  failure.value = ''
  if (password.value !== confirmPassword.value) { failure.value = '两次输入的新账户密码不一致。'; return }
  try {
    const action: AdminIntent = Object.freeze({ kind: 'create_account', label: newUsername.value.trim(), body: Object.freeze({ operation_id: operationId(), username: newUsername.value.trim(), password: password.value, default_quota: quotaBytes(newQuota.value, newUnit.value) }) })
    password.value = confirmPassword.value = ''; review.value = null; acknowledged.value = false; intent.value = action; attempted.value = false; receipt.value = null; notice.value = ''
    void focusPanel(reviewPanel)
  } catch (error) { failure.value = message(error) }
}
function prepareReview(): void {
  if (locked.value || stale.value || !review.value || !details.value) return
  try {
    const selected = review.value, user = details.value.account.id, operation_id = operationId()
    const action: AdminIntent = selected.kind === 'default_quota'
      ? { kind: selected.kind, label: selected.label, user, body: Object.freeze({ operation_id, expected_revision: details.value.policy.revision, quota: quotaBytes(amount.value, unit.value) }) }
      : selected.kind === 'vault_quota'
        ? { kind: selected.kind, label: selected.label, user, vault: selected.vault.id, body: Object.freeze({ operation_id, expected_quota: selected.vault.quota, quota: quotaBytes(amount.value, unit.value) }) }
        : { kind: selected.kind, label: selected.label, user, device: selected.device.id, body: Object.freeze({ operation_id }) }
    intent.value = Object.freeze(action); acknowledged.value = false; attempted.value = false; receipt.value = null; failure.value = ''; notice.value = ''
    void focusPanel(reviewPanel)
  } catch (error) { failure.value = message(error) }
}
async function finish(result: AdminReceipt, action: AdminIntent): Promise<void> {
  validateReceipt(result, action); receipt.value = result
  if (result.state !== 'completed') { notice.value = '服务器未找到原操作回执。核对原请求后可按同一编号重试。'; return }
  intent.value = null; acknowledged.value = false; review.value = null; mutationBusy.value = false
  notice.value = `操作已完成 · ${action.label} · ${time(result.confirmed_at)}`
  if (action.kind === 'create_account') { selectedId.value = result.user_id!; newUsername.value = ''; vaultCursor.value = deviceCursor.value = null; vaultPrevious.value = []; devicePrevious.value = [] }
  if (action.kind === 'revoke_device' && action.device === props.api.deviceId) { emit('expired'); return }
  await load(); emit('refreshed'); void readDiagnostics()
}
async function send(retry = false): Promise<void> {
  if (!intent.value || mutationBusy.value || readBusy.value || !acknowledged.value || (!retry && attempted.value) || (retry && (!attempted.value || receipt.value?.state !== 'not_found'))) return
  const action = intent.value; mutationBusy.value = true; attempted.value = true; receipt.value = null; failure.value = ''
  try { const result = await execute(props.api, action); if (alive && intent.value === action) await finish(result, action) }
  catch (error) {
    if (alive && intent.value === action) {
      failure.value = message(error)
      if (error instanceof SyncApiError && [400, 404, 409, 422].includes(error.status)) {
        const explanation = failure.value; intent.value = null; review.value = null; acknowledged.value = false; mutationBusy.value = false
        await load(); if (alive) failure.value = explanation + (failure.value ? ' ' + failure.value : '')
      }
      else if (props.api.signedIn) failure.value += ' 请先查询原操作结果。'
    }
  } finally { if (alive) mutationBusy.value = false }
}
async function reconcile(): Promise<void> {
  if (!intent.value || !attempted.value || mutationBusy.value) return
  const action = intent.value; mutationBusy.value = true; failure.value = ''
  try { const result = await props.api.adminOperation(action.body.operation_id); if (alive && intent.value === action) await finish(result, action) }
  catch (error) { if (alive && intent.value === action) failure.value = message(error) }
  finally { if (alive) mutationBusy.value = false }
}
function closeIntent(): void {
  if (mutationBusy.value) return
  intent.value = null; receipt.value = null; review.value = null; acknowledged.value = false
  password.value = confirmPassword.value = ''; notice.value = '已清空本页核对记录。服务端操作结果仍可用原编号查询。'
  void load()
}
const dependencyText = computed(() => {
  const code = diagnostics.value?.dependencies.code
  const names: Record<string, string> = { READY: '依赖就绪', DATABASE_UNAVAILABLE: '数据库不可用', SCHEMA_INCOMPATIBLE: '数据库结构不兼容', STAGING_UNAVAILABLE: '暂存目录不可用', STAGING_INTEGRITY: '暂存读写校验失败', OBJECT_STORAGE_UNAVAILABLE: '对象存储不可用', OBJECT_STORAGE_INTEGRITY: '对象存储校验失败', DEPENDENCY_TIMEOUT: '依赖探测超时', DEPENDENCY_UNAVAILABLE: '依赖不可用' }
  return code ? names[code] ?? '等待确认' : '等待确认'
})
onMounted(() => { void load(); void readDiagnostics() })
onBeforeUnmount(() => { alive = false; version++; diagnosticVersion++; intent.value = null; password.value = confirmPassword.value = ''; emit('pending', false) })
</script>

<template>
  <section class="surface admin-diagnostics" aria-labelledby="diagnostics-title">
    <div class="surface-heading"><div><p>Diagnostics</p><h2 id="diagnostics-title">服务诊断</h2></div><button class="secondary-button" :disabled="diagnosticsBusy" @click="readDiagnostics">{{ diagnosticsBusy ? '正在检测…' : '刷新诊断' }}</button></div>
    <p v-if="diagnosticsFailure" class="operations-error" role="alert">{{ diagnosticsFailure }}</p>
    <template v-if="diagnostics">
      <p class="operations-updated">{{ diagnosticsStale ? '上次读取，尚未刷新' : '本次读取' }} · {{ time(diagnostics.confirmed_at) }} · {{ diagnostics.dependencies.cached ? '缓存探测' : '本次探测' }}</p>
      <div class="admin-diagnostic-grid"><article :class="{ unhealthy: !diagnostics.dependencies.ready }"><small>依赖</small><strong>{{ dependencyText }}</strong><span>探测确认 {{ time(diagnostics.dependencies.checked_at, true) }}</span><span v-if="diagnostics.dependencies.probe_pending">原探测仍在运行，后续读取会核对它。</span></article><article><small>账目异常</small><strong>{{ diagnostics.accounting_mismatches }} 个知识库</strong><span>需核对登记对象与计费用量</span></article><article><small>回收待核对</small><strong>{{ diagnostics.pending_reclamation_objects }} 个对象</strong><span>使用原回收计划继续</span></article></div>
      <p v-if="diagnostics.upload_maintenance" class="surface-intro admin-maintenance">最近暂存清理 {{ time(diagnostics.upload_maintenance.finished_at) }} · {{ diagnostics.upload_maintenance.duration_ms }} ms · 删除 {{ diagnostics.upload_maintenance.removed }} 个 · 文件系统失败 {{ diagnostics.upload_maintenance.filesystem_failures }} / 元数据失败 {{ diagnostics.upload_maintenance.metadata_failures }}<br>累计删除 {{ diagnostics.upload_maintenance.total_removed }} 个 · 累计失败 {{ diagnostics.upload_maintenance.total_failures }} 次</p>
    </template>
  </section>
  <section class="surface admin-accounts" aria-labelledby="admin-title">
    <div class="surface-heading"><div><p>Accounts &amp; policies</p><h2 id="admin-title">账户与配额</h2></div><div class="admin-heading-actions"><button class="secondary-button" :disabled="locked" @click="openCreate">开通账户</button><button class="secondary-button" :disabled="locked" @click="load(true)">刷新管理数据</button></div></div>
    <p v-if="failure" class="operations-error" role="alert">{{ failure }}</p><p v-if="notice" class="storage-notice" role="status">{{ notice }}</p>
    <p v-if="readBusy" class="operations-updated" role="status">正在读取账户和资源…</p>
    <div class="admin-layout">
      <aside class="admin-directory">
        <h3>账户列表</h3><p v-if="accounts" class="operations-updated">服务器确认 {{ time(accounts.confirmed_at) }}</p>
        <button v-for="account in accounts?.items" :key="account.id" class="admin-account-button" :class="{ selected: selectedId === account.id }" :disabled="locked" @click="selectAccount(account.id)"><strong>{{ account.username }}</strong><small>{{ account.id }}</small></button>
        <nav class="admin-pagination" aria-label="管理账户分页"><button class="secondary-button" :disabled="locked || !previous.length" @click="pageAccounts(false)">上一页</button><span>{{ previous.length + 1 }}</span><button class="secondary-button" :disabled="locked || !accounts?.next_before" @click="pageAccounts(true)">下一页</button></nav>
      </aside>
      <div class="admin-resources">
        <template v-if="details">
          <div class="admin-account-heading"><div><h3>{{ details.account.username }}</h3><code>{{ details.account.id }}</code></div><span class="secure-badge">{{ stale ? '待刷新' : '服务器确认' }}</span></div>
          <p class="operations-updated">{{ time(details.confirmed_at) }} · {{ details.totals.vault_count }} 个知识库 · 登记 {{ bytes(details.totals.charged_bytes) }} / 配额 {{ bytes(details.totals.quota_bytes) }}</p>
          <div class="admin-default"><span>新知识库默认配额 <strong>{{ bytes(details.policy.default_quota) }}</strong></span><button class="secondary-button" :disabled="locked || stale" @click="chooseDefault">调整默认配额</button></div>
          <h3>知识库</h3><p v-if="!details.vaults.length" class="empty-state">本页没有知识库。</p>
          <div class="admin-resource-list"><article v-for="vault in details.vaults" :key="vault.id" class="admin-resource"><div><strong>{{ vault.name }}</strong><small>{{ vault.id }}</small><p>登记 {{ bytes(vault.used) }} · 上传预留 {{ bytes(vault.reserved_bytes) }} · 配额 {{ bytes(vault.quota) }}</p></div><button class="secondary-button" :disabled="locked || stale" @click="chooseVault(vault)">调整配额</button></article></div>
          <nav class="admin-pagination" aria-label="管理知识库分页"><button class="secondary-button" :disabled="locked || !vaultPrevious.length" @click="pageResources('vault',false)">上一页</button><span>{{ vaultPrevious.length + 1 }}</span><button class="secondary-button" :disabled="locked || !details.vaults_next_before" @click="pageResources('vault',true)">下一页</button></nav>
          <h3>设备</h3><div class="admin-resource-list"><article v-for="device in details.devices" :key="device.id" class="admin-resource"><div><strong>{{ device.name }}</strong><small>{{ device.id }}</small><p>{{ device.revoked ? '已撤销' : '可访问' }}{{ device.id === api.deviceId ? ' · 当前设备' : '' }}</p></div><button v-if="!device.revoked" class="danger-button" :disabled="locked || stale" @click="chooseDevice(device)">撤销设备</button></article></div>
          <nav class="admin-pagination" aria-label="管理设备分页"><button class="secondary-button" :disabled="locked || !devicePrevious.length" @click="pageResources('device',false)">上一页</button><span>{{ devicePrevious.length + 1 }}</span><button class="secondary-button" :disabled="locked || !details.devices_next_before" @click="pageResources('device',true)">下一页</button></nav>
        </template>
        <p v-else-if="!readBusy" class="empty-state">选择账户查看知识库、配额和设备。</p>
      </div>
    </div>
    <form v-if="!intent" ref="createPanel" tabindex="-1" class="admin-create" @submit.prevent="prepareCreate">
      <h3>开通新账户</h3><fieldset :disabled="locked"><div class="admin-form-grid"><label>新账户名<input v-model="newUsername" autocomplete="off" maxlength="80" required></label><label>默认配额<div class="admin-quota"><input v-model="newQuota" type="text" inputmode="decimal" maxlength="64" required aria-label="新账户默认配额"><select v-model="newUnit" aria-label="新账户配额单位"><option v-for="item in units" :key="item.value" :value="item.value">{{ item.name }}</option></select></div></label><label>新账户密码<input v-model="password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label><label>确认新账户密码<input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label></div><button class="secondary-button" type="submit">审核新账户</button></fieldset>
    </form>
    <form v-if="review && !intent" ref="reviewPanel" tabindex="-1" class="admin-review" @submit.prevent="prepareReview"><h3>{{ review.label }}</h3><template v-if="review.kind !== 'revoke_device'"><label>新配额<div class="admin-quota"><input v-model="amount" type="text" inputmode="decimal" maxlength="64" required><select v-model="unit" aria-label="新配额单位"><option v-for="item in units" :key="item.value" :value="item.value">{{ item.name }}</option></select></div></label><p>{{ review.kind === 'default_quota' ? '此策略用于之后创建的知识库。' : '新配额须覆盖登记对象与有效上传预留。' }}</p></template><p v-else>撤销后，此设备的下一次请求会被拒绝。</p><button class="secondary-button" type="submit" :disabled="locked || stale">审核管理操作</button></form>
    <div v-if="intent" ref="reviewPanel" tabindex="-1" class="admin-review"><h3>确认管理操作</h3><p>{{ intent.label }}</p><code>操作 {{ intent.body.operation_id }}</code><p v-if="intent.kind !== 'revoke_device'">{{ intent.kind === 'create_account' ? '新账户默认配额' : '新配额' }} {{ bytes(intent.kind === 'create_account' ? intent.body.default_quota : intent.body.quota) }} · 精确 {{ intent.kind === 'create_account' ? intent.body.default_quota : intent.body.quota }} 字节</p><p v-else>此设备的下一次认证请求将被拒绝。</p>
      <label v-if="!attempted" class="admin-ack"><input v-model="acknowledged" type="checkbox">我已核对账户、资源和配额</label><button v-if="!attempted" class="danger-button" :disabled="!acknowledged" @click="send()">确认执行管理操作</button>
      <p v-if="mutationBusy" role="status">正在请求，请保留原操作编号…</p><button v-if="attempted" class="secondary-button" :disabled="mutationBusy" @click="reconcile">查询原管理结果</button><button v-if="receipt?.state === 'not_found'" class="danger-button" :disabled="mutationBusy" @click="send(true)">按原编号重试管理操作</button><button class="secondary-button" :disabled="mutationBusy" @click="closeIntent">结束本地核对</button>
      <p v-if="receipt" class="operations-updated">核对确认 {{ time(receipt.confirmed_at) }} · {{ receipt.state === 'not_found' ? '未找到原回执' : '已完成' }}</p>
    </div>
  </section>
  <section class="surface admin-guide"><div class="surface-heading"><div><p>Deploy &amp; recover</p><h2>部署与恢复</h2></div></div><ol><li><strong>首次部署：</strong>初始化数据库和对象存储，固定初始账户凭据，再配置获授权的管理员账户。</li><li><strong>升级前：</strong>结束未完成上传，创建新的备份并完整校验，核对下面的运维回执；保持原数据库和对象桶。</li><li><strong>需要恢复时：</strong>使用空数据库与空对象桶，恢复后检查依赖，再接入两台测试设备确认同步。</li></ol><p><a href="https://github.com/KiriAky107/Sync-for-OpenNexus#backup-and-restore" target="_blank" rel="noreferrer">查看部署、备份与恢复命令</a></p></section>
</template>
