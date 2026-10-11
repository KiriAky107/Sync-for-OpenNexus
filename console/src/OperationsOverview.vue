<script setup lang="ts">
import { t, formatDate, formatNumber } from './locale.ts'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type { SyncApi } from './api'
import type { Observability, BackupReminder, RedactedExport } from './observability'
const props = defineProps<{ api: SyncApi; disabled: boolean }>()
const emit = defineEmits<{ expired: []; forbidden: [] }>()
const page = shallowRef<Observability | null>(null), exported = shallowRef<RedactedExport | null>(null)
const busy = ref(false), failure = ref(''), stale = ref(false), exportUrl = ref('')
const cursor = ref<string | null>(null), previous = ref<(string | null)[]>([])
let alive = true, generation = 0
const time = (value: number) => formatDate(value)
const number = (value: number) => formatNumber(value)
const reminder = (value: BackupReminder | null) => !value ? t('尚无成功回执，时间未知') : value.state === 'clock_ahead' ? t('回执时间晚于服务时钟，时间未知')
  : `${t(value.state === 'stale_receipt' ? '已超过 24 小时' : '24 小时内的成功回执')} · ${time(Date.parse(value.finished_utc))}`
function clearExport() { if (exportUrl.value) URL.revokeObjectURL(exportUrl.value); exportUrl.value = ''; exported.value = null }
function clear() { page.value = null; cursor.value = null; previous.value = []; failure.value = ''; stale.value = false; clearExport() }
function report(error: unknown) {
  const code = error instanceof Error ? error.message : ''
  if (code === 'SESSION_CHANGED') { clear(); return }
  if (!props.api.signedIn) { clear(); emit('expired'); return }
  if (code === 'OPERATIONS_FORBIDDEN') { clear(); emit('forbidden'); return }
  failure.value = ({ INVALID_RESPONSE: '运维观测不完整，请重新读取。', REQUEST_TIMEOUT: '读取超时，可稍后重新读取。',
    OPERATIONS_STATUS_UNAVAILABLE: '原运维回执暂时不可读取，请检查记录存储。', DATABASE_UNAVAILABLE: '数据库暂时不可用。',
    HTTP_404: '当前服务尚未提供运维观测，请核对服务版本。' } as Record<string, string>)[code] ?? '读取未完成，请检查连接后重试。'
}
async function load(before = cursor.value, stack = previous.value) {
  if (!alive || busy.value || props.disabled) return
  const ticket = ++generation; busy.value = true; failure.value = ''; stale.value = !!page.value; clearExport()
  try {
    const next = await props.api.observability(before)
    if (!alive || ticket !== generation) return
    page.value = next; cursor.value = before; previous.value = stack; stale.value = false
  } catch (error) { if (alive && ticket === generation) report(error) }
  finally { if (alive && ticket === generation) busy.value = false }
}
function paginate(next: boolean) {
  if (next && page.value?.history.next_before) void load(page.value.history.next_before, [...previous.value, cursor.value])
  else if (!next && previous.value.length) void load(previous.value.at(-1) ?? null, previous.value.slice(0, -1))
}
async function prepareExport() {
  if (!alive || busy.value || props.disabled) return
  const ticket = ++generation; busy.value = true; failure.value = ''; clearExport()
  try {
    const result = await props.api.diagnosticExport()
    if (!alive || ticket !== generation) return
    exported.value = result
    exportUrl.value = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2) + '\n'], { type: 'application/json' }))
  } catch (error) { if (alive && ticket === generation) report(error) }
  finally { if (alive && ticket === generation) busy.value = false }
}
watch(() => props.disabled, disabled => { if (!disabled && !page.value && !busy.value && !failure.value) void load() })
onMounted(() => { void load() })
onBeforeUnmount(() => { alive = false; generation++; clear() })
</script>

<template>
  <section class="surface operations-overview" :aria-label="t('运维观测与提醒')">
    <div class="surface-heading"><div><p>{{ t('Operations overview') }}</p><h2>{{ t('运维观测与提醒') }}</h2></div><button class="secondary-button" :disabled="busy || disabled" @click="load(null, [])">{{ t('刷新运维观测') }}</button></div>
    <p class="surface-intro">{{ t('按登记对象和有效上传预留统计配额，达到 80% 提示接近配额。这里读取已有记录；备份、校验、恢复与对象回收仍使用原 CLI。') }}</p>
    <p v-if="failure" class="operations-error" role="alert">{{ t(failure) }}</p><p v-if="busy" role="status">{{ t('正在读取运维观测…') }}</p>
    <p v-if="page" class="operations-updated">{{ stale ? t('上次读取，尚未刷新') : t('读取时间') }} · {{ time(page.observed_at * 1000) }} {{ t('· 数据库快照与原 CLI 回执分别读取') }}</p>
    <template v-if="page">
      <dl class="observability-statistics"><dt>{{ t('接近配额') }}</dt><dd>{{ number(page.capacity.near_quota) }} / {{ number(page.capacity.vaults) }} {{ t('个知识库') }}</dd><dt>{{ t('达到或超过配额') }}</dt><dd>{{ number(page.capacity.blocked) }} {{ t('个知识库') }}</dd><dt>{{ t('账目不一致') }}</dt><dd>{{ number(page.accounting_mismatches) }}</dd><dt>{{ t('回收待核对对象') }}</dt><dd>{{ number(page.pending_reclamation_objects) }}</dd><dt>{{ t('修订历史') }}</dt><dd>{{ t('永久保留') }}</dd></dl>
      <h3>{{ t('备份与校验提醒') }}</h3><p v-if="!page.backup_receipts.configured" class="empty-state">{{ t('运维记录路径未配置，备份和校验时间未知。') }}</p>
      <dl v-else class="observability-statistics"><dt>{{ t('最近成功备份回执') }}</dt><dd>{{ reminder(page.backup_receipts.backup) }}</dd><dt>{{ t('最近成功校验回执') }}</dt><dd>{{ reminder(page.backup_receipts.verify) }}</dd><dt>{{ t('尚无完成回执') }}</dt><dd>{{ number(page.backup_receipts.unfinished_operations) }} {{ t('项') }}</dd><dt>{{ t('失败回执') }}</dt><dd>{{ number(page.backup_receipts.failed_operations) }} {{ t('项') }}</dd></dl>
      <p class="surface-intro">{{ t('备份和校验是独立回执，尚未关联同一归档。恢复前仍需对选定归档执行完整校验。24 小时提示不重新检查归档，也不代表实际磁盘或对象桶的剩余容量。') }}</p>
      <h3>{{ t('暂存清理历史') }}</h3>
      <p v-if="page.latest_cleanup">{{ t('最近检查 ·') }} {{ time(page.latest_cleanup.finished_at * 1000) }} {{ t('· 累计移除') }} {{ number(page.latest_cleanup.total_removed) }} {{ t('项 · 累计失败') }} {{ number(page.latest_cleanup.total_failures) }} {{ t('项') }}</p>
      <p class="surface-intro">{{ t('保留最近 1000 条处理过候选或出现错误的清理记录，每页 20 条。空闲检查只更新最近检查摘要；修订历史不受此限制。') }}</p>
      <p v-if="!page.history.items.length" class="empty-state">{{ t('本页没有清理记录。') }}</p>
      <div class="operations-list"><article v-for="item in page.history.items" :key="item.id" class="operation-item"><strong>{{ time(item.finished_at * 1000) }}</strong><p>{{ t('候选') }} {{ number(item.selected) }} {{ t('· 移除') }} {{ number(item.removed) }} {{ t('· 释放') }} {{ number(item.released_bytes) }} {{ t('B · 耗时') }} {{ number(item.duration_ms) }} ms</p><p>{{ t('文件系统失败') }} {{ number(item.filesystem_failures) }} {{ t('· 元数据失败') }} {{ number(item.metadata_failures) }}</p></article></div>
      <nav class="admin-pagination" :aria-label="t('清理历史分页')"><button class="secondary-button" :disabled="busy || disabled || !previous.length" @click="paginate(false)">{{ t('上一页') }}</button><span>{{ previous.length + 1 }}</span><button class="secondary-button" :disabled="busy || disabled || !page.history.next_before" @click="paginate(true)">{{ t('下一页') }}</button></nav>
    </template>
    <h3>{{ t('脱敏诊断导出') }}</h3><p class="surface-intro">{{ t('只包含读取时间、统计、固定策略和状态；最多 20 条清理记录。不包含账户、知识库、设备或操作编号、文件名、本地路径、对象哈希、凭据与文件内容。先查看范围，再显式保存 JSON。') }}</p>
    <button class="secondary-button" :disabled="busy || disabled" @click="prepareExport()">{{ t('准备脱敏诊断') }}</button>
    <div v-if="exported" class="diagnostic-export"><p>{{ t('导出读取时间 ·') }} {{ time(exported.observed_at * 1000) }} · {{ exported.cleanup_page.length }} {{ t('条清理记录') }}</p><details><summary>{{ t('查看将保存的 JSON') }}</summary><pre>{{ JSON.stringify(exported, null, 2) }}</pre></details><a :href="exportUrl" download="opennexus-sync-diagnostics.json">{{ t('保存脱敏 JSON') }}</a></div>
  </section>
</template>
