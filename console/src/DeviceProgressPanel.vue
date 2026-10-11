<script setup lang="ts">
import { t, formatDate } from './locale.ts'
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import type { SyncApi, Vault, DeviceProgressPage } from './api'

const props = defineProps<{ api: SyncApi; vaults: Vault[]; account?: string; disabled: boolean }>()
const emit = defineEmits<{ expired: []; forbidden: [] }>()
const selected = ref(''), page = shallowRef<DeviceProgressPage | null>(null)
const busy = ref(false), failure = ref(''), stale = ref(false)
const cursor = ref<string | null>(null), previous = ref<(string | null)[]>([])
const choices = computed(() => props.vaults)
let alive = true, generation = 0
const time = (value: number | null) => value === null ? t('尚无观测') : formatDate(value * 1000)
function clear() { page.value = null; cursor.value = null; previous.value = []; failure.value = ''; stale.value = false }
function report(error: unknown) {
  const code = error instanceof Error ? error.message : ''
  if (code === 'SESSION_CHANGED') { clear(); return }
  if (!props.api.signedIn) { clear(); emit('expired'); return }
  if (code === 'OPERATIONS_FORBIDDEN') { clear(); emit('forbidden'); return }
  failure.value = ({
    VAULT_NOT_FOUND: '知识库已不可访问，请刷新账户列表。',
    INVALID_REQUEST: '知识库或分页编号无效，请重新选择。',
    INVALID_RESPONSE: '设备观测不完整或与当前知识库不符，请重新读取。',
    REQUEST_TIMEOUT: '读取超时，可稍后重新读取。', DATABASE_UNAVAILABLE: '数据库暂时不可用。',
    HTTP_404: '当前服务尚未提供设备进度查询，请核对服务版本。',
  } as Record<string, string>)[code] ?? '读取未完成，请检查连接后重新读取。'
}
async function load() {
  if (!alive || busy.value || props.disabled || !selected.value) return
  const ticket = ++generation, vault = selected.value, account = props.account
  busy.value = true; failure.value = ''; stale.value = !!page.value
  try {
    const next = await props.api.deviceProgress(vault, cursor.value, account)
    if (!alive || ticket !== generation || vault !== selected.value || account !== props.account) return
    page.value = next; stale.value = false
  } catch (error) { if (alive && ticket === generation) report(error) }
  finally { if (alive && ticket === generation) busy.value = false }
}
function choose(value: string) {
  generation++; busy.value = false; clear(); selected.value = value
  void load()
}
async function paginate(next: boolean) {
  if (busy.value || props.disabled) return
  if (next) { if (!page.value?.next_before) return; previous.value.push(cursor.value); cursor.value = page.value.next_before }
  else { if (!previous.value.length) return; cursor.value = previous.value.pop() ?? null }
  page.value = null; await load()
}
watch(() => [props.account, props.vaults.map(vault => vault.id).join(',')], (_, old) => {
  const accountChanged = old && old[0] !== props.account
  if (accountChanged || !props.vaults.some(vault => vault.id === selected.value)) choose(props.vaults[0]?.id ?? '')
}, { immediate: true })
watch(() => props.disabled, disabled => { if (!disabled && !page.value && !busy.value && !failure.value) void load() })
onBeforeUnmount(() => { alive = false; generation++; clear() })
</script>

<template>
  <section class="surface device-progress-surface" :aria-label="t('设备活动与服务端进度')">
    <div class="surface-heading"><div><p>{{ t('Device observations') }}</p><h2>{{ t('设备活动与服务端进度') }}</h2></div><button class="secondary-button" :disabled="busy || disabled || !selected" @click="load()">{{ busy ? t('正在读取…') : t('刷新设备观测') }}</button></div>
    <p class="surface-intro">{{ t('最近认证活动每设备最多 60 秒采样一次。已确认提交来自成功写入的修订事务；生成变化页只表示服务端生成过该位置的响应。客户端收取、应用结果尚无确认。') }}</p>
    <p v-if="account" class="operations-updated">{{ t('当前管理账户 ·') }} {{ account }}</p>
    <label class="device-progress-choice">{{ t('知识库') }}<select :value="selected" :disabled="disabled || !choices.length" @change="choose(($event.target as HTMLSelectElement).value)"><option v-for="vault in choices" :key="vault.id" :value="vault.id">{{ vault.name }} · {{ vault.id }}</option></select></label>
    <p v-if="failure" class="operations-error" role="alert">{{ t(failure) }}</p><p v-if="busy" role="status">{{ t('正在读取当前知识库的设备观测…') }}</p>
    <p v-if="page" class="operations-updated">{{ stale ? t('上次读取，尚未刷新') : t('读取时间') }} · {{ time(page.observed_at) }} {{ t('· 知识库当前序列') }} {{ page.vault_sequence }}</p>
    <div class="device-progress-list"><article v-for="item in page?.items" :key="item.device_id" class="device-progress-item">
      <div class="device-progress-heading"><strong>{{ item.name }}</strong><span class="status-pill" :class="item.revoked ? 'muted' : 'positive'">{{ item.revoked ? t('已撤销访问') : t('可访问') }}</span></div><code>{{ item.device_id }}</code>
      <dl><dt>{{ t('最近认证活动') }}</dt><dd>{{ time(item.authenticated_at) }}</dd><dt>{{ t('已确认提交') }}</dt><dd>{{ item.committed_sequence === null ? t('尚无观测') : t('序列 ') + item.committed_sequence + ' · ' + time(item.committed_at) }}</dd><dt>{{ t('服务端生成变化页') }}</dt><dd>{{ item.offered_sequence === null ? t('尚无观测') : t('位置 ') + item.offered_sequence + ' · ' + time(item.offered_at) }}</dd><dt>{{ t('客户端应用确认') }}</dt><dd>{{ t('未知') }}</dd></dl>
    </article></div>
    <p v-if="!choices.length || page && !page.items.length" class="empty-state">{{ !choices.length ? t('选择有权限访问的知识库后读取设备观测。') : t('此账户尚无设备。') }}</p>
    <nav v-if="page || previous.length" class="admin-pagination" :aria-label="t('设备观测分页')"><button class="secondary-button" :disabled="busy || disabled || !previous.length" @click="paginate(false)">{{ t('上一页') }}</button><span>{{ previous.length + 1 }}</span><button class="secondary-button" :disabled="busy || disabled || !page?.next_before" @click="paginate(true)">{{ t('下一页') }}</button></nav>
  </section>
</template>
