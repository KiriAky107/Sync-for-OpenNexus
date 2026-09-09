<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { SyncApi, type Device, type ServiceStatus, type Vault } from './api'

const api = new SyncApi()
const service = reactive<ServiceStatus>({ health: false, ready: false, protocol: 1, maxObjectSize: 100 * 1024 * 1024 })
const checking = ref(true)
const signedIn = ref(false)
const busy = ref(false)
const username = ref('')
const password = ref('')
const deviceName = ref('OpenNexus Web Console')
const sessionLabel = ref('')
const newVaultName = ref('')
const vaults = ref<Vault[]>([])
const devices = ref<Device[]>([])
const toast = ref('')
const toastError = ref(false)
let toastTimer: number | undefined
let statusTimer: number | undefined

const used = computed(() => vaults.value.reduce((total, vault) => total + Number(vault.used || 0), 0))
const quota = computed(() => vaults.value.reduce((total, vault) => total + Number(vault.quota || 0), 0))
const activeDevices = computed(() => devices.value.filter(device => !device.revoked).length)

function formatBytes(value: number) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = Number(value) || 0
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

function shortId(value: string) { return `${value.slice(0, 12)}…` }
function usagePercent(vault: Vault) { return Math.min(100, vault.quota ? vault.used / vault.quota * 100 : 0) }
function firstCharacter(value: string, fallback: string) { return value.trim().slice(0, 1).toUpperCase() || fallback }

function notify(text: string, error = false) {
  toast.value = text
  toastError.value = error
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 4800)
}

async function refreshStatus() {
  checking.value = true
  try { Object.assign(service, await api.status()) }
  finally { checking.value = false }
}

async function loadAccount() {
  const [vaultPayload, devicePayload] = await Promise.all([api.vaults(), api.devices()])
  vaults.value = vaultPayload.items
  devices.value = devicePayload.items
}

async function refreshAccount() {
  if (busy.value) return
  busy.value = true
  try {
    await loadAccount()
    notify('账户数据已刷新')
  } catch (error) {
    if (!api.signedIn) leaveConsole()
    notify(error instanceof Error ? error.message : 'REFRESH_FAILED', true)
  } finally { busy.value = false }
}

function leaveConsole() {
  api.clear()
  signedIn.value = false
  password.value = ''
  vaults.value = []
  devices.value = []
}

async function signIn() {
  if (busy.value) return
  busy.value = true
  const secret = password.value
  const account = username.value.trim()
  const device = deviceName.value.trim()
  password.value = ''
  try {
    await api.login(account, secret, device)
    sessionLabel.value = `${account} · ${device}`
    signedIn.value = true
    await loadAccount()
    notify('设备会话已建立')
  } catch (error) {
    leaveConsole()
    notify(error instanceof Error ? error.message : 'LOGIN_FAILED', true)
  } finally { busy.value = false }
}

async function createVault() {
  const name = newVaultName.value.trim()
  if (!name || busy.value) return
  busy.value = true
  try {
    await api.createVault(name)
    newVaultName.value = ''
    await loadAccount()
    notify(`已创建 Vault：${name}`)
  } catch (error) {
    notify(error instanceof Error ? error.message : 'CREATE_VAULT_FAILED', true)
  } finally { busy.value = false }
}

async function revokeDevice(device: Device) {
  if (!window.confirm(`撤销设备“${device.name}”？该设备的下一次请求会被拒绝。`)) return
  busy.value = true
  try {
    await api.revokeDevice(device.id)
    if (device.id === api.deviceId) {
      leaveConsole()
      notify('当前设备已撤销')
    } else {
      await loadAccount()
      notify(`已撤销设备：${device.name}`)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : 'REVOKE_FAILED', true)
  } finally { busy.value = false }
}

async function logout() {
  if (busy.value) return
  busy.value = true
  try { await api.logout() }
  finally {
    leaveConsole()
    busy.value = false
    notify('会话已退出')
  }
}

onMounted(() => {
  void refreshStatus()
  statusTimer = window.setInterval(() => { void refreshStatus() }, 15_000)
})
onBeforeUnmount(() => {
  window.clearInterval(statusTimer)
  window.clearTimeout(toastTimer)
  leaveConsole()
})
</script>

<template>
  <div class="ambient ambient-one" aria-hidden="true" />
  <div class="ambient ambient-two" aria-hidden="true" />
  <header class="topbar">
    <a class="brand" href="/console" aria-label="OpenNexus Sync Console">
      <span class="brand-mark" aria-hidden="true"><i /><i /><i /></span>
      <span><strong>OpenNexus</strong><small>Sync Console</small></span>
    </a>
    <div class="service-strip" aria-label="服务状态">
      <span class="status-chip" :class="{ ok: service.health, bad: !checking && !service.health }"><i /><b>服务</b><em>{{ checking ? '检测中' : service.health ? '在线' : '离线' }}</em></span>
      <span class="status-chip" :class="{ ok: service.ready, bad: !checking && !service.ready }"><i /><b>依赖</b><em>{{ checking ? '检测中' : service.ready ? '就绪' : '不可用' }}</em></span>
      <button class="icon-button" type="button" :disabled="checking" aria-label="刷新服务状态" title="刷新服务状态" @click="refreshStatus">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" /></svg>
      </button>
    </div>
  </header>

  <main>
    <section v-if="!signedIn" class="hero">
      <div class="hero-copy">
        <div class="eyebrow"><span /> Self-hosted workspace sync</div>
        <h1>让知识在设备之间<br><em>安静地抵达。</em></h1>
        <p>OpenNexus Sync 为每个 Vault 保留完整修订历史、附件完整性校验和设备级撤销。此控制台只连接当前服务器，不在浏览器持久保存密码或令牌。</p>
        <div class="protocol-grid" aria-label="协议能力">
          <article><strong>v{{ service.protocol }}</strong><span>同步协议</span></article>
          <article><strong>{{ formatBytes(service.maxObjectSize) }}</strong><span>单对象上限</span></article>
          <article><strong>SHA-256</strong><span>内容完整性</span></article>
        </div>
      </div>

      <section class="login-card" aria-labelledby="login-title">
        <div class="card-glow" aria-hidden="true" />
        <div class="card-heading">
          <span class="lock-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="11" rx="3" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg></span>
          <div><p>设备会话</p><h2 id="login-title">登录 Sync Server</h2></div>
        </div>
        <form @submit.prevent="signIn">
          <label>账户<input v-model="username" autocomplete="username" maxlength="80" required placeholder="输入 Sync 账户"></label>
          <label>密码<input v-model="password" type="password" autocomplete="current-password" minlength="12" maxlength="256" required placeholder="至少 12 个字符"></label>
          <label>设备名称<input v-model="deviceName" autocomplete="off" maxlength="120" required></label>
          <button class="primary-button" type="submit" :disabled="busy || !password"><span>{{ busy ? '正在建立会话…' : '建立安全会话' }}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg></button>
        </form>
        <p class="privacy-note"><span>●</span>密码在发出请求前即从输入框清除；会话令牌只保存在当前页面内存。</p>
      </section>
    </section>

    <section v-else class="console-view">
      <div class="console-heading">
        <div><div class="eyebrow"><span /> Connected workspace</div><h1>同步空间</h1><p>{{ sessionLabel }}</p></div>
        <button class="secondary-button" type="button" :disabled="busy" @click="logout">退出登录</button>
      </div>

      <div class="metric-grid">
        <article><span>远端 Vault</span><strong>{{ vaults.length }}</strong><small>当前账户可访问</small></article>
        <article><span>已使用空间</span><strong>{{ formatBytes(used) }}</strong><small>总配额 {{ formatBytes(quota) }}</small></article>
        <article><span>设备</span><strong>{{ devices.length }}</strong><small>{{ activeDevices }} 台可访问</small></article>
        <article><span>协议</span><strong>v{{ service.protocol }}</strong><small>历史长期保留</small></article>
      </div>

      <div class="content-grid">
        <section class="surface">
          <div class="surface-heading">
            <div><p>Vaults</p><h2>远端知识库</h2></div>
            <button class="icon-button" type="button" :disabled="busy" aria-label="刷新账户数据" title="刷新账户数据" @click="refreshAccount">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" /></svg>
            </button>
          </div>
          <form class="create-form" @submit.prevent="createVault">
            <label>创建新 Vault</label>
            <div><input v-model="newVaultName" maxlength="120" required placeholder="例如：产品知识库"><button type="submit" :disabled="busy || !newVaultName.trim()">创建</button></div>
          </form>
          <div class="vault-list">
            <div v-if="!vaults.length" class="empty-state">还没有远端 Vault</div>
            <article v-for="vault in vaults" :key="vault.id" class="vault-item">
              <div class="vault-symbol">N</div>
              <div class="item-copy">
                <strong>{{ vault.name }}</strong><small>{{ shortId(vault.id) }} · revision {{ vault.sequence }}</small>
                <progress :value="usagePercent(vault)" max="100" />
              </div>
              <div class="item-side"><strong>{{ formatBytes(vault.used) }}</strong><small>of {{ formatBytes(vault.quota) }}</small></div>
            </article>
          </div>
        </section>

        <section class="surface">
          <div class="surface-heading"><div><p>Devices</p><h2>设备会话</h2></div><span class="secure-badge">逐次鉴权</span></div>
          <p class="surface-intro">撤销会让该设备的下一次请求立即失败。Vault 内容和本地文件不会因此删除。</p>
          <div class="device-list">
            <article v-for="device in devices" :key="device.id" class="device-item" :class="{ revoked: device.revoked }">
              <div class="device-symbol">{{ firstCharacter(device.name, 'D') }}</div>
              <div class="item-copy"><strong>{{ device.name }}</strong><small>{{ device.revoked ? '已撤销' : '可访问' }}<template v-if="device.id === api.deviceId"> · 当前设备</template></small></div>
              <button v-if="!device.revoked" class="danger-button" type="button" :disabled="busy" @click="revokeDevice(device)">撤销</button>
            </article>
          </div>
        </section>
      </div>
    </section>
  </main>

  <footer><span>OpenNexus Sync v1</span><span>Transport encryption · Device revocation · Immutable history</span></footer>
  <div v-if="toast" class="toast" :class="{ error: toastError }" role="status" aria-live="polite">{{ toast }}</div>
</template>
