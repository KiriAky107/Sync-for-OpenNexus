<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { SyncApi, PlanState, ReclamationPage, ReclamationDetail } from './api'
const props = defineProps<{ api: SyncApi; disabled: boolean }>()
const emit = defineEmits<{ expired: []; forbidden: [] }>()
const page = shallowRef<ReclamationPage | null>(null), detail = shallowRef<ReclamationDetail | null>(null)
const busy = ref(false), stale = ref(false), failure = ref('')
const vault = ref(''), state = ref<PlanState | ''>(''), lookupId = ref('')
const cursor = ref<string | null>(null), previous = ref<(string | null)[]>([])
let applied: { vault: string; state: PlanState | '' } = { vault: '', state: '' }
let alive = true, generation = 0
const time = (seconds: number | null) => seconds === null ? '尚无回执' : new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
const states = { preview: '原计划预览', running: '正在核对', complete: '已完成' }
const itemStates = { prepared: '待核对', reclaimed: '已回收', protected: '已保护' }
const reasons: Record<string, string> = { revision: '永久修订引用', upload: '上传记录', pending_reclamation: '其他回收待核对', grace_period: '对象宽限期', completion_receipt: '近期完成回执', absent_from_backup: '未包含在原备份' }
function clear() { page.value = null; detail.value = null; cursor.value = null; previous.value = []; lookupId.value = ''; vault.value = ''; state.value = ''; applied = { vault: '', state: '' } }
function report(error: unknown) {
  const code = error instanceof Error ? error.message : ''
  if (code === 'SESSION_CHANGED') { clear(); return }
  if (!props.api.signedIn) { clear(); emit('expired'); return }
  if (code === 'OPERATIONS_FORBIDDEN') { clear(); emit('forbidden'); return }
  failure.value = ({ GC_PLAN_NOT_FOUND: '未找到该原计划，请核对编号。', INVALID_REQUEST: '请填写完整的知识库或原计划编号。', INVALID_RESPONSE: '返回的计划或回执不完整，请重新读取。', REQUEST_TIMEOUT: '读取超时，可稍后刷新原计划。', DATABASE_UNAVAILABLE: '数据库暂时不可用，可稍后重新读取。' } as Record<string, string>)[code] ?? '读取未完成，请检查连接后重新读取。'
}
async function readDetail(id: string, ticket: number) {
  const result = await props.api.reclamationPlan(id)
  if (alive && ticket === generation) { detail.value = result; stale.value = false; lookupId.value = result.plan_id }
}
async function load(reset = false) {
  if (!alive || busy.value || props.disabled) return
  if (reset) { applied = { vault: vault.value.trim(), state: state.value }; cursor.value = null; previous.value = []; detail.value = null }
  const ticket = ++generation; busy.value = true; failure.value = ''; stale.value = !!page.value || !!detail.value
  try {
    const result = await props.api.reclamationPlans({ ...applied, before: cursor.value })
    if (!alive || ticket !== generation) return
    page.value = result
    if (detail.value) await readDetail(detail.value.plan_id, ticket)
    stale.value = false
  } catch (error) { if (alive && ticket === generation) report(error) }
  finally { if (alive && ticket === generation) busy.value = false }
}
async function select(id: string) {
  if (!alive || busy.value || props.disabled) return
  const ticket = ++generation; busy.value = true; failure.value = ''; detail.value = null
  try { await readDetail(id, ticket) }
  catch (error) { if (alive && ticket === generation) report(error) }
  finally { if (alive && ticket === generation) busy.value = false }
}
async function paginate(next: boolean) {
  if (busy.value || props.disabled) return
  if (next) { if (!page.value?.next_before) return; previous.value.push(cursor.value); cursor.value = page.value.next_before }
  else { if (!previous.value.length) return; cursor.value = previous.value.pop() ?? null }
  detail.value = null; await load()
}
onMounted(() => { void load() })
onBeforeUnmount(() => { alive = false; generation++; clear() })
</script>

<template>
  <section class="surface reclamation-surface" aria-labelledby="reclamation-title">
    <div class="surface-heading"><div><p>Reclamation plans</p><h2 id="reclamation-title">对象回收只读核对</h2></div><button class="secondary-button" :disabled="busy || disabled" @click="load()">{{ busy ? '正在读取…' : '刷新原计划' }}</button></div>
    <p class="surface-intro">读取 CLI 已创建的原计划、冻结候选与对象回执。保护统计属于原计划快照；本次读取没有重新检查引用或备份。历史修订默认永久保留。</p>
    <form class="reclamation-filters" @submit.prevent="load(true)"><fieldset :disabled="busy || disabled"><label>知识库编号<input v-model="vault" maxlength="32" placeholder="全部知识库" autocomplete="off"></label><label>原计划状态<select v-model="state"><option value="">全部状态</option><option value="preview">原计划预览</option><option value="running">正在核对</option><option value="complete">已完成</option></select></label><button class="secondary-button" type="submit">筛选计划</button></fieldset></form>
    <form class="reclamation-lookup" @submit.prevent="select(lookupId.trim())"><label>按原计划编号核对<input v-model="lookupId" maxlength="32" required autocomplete="off"></label><button class="secondary-button" :disabled="busy || disabled" type="submit">读取原计划</button></form>
    <p v-if="failure" class="operations-error" role="alert">{{ failure }}</p><p v-if="busy" class="operations-updated" role="status">正在读取原快照与回执…</p>
    <p v-if="page" class="operations-updated">{{ stale ? '上次读取，尚未刷新' : '列表读取时间' }} · {{ time(page.observed_at) }}</p>
    <div class="admin-resource-list"><article v-for="item in page?.items" :key="item.plan_id" class="admin-resource"><div><strong>{{ states[item.state] }}</strong><small>{{ item.plan_id }}</small><p>知识库 {{ item.vault_id }}<br>创建 {{ time(item.created) }} · 已选择 {{ item.selected_objects }} / 原可选 {{ item.eligible_objects }} 个 · {{ item.candidate_bytes }} 字节</p><p v-if="item.expired && item.state === 'preview'">原冻结有效期已过</p></div><button class="secondary-button" :disabled="busy || disabled" @click="select(item.plan_id)">查看原计划</button></article></div>
    <p v-if="page && !page.items.length" class="empty-state">当前筛选下没有原计划。</p>
    <nav class="admin-pagination" aria-label="回收计划分页"><button class="secondary-button" :disabled="busy || disabled || !previous.length" @click="paginate(false)">上一页</button><span>{{ previous.length + 1 }}</span><button class="secondary-button" :disabled="busy || disabled || !page?.next_before" @click="paginate(true)">下一页</button></nav>
    <article v-if="detail" class="reclamation-detail">
      <h3>{{ states[detail.state] }} · 原计划快照</h3><p class="operations-updated">{{ stale ? '上次读取，尚未刷新' : '详情读取时间' }} · {{ time(detail.observed_at) }}</p>
      <dl><dt>原计划编号</dt><dd><code>{{ detail.plan_id }}</code></dd><dt>知识库</dt><dd>{{ detail.vault_id }}</dd><dt>原 CLI 操作者</dt><dd>{{ detail.actor_id }}</dd><dt>原状态代码</dt><dd>{{ detail.code }}</dd><dt>创建 / 冻结有效期</dt><dd>{{ time(detail.created) }} / {{ time(detail.expires) }}{{ detail.expired ? ' · 已过原有效期' : '' }}</dd><dt>最后完成回执</dt><dd>{{ time(detail.finished) }}</dd><dt>原备份 manifest SHA-256</dt><dd>{{ detail.backup_manifest_sha256 }}</dd><dt>原序列 / 登记用量</dt><dd>{{ detail.sequence }} / {{ detail.charged_bytes }} 字节</dd><dt>宽限期 / 原截止时间</dt><dd>{{ detail.preview.grace_hours }} 小时 / {{ time(detail.preview.cutoff) }}</dd></dl>
      <p>原可选 {{ detail.eligible_objects }} 个 / {{ detail.eligible_bytes }} 字节；本计划选择 {{ detail.selected_objects }} 个 / {{ detail.candidate_bytes }} 字节。{{ detail.has_more ? '原预览仅选择部分候选。' : '原候选已完整列入此计划。' }}</p>
      <h4>原保护统计</h4><ul><li v-for="(count, reason) in detail.preview.protected_counts" :key="reason">{{ reasons[reason] ?? reason }} · {{ count }} 个</li></ul><p v-if="!Object.keys(detail.preview.protected_counts).length">原预览没有记录受保护对象。</p>
      <h4>冻结候选 · {{ detail.preview.candidates.length }}</h4><ul class="reclamation-items"><li v-for="item in detail.preview.candidates" :key="item.hash"><code>{{ item.hash }}</code><span>{{ item.size }} 字节 · 原创建 {{ time(item.created) }}</span></li></ul>
      <h4>原对象回执</h4><p>已回收 {{ detail.reclaimed_objects }} 个 / {{ detail.reclaimed_bytes }} 字节 · 已保护 {{ detail.protected_objects }} 个 · 待核对 {{ detail.pending_objects }} 个</p><p v-if="!detail.items.length">尚无对象执行回执。</p><ul class="reclamation-items"><li v-for="item in detail.items" :key="item.hash"><code>{{ item.hash }}</code><span>{{ itemStates[item.state] }} · {{ item.code }} · {{ item.size }} 字节 · {{ time(item.confirmed_at) }}</span></li></ul>
      <p class="surface-intro">使用此原计划编号在部署主机运行现有 <code>gc-status</code> 核对。网页只读取快照与回执。</p>
    </article>
  </section>
</template>
