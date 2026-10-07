<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { SyncApi, SyncApiError, type FilePage, type HistoryPage, type Preview, type RestoreRequest, type RestoreTarget, type Revision, type Vault } from './api'
import { compareText } from './textDiff'

const props = defineProps<{ api: SyncApi; vaults: Vault[] }>()
const emit = defineEmits<{ refreshed: []; expired: []; pending: [value: boolean] }>()
const vaultId = ref(''), query = ref(''), includeDeleted = ref(false)
const files = shallowRef<FilePage | null>(null), history = shallowRef<HistoryPage | null>(null)
const fileId = ref(''), source = shallowRef<Revision | null>(null), head = shallowRef<Revision | null>(null)
const historicalPreview = shallowRef<Preview | null>(null), currentPreview = shallowRef<Preview | null>(null)
const targetPath = ref(''), target = shallowRef<RestoreTarget | null>(null), acknowledged = ref(false)
const listBusy = ref(false), detailBusy = ref(false), targetBusy = ref(false), restoring = ref(false)
const failure = ref(''), notice = ref(''), result = shallowRef<Revision | null>(null)
const intent = shallowRef<Readonly<RestoreRequest> | null>(null), retryAllowed = ref(false)
let listTicket = 0, detailTicket = 0, targetTicket = 0, alive = true
let fileFilter = { q: '', includeDeleted: false }
const locked = computed(() => Boolean(intent.value) || restoring.value)
const comparison = computed(() => currentPreview.value?.kind === 'text' && historicalPreview.value?.kind === 'text'
  ? compareText(currentPreview.value.text, historicalPreview.value.text) : null)
const targetValid = computed(() => target.value && !target.value.occupied && target.value.path === targetPath.value && target.value.current_revision === head.value?.sequence)
const canRestore = computed(() => source.value?.operation === 'put' && source.value.sequence !== head.value?.sequence && targetValid.value && acknowledged.value && !detailBusy.value && !targetBusy.value && !locked.value)

function bytes(value: number) { return value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1024 ** 2).toFixed(1)} MiB` }
function time(value?: number | null) { return value == null ? '时间未知（旧修订）' : new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) }
function errorText(error: unknown) {
  const code = error instanceof Error ? error.message : ''
  const messages: Record<string, string> = {
    REVISION_CONFLICT: '文件在审核后发生变化，请刷新并重新查看差异。', PATH_CONFLICT: '目标路径已被其他文件占用，请选择新路径后重新核对。',
    INVALID_REQUEST: '路径或请求无效，请使用知识库内的可移植相对路径。', STORAGE_INTEGRITY: '历史对象完整性检查未通过，暂停恢复并联系管理员。',
    STORAGE_UNAVAILABLE: '对象存储暂时不可用，请稍后重新读取。', PREVIEW_BUSY: '预览正在处理其他请求，请稍后重新读取。',
    RESTORE_SOURCE_DELETED: '删除记录没有可恢复的内容，请选择此前的内容修订。', IDEMPOTENCY_REUSED: '操作身份已用于另一项操作，请刷新后重新审核。',
    FILE_NOT_FOUND: '文件已不可访问，请刷新文件列表。', REVISION_NOT_FOUND: '历史修订已不可访问，请重新读取。',
  }
  if (error instanceof SyncApiError && error.status === 401) emit('expired')
  return messages[code] ?? '请求未完成，已显示的内容尚未刷新。请检查连接后重试。'
}
function unsupported(preview: Preview) {
  if (preview.kind === 'deleted') return '此版本是删除记录，没有文件内容。'
  if (preview.kind !== 'unsupported') return ''
  const reasons: Record<string, string> = { PREVIEW_TOO_LARGE: '文件超过安全预览大小', UNSUPPORTED_TYPE: '此格式不提供在线预览', NOT_UTF8: '文本不是 UTF-8 编码', BINARY_CONTENT: '内容包含二进制数据', INVALID_IMAGE: '图片无法安全解码', IMAGE_TOO_LARGE: '图片像素数量超过限制' }
  return (reasons[preview.reason] ?? '此内容无法在线预览') + '。恢复仍使用原始对象字节，请核对摘要和大小。'
}
function clearDetail() {
  detailTicket++; targetTicket++
  fileId.value = ''; history.value = null; source.value = null; head.value = null
  historicalPreview.value = null; currentPreview.value = null; target.value = null
  detailBusy.value = false; targetBusy.value = false; acknowledged.value = false; result.value = null
}
async function loadFiles(more = false) {
  if (!vaultId.value || locked.value) return
  const ticket = ++listTicket, vault = vaultId.value
  if (!more) { files.value = null; clearDetail(); fileFilter = { q: query.value, includeDeleted: includeDeleted.value } }
  listBusy.value = true; failure.value = ''; notice.value = ''
  try {
    const page = await props.api.files(vault, { ...fileFilter, ...(more && files.value ? { before: files.value.next_before, boundary: files.value.boundary } : {}) })
    if (!alive || ticket !== listTicket) return
    files.value = more && files.value ? { ...page, items: [...files.value.items, ...page.items] } : page
  } catch (error) { if (alive && ticket === listTicket) failure.value = errorText(error) }
  finally { if (alive && ticket === listTicket) listBusy.value = false }
}
async function loadHistory(file: string, more = false) {
  if (locked.value) return
  const ticket = ++detailTicket, vault = vaultId.value
  detailBusy.value = true; failure.value = ''
  if (!more) { source.value = null; historicalPreview.value = null; currentPreview.value = null; target.value = null; acknowledged.value = false; result.value = null; history.value = null; fileId.value = file }
  try {
    const page = await props.api.history(vault, file, more && history.value ? { before: history.value.next_before, boundary: history.value.boundary } : {})
    if (!alive || ticket !== detailTicket) return
    if (more && head.value?.sequence !== page.current?.sequence) {
      currentPreview.value = null; targetTicket++; target.value = null; acknowledged.value = false
      notice.value = '当前版本已变化，请重新选择历史修订以读取新的差异。'
    }
    history.value = more && history.value ? { ...page, items: [...history.value.items, ...page.items] } : page
    head.value = page.current
  } catch (error) { if (alive && ticket === detailTicket) failure.value = errorText(error) }
  finally { if (alive && ticket === detailTicket) detailBusy.value = false }
}
async function chooseRevision(revision: Revision) {
  if (locked.value) return
  const ticket = ++detailTicket, vault = vaultId.value, file = fileId.value
  targetTicket++; targetBusy.value = false; target.value = null; acknowledged.value = false
  source.value = revision; historicalPreview.value = null; currentPreview.value = null; result.value = null
  detailBusy.value = true; failure.value = ''; notice.value = ''
  try {
    const current = await props.api.file(vault, file)
    const [oldPreview, livePreview] = await Promise.all([props.api.preview(vault, file, revision.sequence), props.api.preview(vault, file, current.sequence)])
    if (!alive || ticket !== detailTicket) return
    head.value = current; historicalPreview.value = oldPreview; currentPreview.value = livePreview; targetPath.value = current.path
  } catch (error) { if (alive && ticket === detailTicket) failure.value = errorText(error) }
  finally { if (alive && ticket === detailTicket) detailBusy.value = false }
}
async function checkTarget() {
  if (!head.value || !source.value || locked.value) return
  const ticket = ++targetTicket, vault = vaultId.value, file = fileId.value, path = targetPath.value, sequence = head.value.sequence
  acknowledged.value = false; target.value = null; targetBusy.value = true; failure.value = ''
  try {
    const checked = await props.api.restoreTarget(vault, file, path)
    if (!alive || ticket !== targetTicket) return
    if (checked.current_revision !== sequence) { notice.value = '当前版本已变化，请重新选择历史修订以读取新的差异。'; return }
    target.value = checked
  } catch (error) { if (alive && ticket === targetTicket) failure.value = errorText(error) }
  finally { if (alive && ticket === targetTicket) targetBusy.value = false }
}
async function acceptResult(receipt: Revision) {
  result.value = receipt; intent.value = null; retryAllowed.value = false; acknowledged.value = false; target.value = null
  notice.value = `恢复已完成，创建修订 ${receipt.sequence}。`
  try {
    const page = await props.api.history(vaultId.value, fileId.value)
    if (!alive) return
    history.value = page; head.value = page.current; currentPreview.value = null
    notice.value += (page.current?.sequence ?? 0) > receipt.sequence ? ' 此后已有其他编辑，当前版本已保留。' : ' 其他设备将在同步时收到此修订。'
  } catch (error) {
    if (alive) { failure.value = errorText(error); notice.value += ' 历史列表尚未刷新，完成回执已确认。' }
  }
  emit('refreshed')
}
async function sendRestore() {
  if (!intent.value || restoring.value) return
  const request = intent.value
  restoring.value = true; failure.value = ''; notice.value = ''; retryAllowed.value = false
  try {
    const receipt = await props.api.restore(vaultId.value, fileId.value, request)
    if (alive) await acceptResult(receipt)
  } catch (error) {
    if (!alive) return
    if (error instanceof SyncApiError && error.status < 500) { intent.value = null; target.value = null; acknowledged.value = false; failure.value = errorText(error) }
    else failure.value = '尚未确认恢复结果。保留了原操作身份，请先核对结果，再决定是否重试。'
  } finally { if (alive) restoring.value = false }
}
function restore() {
  if (!canRestore.value || !source.value || !head.value) return
  intent.value = Object.freeze({ operation_id: crypto.randomUUID(), source_revision: source.value.sequence, base_revision: head.value.sequence, path: targetPath.value })
  void sendRestore()
}
async function reconcile() {
  if (!intent.value || restoring.value) return
  restoring.value = true; failure.value = ''; retryAllowed.value = false
  try {
    const receipt = await props.api.restoreResult(vaultId.value, fileId.value, intent.value.operation_id)
    if (alive) await acceptResult(receipt)
  } catch (error) {
    if (!alive) return
    if (error instanceof SyncApiError && error.message === 'RESTORE_NOT_FOUND') { retryAllowed.value = true; notice.value = '尚未找到完成回执。可以使用原操作身份重试；服务会检查当前版本并防止重复恢复。' }
    else failure.value = errorText(error)
  } finally { if (alive) restoring.value = false }
}
watch(targetPath, () => { targetTicket++; target.value = null; acknowledged.value = false; targetBusy.value = false })
watch(locked, value => emit('pending', value))
watch(() => props.vaults, values => { if (!values.some(v => v.id === vaultId.value) && !locked.value) vaultId.value = values[0]?.id ?? '' }, { immediate: true })
watch(vaultId, () => { void loadFiles() }, { immediate: true })
onBeforeUnmount(() => { alive = false; listTicket++; detailTicket++; targetTicket++; emit('pending', false) })
</script>

<template>
  <section class="surface history-surface" aria-labelledby="history-title">
    <div class="surface-heading"><div><p>File history</p><h2 id="history-title">文件与历史</h2></div><span class="secure-badge">恢复创建新修订</span></div>
    <p class="surface-intro">浏览远端文件与保留修订。移动沿用文件身份，恢复前核对当前内容、目标路径与历史来源。</p>
    <div v-if="!vaults.length" class="empty-state">创建远端知识库后，已同步文件会出现在这里。</div>
    <template v-else>
      <form class="history-filters" @submit.prevent="loadFiles()">
        <label>知识库<select v-model="vaultId" :disabled="locked"><option v-for="vault in vaults" :key="vault.id" :value="vault.id">{{ vault.name }}</option></select></label>
        <label>搜索路径<input v-model="query" maxlength="120" :disabled="locked" placeholder="输入文件名或相对路径"></label>
        <label class="checkbox-label"><input v-model="includeDeleted" type="checkbox" :disabled="locked">包含删除记录</label>
        <button class="secondary-button" type="submit" :disabled="listBusy || locked">{{ listBusy ? '正在读取…' : '刷新文件' }}</button>
      </form>
      <p v-if="failure" class="history-error" role="alert">{{ failure }}</p>
      <p v-if="notice" class="history-notice" role="status">{{ notice }}</p>
      <p v-if="result" class="history-notice">完成回执：修订 {{ result.sequence }} · 操作 {{ result.operation_id }} · {{ result.path }}</p>
      <div v-if="intent" class="pending-box" role="status">
        <strong>{{ restoring ? '正在确认恢复…' : '恢复结果待核对' }}</strong><p>操作 {{ intent.operation_id }} · 历史修订 {{ intent.source_revision }} → {{ intent.path }}</p>
        <p>请保持此页面以保留待核对操作。已收到回执的恢复会直接显示；重试复用原操作身份。</p>
        <button class="secondary-button" :disabled="restoring" @click="reconcile">核对恢复结果</button>
        <button v-if="retryAllowed" class="secondary-button" :disabled="restoring" @click="sendRestore">使用原操作重试</button>
      </div>
      <div class="history-columns">
        <div class="file-column">
          <p class="snapshot-note" v-if="files">文件列表读取到知识库修订 {{ files.boundary }}，新改动可点击刷新。</p>
          <p v-if="files && !files.items.length" class="empty-state">没有匹配的文件{{ includeDeleted ? '或删除记录' : '' }}。</p>
          <button v-for="file in files?.items" :key="file.file_id" class="file-entry" :class="{ selected: fileId === file.file_id }" :disabled="locked" @click="loadHistory(file.file_id)">
            <strong>{{ file.path }}</strong><span>{{ file.operation === 'delete' ? '已删除' : '当前文件' }} · 修订 {{ file.sequence }} · {{ bytes(file.size) }}</span><small>{{ time(file.created_at) }}</small>
          </button>
          <button v-if="files?.has_more" class="secondary-button" :disabled="listBusy || locked" @click="loadFiles(true)">加载更多文件</button>
        </div>
        <div class="revision-column">
          <p v-if="!fileId" class="empty-state">选择文件查看历史。</p>
          <template v-else>
            <div class="timeline-heading"><h3>{{ head?.path }}</h3><button class="secondary-button" :disabled="detailBusy || locked" @click="loadHistory(fileId)">刷新历史</button></div>
            <p class="snapshot-note">身份 {{ fileId }}<br>当前修订 {{ head?.sequence }} · {{ head?.operation === 'delete' ? '已删除' : '有内容' }}</p>
            <div class="timeline">
              <button v-for="revision in history?.items" :key="revision.sequence" :disabled="locked" class="revision-entry" :class="{ selected: source?.sequence === revision.sequence }" @click="chooseRevision(revision)">
                <strong>修订 {{ revision.sequence }}<span>{{ revision.operation === 'delete' ? '删除' : revision.restored_from != null ? '恢复' : '内容 / 路径修改' }}</span><span v-if="revision.sequence === head?.sequence">当前</span></strong>
                <small>{{ time(revision.created_at) }} · {{ revision.device_name || revision.device_id }}</small><small>{{ revision.path }} · {{ bytes(revision.size) }}<template v-if="revision.restored_from != null"> · 恢复自修订 {{ revision.restored_from }}</template></small>
              </button>
            </div>
            <button v-if="history?.has_more" class="secondary-button" :disabled="detailBusy || locked" @click="loadHistory(fileId, true)">加载更早修订</button>
            <p v-if="detailBusy" class="snapshot-note" role="status">正在读取修订与内容…</p>
          </template>
        </div>
      </div>
      <div v-if="source && head && historicalPreview && currentPreview" class="review-box">
        <h3>审核修订 {{ source.sequence }} → 当前修订 {{ head.sequence }}</h3>
        <div class="preview-pair">
          <article v-for="item in [{ title: '当前内容', preview: currentPreview }, { title: '历史内容', preview: historicalPreview }]" :key="item.title">
            <h4>{{ item.title }} · {{ bytes(item.preview.size) }}</h4><p class="preview-path">{{ item.preview.path }}</p>
            <pre v-if="item.preview.kind === 'text'" class="content-preview">{{ item.preview.text }}</pre>
            <template v-else-if="item.preview.kind === 'image'"><img :src="'data:image/png;base64,' + item.preview.data" :alt="item.title + '图片预览'" class="image-preview"><small v-if="item.preview.first_frame_only">动态图仅预览第一帧。</small></template>
            <p v-else class="empty-state">{{ unsupported(item.preview) }}</p>
            <p class="content-hash">SHA-256 {{ item.preview.hash ?? '无内容' }}</p>
          </article>
        </div>
        <template v-if="comparison">
          <p v-if="comparison.kind === 'equal'" class="snapshot-note">预览文本相同。<template v-if="source.hash !== head.hash">原始字节摘要不同，可能包含编码或 BOM 差异。</template></p>
          <p v-else-if="comparison.kind === 'large'" class="snapshot-note">内容较长，请对照两侧完整预览；此处不生成逐行差异。</p>
          <div v-else class="text-diff" aria-label="当前与历史的文本差异"><div v-for="(line, index) in comparison.lines" :key="index" :class="line.kind"><span>{{ line.current ?? '' }}</span><span>{{ line.source ?? '' }}</span><code>{{ line.kind === 'remove' ? '− ' : line.kind === 'add' ? '+ ' : '  ' }}{{ line.text }}</code></div></div>
        </template>
        <p class="snapshot-note">删除记录可从此前内容恢复。默认沿用当前路径；如需其他位置，请先修改目标路径并核对占用。</p>
        <div v-if="source.operation === 'put' && source.sequence !== head.sequence && !result" class="restore-form">
          <label>恢复目标路径<input v-model="targetPath" :disabled="locked || detailBusy" maxlength="768" autocomplete="off"></label>
          <button class="secondary-button" :disabled="locked || targetBusy || detailBusy || !targetPath" @click="checkTarget">{{ targetBusy ? '正在核对…' : '核对目标路径' }}</button>
          <p v-if="target?.occupied" class="history-error">路径与“{{ target.occupied.path }}”冲突，请选择其他位置。</p>
          <p v-else-if="targetValid" class="history-notice">目标路径可用，已核对当前修订 {{ target?.current_revision }}。提交时会再次检查并发修改与路径占用。</p>
          <label class="checkbox-label"><input v-model="acknowledged" type="checkbox" :disabled="!targetValid || locked">已核对当前内容、历史来源和目标路径，确认以原始字节创建新修订。</label>
          <button class="primary-button" :disabled="!canRestore" @click="restore">确认恢复为新修订</button>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.history-surface { margin-top: 22px; }
.history-filters { display: grid; grid-template-columns: minmax(160px, .8fr) minmax(160px, 1.2fr) auto auto; gap: 12px; align-items: end; }
select { width: 100%; padding: 12px; border: 1px solid var(--line); border-radius: 9px; font: inherit; color: var(--text); background: var(--surface); }
.checkbox-label { display: flex; align-items: center; gap: 8px; line-height: 1.7; letter-spacing: 0; }
.checkbox-label input { width: 16px; height: 16px; flex: 0 0 auto; accent-color: var(--accent); }
.history-filters .checkbox-label { padding-bottom: 8px; }
.history-columns { margin-top: 20px; display: grid; grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr); gap: 22px; }
.file-column, .revision-column { min-width: 0; }
.file-entry, .revision-entry { width: 100%; text-align: left; padding: 13px; display: grid; gap: 6px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); cursor: pointer; margin-bottom: 8px; overflow-wrap: anywhere; }
.file-entry strong, .revision-entry strong { font-size: 13px; }
.file-entry span, .file-entry small, .revision-entry small { color: var(--muted); font-size: 11px; }
.file-entry.selected, .revision-entry.selected { background: var(--accent-soft); border-color: var(--accent); }
.revision-entry strong { display: flex; flex-wrap: wrap; gap: 10px; }
.revision-entry strong span { color: var(--accent); font-size: 10px; font-weight: 500; }
.timeline { max-height: 390px; overflow-y: auto; }
.timeline-heading { display: flex; align-items: center; gap: 12px; justify-content: space-between; }
h3 { margin: 12px 0; font-size: 15px; overflow-wrap: anywhere; }
.snapshot-note, .preview-path { color: var(--muted); font-size: 11px; line-height: 1.8; overflow-wrap: anywhere; }
.history-error, .history-notice { padding: 10px 12px; border-radius: 9px; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.history-error { background: var(--danger-soft); color: var(--danger); }
.history-notice { background: var(--accent-soft); color: var(--accent); }
.pending-box { padding: 16px; background: var(--bg-secondary); border: 1px solid var(--line-strong); border-radius: 9px; font-size: 12px; }
.pending-box p { overflow-wrap: anywhere; line-height: 1.7; }
.pending-box button { margin-right: 8px; }
.review-box { margin-top: 24px; padding-top: 12px; border-top: 1px solid var(--line); }
.preview-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.preview-pair article { min-width: 0; padding: 16px; border: 1px solid var(--line); border-radius: 9px; }
h4 { margin: 0; font-size: 12px; }
.content-preview { height: 250px; overflow: auto; white-space: pre; padding: 12px; background: var(--bg-secondary); font-size: 12px; line-height: 1.7; tab-size: 4; }
.image-preview { display: block; max-width: 100%; max-height: 250px; margin: 16px auto; object-fit: contain; }
.content-hash { font: 10px/1.7 ui-monospace, monospace; color: var(--muted); overflow-wrap: anywhere; }
.text-diff { max-height: 280px; overflow: auto; font: 11px/1.8 ui-monospace, monospace; border: 1px solid var(--line); border-radius: 9px; }
.text-diff > div { display: flex; min-width: max-content; }
.text-diff span { width: 36px; flex: 0 0 36px; text-align: right; padding-right: 7px; color: var(--muted); user-select: none; }
.text-diff code { white-space: pre; padding-right: 12px; }
.text-diff .remove { background: var(--danger-soft); }
.text-diff .add { background: var(--success-soft); }
.restore-form { display: grid; gap: 12px; margin-top: 16px; }
.restore-form .secondary-button { justify-self: start; }
.restore-form .primary-button { justify-self: start; width: auto; }
@media (max-width: 820px) { .history-filters { grid-template-columns: 1fr 1fr; } .history-columns { grid-template-columns: 1fr; } .file-column { max-height: 340px; overflow: auto; } }
@media (max-width: 560px) { .history-filters, .preview-pair { grid-template-columns: 1fr; } .surface-heading { gap: 12px; flex-wrap: wrap; } .timeline-heading { flex-wrap: wrap; } }
</style>
