<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { type SyncApi, type PendingUpload, type UploadPage, type UploadResult, type Usage, type Vault } from './api'

const props = defineProps<{ api: SyncApi; vaults: Vault[] }>()
const emit = defineEmits<{ refreshed: []; expired: []; pending: [value: boolean] }>()
const vaultId = ref(''), busy = ref(false), mutationBusy = ref(false), failure = ref(''), notice = ref('')
const usage = shallowRef<Usage | null>(null), uploads = shallowRef<UploadPage | null>(null)
const selected = shallowRef<PendingUpload | null>(null), acknowledged = ref(false)
const intent = shallowRef<Readonly<{ vault: string; upload: string }> | null>(null)
const receipt = shallowRef<UploadResult | null>(null)
const cursor = ref<string | null>(null), previous = ref<(string | null)[]>([]), stale = ref(false)
let ticket = 0, alive = true
const locked = computed(() => busy.value || mutationBusy.value || !!intent.value)
const pending = computed(() => !!intent.value || mutationBusy.value)
watch(pending, value => emit('pending', value))
function bytes(value: number) {
  const units = ['B', 'KiB', 'MiB', 'GiB']
  let unit = 0
  while (value >= 1024 && unit < 3) { value /= 1024; unit++ }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`
}
const time = (value: number) => new Date(value*1000).toLocaleString('zh-CN', { hour12: false })
function errorText(error: unknown): string {
  if (!props.api.signedIn) { emit('expired'); return '会话已失效，请重新登录。' }
  const code = error instanceof Error ? error.message : ''
  if (code === 'STAGING_UNAVAILABLE') return '暂存目录不可写，上传记录仍可核对与重试。'
  if (code === 'VAULT_NOT_FOUND') return '知识库已不可访问，请核对账户与权限。'
  return '读取未完成，请检查连接后重试。'
}
async function read(reset = false): Promise<void> {
  if (!vaultId.value || mutationBusy.value) return
  const version = ++ticket, vault = vaultId.value
  busy.value = true; failure.value = ''; stale.value = !!usage.value
  uploads.value = null
  if (reset) { cursor.value = null; previous.value = [] }
  try {
    const [ledger, page] = await Promise.all([props.api.usage(vault), props.api.uploads(vault, cursor.value)])
    if (!alive || version !== ticket) return
    usage.value = ledger; uploads.value = page; stale.value = false
  } catch (error) { if (alive && version === ticket) failure.value = errorText(error) }
  finally { if (alive && version === ticket) busy.value = false }
}
async function nextPage(): Promise<void> {
  if (locked.value || !uploads.value?.next_before) return
  previous.value.push(cursor.value); cursor.value = uploads.value.next_before
  selected.value = null; acknowledged.value = false; await read()
}
async function previousPage(): Promise<void> {
  if (locked.value || !previous.value.length) return
  cursor.value = previous.value.pop() ?? null; selected.value = null; acknowledged.value = false; await read()
}
function choose(upload: PendingUpload): void {
  if (locked.value) return
  selected.value = upload; acknowledged.value = false; notice.value = ''; failure.value = ''; receipt.value = null
}
function describe(result: UploadResult): string {
  const descriptions: Record<UploadResult['state'], string> = {
    completed: '上传已经完成，对象已保留。取消操作没有删除它。', cancelled: '上传已取消，预留已释放。',
    expired: '上传已过期，不再预留配额。若仍在列表中，可取消以清理暂存。',
    damaged: '暂存已损坏，原上传已结束。同步设备需要重新上传。',
    reclaimed: '已完成上传的未引用对象已由管理员回收。需要重新上传后再提交修订。',
    reclamation_pending: '管理员回收尚未核对完成。请等待管理员恢复原计划，再读取结果。',
    active: '原上传仍未完成。你可以重试已确认的取消操作。',
    not_found: '服务没有该上传或回执的记录，无法确认原结果。请刷新列表核对。',
  }
  return descriptions[result.state]
}
async function finish(result: UploadResult): Promise<void> {
  receipt.value = result; notice.value = describe(result)
  if (['completed', 'cancelled', 'damaged', 'reclaimed'].includes(result.state) || (result.state === 'expired' && result.expires == null)) {
    intent.value = null; selected.value = null; acknowledged.value = false
    mutationBusy.value = false
    await read(true)
    emit('refreshed')
  }
}
async function cancel(retry = false): Promise<void> {
  if (mutationBusy.value || busy.value) return
  if (retry && receipt.value?.state !== 'active' && !(receipt.value?.state === 'expired' && receipt.value.expires != null)) return
  if (!intent.value) {
    if (!selected.value || !acknowledged.value || stale.value) return
    intent.value = Object.freeze({ vault: vaultId.value, upload: selected.value.id })
  }
  const operation = intent.value
  mutationBusy.value = true; failure.value = ''; receipt.value = null
  try { const result = await props.api.cancelUpload(operation.vault, operation.upload); if (alive && intent.value === operation) await finish(result) }
  catch (error) {
    if (alive && intent.value === operation) failure.value = errorText(error) + ' 请先查询原操作结果，再决定是否重试。'
  } finally { if (alive) mutationBusy.value = false }
}
async function reconcile(): Promise<void> {
  if (!intent.value || mutationBusy.value) return
  const operation = intent.value
  mutationBusy.value = true; failure.value = ''
  try { const result = await props.api.uploadResult(operation.vault, operation.upload); if (alive && intent.value === operation) await finish(result) }
  catch (error) { if (alive && intent.value === operation) failure.value = errorText(error) }
  finally { if (alive) mutationBusy.value = false }
}
function closeReconciliation(): void {
  if (mutationBusy.value) return
  intent.value = null; selected.value = null; acknowledged.value = false
  notice.value = '已结束本地核对；没有再次发送取消请求。'
  void read(true)
}
watch(() => props.vaults.map(vault => vault.id).join(','), () => {
  if (!vaultId.value || !props.vaults.some(vault => vault.id === vaultId.value)) vaultId.value = props.vaults[0]?.id ?? ''
}, { immediate: true })
watch(vaultId, () => { ticket++; usage.value = null; uploads.value = null; selected.value = null; acknowledged.value = false; receipt.value = null; notice.value = ''; void read(true) }, { immediate: true })
onBeforeUnmount(() => { alive = false; ticket++; emit('pending', false) })
</script>

<template>
  <section class="surface storage-surface" aria-labelledby="storage-title">
    <div class="surface-heading"><div><p>Storage &amp; uploads</p><h2 id="storage-title">用量与上传</h2></div><button class="secondary-button" :disabled="locked || !vaultId" @click="read(true)">刷新用量</button></div>
    <label class="storage-vault">知识库<select v-model="vaultId" aria-label="用量知识库" :disabled="locked"><option v-for="vault in vaults" :key="vault.id" :value="vault.id">{{ vault.name }}</option></select></label>
    <p v-if="!vaults.length" class="empty-state">创建知识库后可查看用量与上传。</p>
    <p v-if="failure" class="operations-error" role="alert">{{ failure }}</p>
    <p v-if="notice" class="storage-notice" role="status">{{ notice }}</p>
    <p v-if="busy" role="status" class="operations-updated">正在确认账目与上传状态…</p>
    <template v-if="usage">
      <p class="operations-updated">{{ stale ? '上次读取，尚未刷新' : '服务器确认' }} · {{ time(usage.confirmed_at) }} · 修订 {{ usage.sequence }}</p>
      <p v-if="!usage.accounting_matches" class="operations-error" role="alert">配额账目与已登记对象的总量不一致，请联系管理员核对。</p>
      <p v-if="usage.reclamation_pending_objects" class="operations-error" role="alert">{{ usage.reclamation_pending_objects }} 个对象（{{ bytes(usage.reclamation_pending_bytes) }}）的回收结果待管理员核对，仍保留原账目。</p>
      <div class="storage-ledger"><article><small>当前唯一对象</small><strong>{{ bytes(usage.current_object_bytes) }}</strong></article><article><small>仅历史引用</small><strong>{{ bytes(usage.historical_only_bytes) }}</strong></article><article><small>尚未引用对象</small><strong>{{ bytes(usage.unreferenced_object_bytes) }}</strong></article><article><small>有效上传预留</small><strong>{{ bytes(usage.reserved_bytes) }}</strong></article></div>
      <p class="surface-intro">已计费 {{ bytes(usage.charged_bytes) }} / {{ bytes(usage.quota) }} · 可用 {{ bytes(usage.available_bytes) }}。{{ usage.active_files }} 个当前文件的逻辑大小为 {{ bytes(usage.logical_file_bytes) }}，相同对象只计费一次。历史无限保留。</p>
      <progress class="storage-progress" :value="usage.charged_bytes + usage.reserved_bytes" :max="Math.max(1, usage.quota)" aria-label="对象及上传预留用量" />
      <p class="surface-intro">未完成上传 {{ usage.pending_uploads }} 个 · 其中过期 {{ usage.expired_uploads }} 个。过期后不再预留配额，暂存删除失败时保留记录供重试。</p>
    </template>
    <div v-if="uploads" class="storage-uploads">
      <p v-if="!uploads.items.length" class="empty-state">本页没有未完成上传。</p>
      <article v-for="upload in uploads.items" :key="upload.id" class="storage-upload">
        <div class="item-copy"><strong>{{ upload.device_name }}<span v-if="upload.device_id === api.deviceId"> · 当前设备</span></strong><small>{{ upload.id }}</small><span>确认 {{ bytes(upload.offset_bytes) }} / {{ bytes(upload.size) }} · 到期 {{ time(upload.expires) }}</span><small>{{ upload.state === 'expired' ? '已过期' : upload.state === 'device_revoked' ? '设备已撤销，不能续传' : '可由原设备续传' }}</small></div>
        <button class="secondary-button" :disabled="locked || stale" @click="choose(upload)">查看 / 取消</button>
      </article>
      <nav class="storage-pagination" aria-label="上传分页"><button class="secondary-button" :disabled="locked || !previous.length" @click="previousPage">上一页</button><span>第 {{ previous.length + 1 }} 页</span><button class="secondary-button" :disabled="locked || !uploads.next_before" @click="nextPage">下一页</button></nav>
    </div>
    <div v-if="selected || intent" class="storage-review">
      <h3>取消上传核对</h3><code>{{ intent?.upload || selected?.id }}</code><p class="surface-intro">停止这个上传并释放它的预留。已经完成的对象会保留；此操作不删除笔记或历史修订。</p>
      <template v-if="!intent"><label><input v-model="acknowledged" type="checkbox" />我已核对设备、确认偏移和上传编号</label><button class="danger-button" :disabled="!acknowledged || locked || stale" @click="cancel()">确认取消此上传</button></template>
      <template v-else><p class="surface-intro">写入结果尚待核对，原上传编号保持不变。</p><button class="secondary-button" :disabled="mutationBusy" @click="reconcile">查询原操作结果</button><button v-if="receipt?.state === 'active' || (receipt?.state === 'expired' && receipt.expires != null)" class="danger-button" :disabled="mutationBusy" @click="cancel(true)">重试原取消操作</button><button class="secondary-button" :disabled="mutationBusy" @click="closeReconciliation">结束本地核对</button></template>
      <p v-if="receipt" class="operations-updated">结果 {{ receipt.state }} · 确认 {{ time(receipt.confirmed_at) }}</p>
    </div>
  </section>
</template>
