export interface ServiceStatus {
  health: boolean
  ready: boolean
  protocol: number
  maxObjectSize: number
}

export interface Session {
  access_token: string
  refresh_token: string
  expires_in: number
  device_id: string
  must_change_credentials: boolean
}

export interface Vault {
  id: string
  name: string
  sequence: number
  used: number
  quota: number
}

export interface Device {
  id: string
  name: string
  revoked: number | boolean
}

export type PlanState = 'preview' | 'running' | 'complete'
export interface ReclamationSummary {
  plan_id: string; vault_id: string; actor_id: string; state: PlanState; code: string
  created: number; expires: number; finished: number | null; expired: boolean
  sequence: number; charged_bytes: number; selected_objects: number; candidate_bytes: number
  eligible_objects: number; eligible_bytes: number; has_more: boolean
}
export interface ReclamationPage {
  schema_version: 1; snapshot_only: true; observed_at: number
  items: ReclamationSummary[]; next_before: string | null
}
export interface ReclamationCandidate { hash: string; size: number; created: number }
export interface ReclamationItem { hash: string; size: number; state: 'prepared' | 'reclaimed' | 'protected'; code: string; confirmed_at: number }
export interface ReclamationDetail extends ReclamationSummary {
  schema_version: 1; snapshot_only: true; observed_at: number; backup_manifest_sha256: string
  preview: {
    sequence: number; charged_bytes: number; grace_hours: number; cutoff: number
    policy: { history_retention: 'indefinite'; automatic_reclamation: false; default_grace_hours: number; minimum_grace_hours: number; protection: string[]; completion_after_reclamation: string }
    candidates: ReclamationCandidate[]; eligible_objects: number; candidate_bytes: number
    eligible_bytes: number; protected_counts: Record<string, number>; has_more: boolean
  }
  items: ReclamationItem[]; reclaimed_bytes: number; reclaimed_objects: number; protected_objects: number; pending_objects: number
}
const planId = /^[0-9a-f]{32}$/, contentHash = /^[0-9a-f]{64}$/
const planCursor = (value: unknown): value is string => typeof value === 'string' && /^[0-9]{1,16}:[0-9a-f]{32}$/.test(value) && Number.isSafeInteger(Number(value.split(':')[0]))
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const boundedText = (value: unknown, max: number): value is string => typeof value === 'string' && value.length <= max
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
function invalid(): never { throw new Error('INVALID_RESPONSE') }
function summaryValid(value: unknown, observed: number): value is ReclamationSummary {
  if (!object(value) || typeof value.plan_id !== 'string' || !planId.test(value.plan_id) || typeof value.vault_id !== 'string' || !planId.test(value.vault_id)) return false
  if (!boundedText(value.actor_id, 256) || !boundedText(value.code, 80) || !['preview', 'running', 'complete'].includes(String(value.state))) return false
  if (!['created', 'expires', 'sequence', 'charged_bytes', 'selected_objects', 'candidate_bytes', 'eligible_objects', 'eligible_bytes'].every(key => integer(value[key]))) return false
  if (value.finished !== null && !integer(value.finished)) return false
  return value.expired === ((value.expires as number) <= observed) && typeof value.has_more === 'boolean'
    && (value.selected_objects as number) <= 100 && (value.eligible_objects as number) >= (value.selected_objects as number)
    && (value.eligible_bytes as number) >= (value.candidate_bytes as number)
    && value.has_more === ((value.eligible_objects as number) > (value.selected_objects as number))
}
export function parseReclamationPage(value: unknown, before: string | null = null): ReclamationPage {
  if (!object(value) || value.schema_version !== 1 || value.snapshot_only !== true || !integer(value.observed_at) || !Array.isArray(value.items) || value.items.length > 20) invalid()
  const items = value.items
  if (!items.every(item => summaryValid(item, value.observed_at as number)) || new Set(items.map(item => item.plan_id)).size !== items.length) invalid()
  const precedes = (created: number, id: string, cursor: string) => {
    const [time, last] = cursor.split(':'); return created < Number(time) || created === Number(time) && id < last!
  }
  for (let i = 0; i < items.length; i++) {
    const item = items[i]!, previous = items[i-1]
    if (before && !precedes(item.created, item.plan_id, before) || previous && !precedes(item.created, item.plan_id, `${previous.created}:${previous.plan_id}`)) invalid()
  }
  if (value.next_before !== null && (!planCursor(value.next_before) || !items.length || value.next_before !== `${items.at(-1)!.created}:${items.at(-1)!.plan_id}`)) invalid()
  return value as unknown as ReclamationPage
}
export function parseReclamationDetail(value: unknown, expectedId: string): ReclamationDetail {
  if (!object(value) || value.schema_version !== 1 || value.snapshot_only !== true || !integer(value.observed_at) || !summaryValid(value, value.observed_at) || value.plan_id !== expectedId) invalid()
  if (typeof value.backup_manifest_sha256 !== 'string' || !contentHash.test(value.backup_manifest_sha256) || !object(value.preview)) invalid()
  const preview = value.preview
  if (!integer(preview.sequence) || !integer(preview.charged_bytes) || !integer(preview.cutoff) || typeof preview.grace_hours !== 'number' || !Number.isFinite(preview.grace_hours) || preview.grace_hours < 24 || preview.grace_hours > 24*3650) invalid()
  if (!object(preview.policy) || preview.policy.history_retention !== 'indefinite' || preview.policy.automatic_reclamation !== false || !integer(preview.policy.default_grace_hours) || !integer(preview.policy.minimum_grace_hours) || !boundedText(preview.policy.completion_after_reclamation, 80) || !Array.isArray(preview.policy.protection) || preview.policy.protection.length > 32 || !preview.policy.protection.every(item => boundedText(item, 80))) invalid()
  if (!object(preview.protected_counts) || Object.keys(preview.protected_counts).length > 32 || !Object.entries(preview.protected_counts).every(([key, count]) => key.length <= 80 && integer(count))) invalid()
  if (!Array.isArray(preview.candidates) || preview.candidates.length !== value.selected_objects || !preview.candidates.every(item => object(item) && typeof item.hash === 'string' && contentHash.test(item.hash) && integer(item.size) && integer(item.created))) invalid()
  if (new Set(preview.candidates.map(item => item.hash)).size !== preview.candidates.length) invalid()
  for (const key of ['sequence', 'charged_bytes', 'candidate_bytes', 'eligible_objects', 'eligible_bytes', 'has_more']) if (preview[key] !== value[key as keyof ReclamationSummary]) invalid()
  if (preview.candidates.reduce((sum, item) => sum + item.size, 0) !== value.candidate_bytes) invalid()
  if (!Array.isArray(value.items) || value.items.length > 100) invalid()
  const candidates = new Map(preview.candidates.map(item => [item.hash, item.size]))
  if (!value.items.every(item => object(item) && typeof item.hash === 'string' && candidates.get(item.hash) === item.size && integer(item.size) && ['prepared', 'reclaimed', 'protected'].includes(String(item.state)) && boundedText(item.code, 80) && integer(item.confirmed_at)) || new Set(value.items.map(item => item.hash)).size !== value.items.length) invalid()
  for (const [field, state] of [['reclaimed_objects', 'reclaimed'], ['protected_objects', 'protected'], ['pending_objects', 'prepared']]) if (value[field!] !== value.items.filter(item => item.state === state).length) invalid()
  if (!integer(value.reclaimed_bytes) || value.reclaimed_bytes !== value.items.filter(item => item.state === 'reclaimed').reduce((sum, item) => sum + item.size, 0)) invalid()
  return value as unknown as ReclamationDetail
}

export interface OperatorAccess { operator: true; user_id: string; deployment_default_quota: number }
export interface AdminAccount { id: string; username: string; default_quota: number; policy_revision: number }
export interface AdminAccounts { items: AdminAccount[]; next_before: string | null; confirmed_at: number }
export interface AdminVault extends Vault { reserved_bytes: number }
export interface AdminDetails {
  account: { id: string; username: string }; policy: { default_quota: number; revision: number }
  vaults: AdminVault[]; devices: Device[]; totals: { vault_count: number; charged_bytes: number; quota_bytes: number }
  vaults_next_before: string | null; devices_next_before: string | null; confirmed_at: number
}
export interface AdminDiagnostics {
  confirmed_at: number; accounting_mismatches: number; pending_reclamation_objects: number
  dependencies: { ready: boolean; code: string; cached: boolean; started_at: number | null; checked_at: number | null; probe_pending: boolean }
  upload_maintenance: { finished_at: number; duration_ms: number; removed: number; released_bytes: number; filesystem_failures: number; metadata_failures: number; total_removed: number; total_failures: number } | null
}
export interface AdminOperation { operation_id: string }
export interface AccountCreate extends AdminOperation { username: string; password: string; default_quota: number }
export interface DefaultQuota extends AdminOperation { expected_revision: number; quota: number }
export interface VaultQuota extends AdminOperation { expected_quota: number; quota: number }
export interface AdminReceipt {
  schema_version: 1; operation_id: string; confirmed_at: number; state: 'completed' | 'not_found'
  kind?: 'create_account' | 'default_quota' | 'vault_quota' | 'revoke_device'
  user_id?: string; username?: string; default_quota?: number; policy_revision?: number
  vault_id?: string; quota?: number; device_id?: string; revoked?: boolean
}

export interface Usage {
  schema_version: 1; confirmed_at: number; sequence: number; quota: number; charged_bytes: number
  object_bytes: number; object_count: number; current_object_bytes: number; historical_only_bytes: number
  unreferenced_object_bytes: number; logical_file_bytes: number; active_files: number; reserved_bytes: number
  confirmed_upload_bytes: number; pending_uploads: number; expired_uploads: number; available_bytes: number
  accounting_matches: boolean; history_retention: 'indefinite'
  reclamation_pending_objects: number; reclamation_pending_bytes: number
}
export interface PendingUpload {
  id: string; vault_id: string; device_id: string; device_name: string; device_revoked: number
  hash: string; size: number; offset_bytes: number; expires: number; state: 'active' | 'expired' | 'device_revoked'
}
export interface UploadPage { schema_version: 1; confirmed_at: number; items: PendingUpload[]; next_before: string | null }
export interface UploadResult {
  state: 'active' | 'expired' | 'cancelled' | 'completed' | 'damaged' | 'not_found' | 'reclaimed' | 'reclamation_pending'
  confirmed_at: number; size?: number; offset_bytes?: number; content_hash?: string; expires?: number
}

export interface OperationReceipt {
  sequence: number
  operation_id: string
  kind: 'backup' | 'verify' | 'restore'
  state: 'unfinished' | 'succeeded' | 'failed'
  code: string
  started_utc: string
  finished_utc: string | null
  object_count: number | null
  object_bytes: number | null
  verified_objects: number | null
  backup_age_seconds: number | null
}
export interface OperationsPage {
  schema: number
  configured: boolean
  items: OperationReceipt[]
  next_before: number | null
}

export interface Revision {
  sequence: number
  file_id: string
  base_revision: number
  path: string
  operation: 'put' | 'delete'
  hash: string | null
  size: number
  device_id: string
  device_name?: string | null
  operation_id: string
  created_at?: number | null
  restored_from?: number | null
}
export interface FilePage {
  items: Revision[]
  boundary: number
  has_more: boolean
  next_before: number | null
}
export interface HistoryPage extends FilePage { current: Revision | null }
interface PreviewMetadata { sequence: number; path: string; size: number; hash: string | null }
export type Preview = PreviewMetadata & (
  { kind: 'text'; format: string; text: string } |
  { kind: 'image'; mime: 'image/png'; data: string; width: number; height: number; first_frame_only: boolean } |
  { kind: 'deleted' } |
  { kind: 'unsupported'; reason: string; limit?: number }
)
export interface RestoreRequest {
  operation_id: string
  source_revision: number
  base_revision: number
  path: string | null
}
export interface RestoreTarget {
  path: string
  current_revision: number
  boundary: number
  occupied: { file_id: string; path: string } | null
}
export class SyncApiError extends Error {
  readonly status: number
  readonly details: Record<string, unknown>
  constructor(code: string, status: number, details: Record<string, unknown> = {}) {
    super(code)
    this.status = status
    this.details = details
  }
}
interface ErrorPayload {
  error?: { code?: string; details?: Record<string, unknown> }
}

async function safeJson<T>(response: Response): Promise<T | null> {
  try { return await response.json() as T }
  catch { return null }
}

export class SyncApi {
  private access = ''
  private refresh = ''
  deviceId = ''
  mustChangeCredentials = false
  private generation = 0
  private rotation?: { generation: number; request: Promise<boolean> }

  get signedIn() { return Boolean(this.access) }

  private async raw(path: string, init: RequestInit = {}, authenticate = true): Promise<Response> {
    const headers = new Headers(init.headers)
    if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    if (authenticate && this.access) headers.set('Authorization', `Bearer ${this.access}`)
    return fetch(path, { ...init, headers, cache: 'no-store', credentials: 'same-origin' })
  }

  private assertGeneration(generation: number) {
    if (generation !== this.generation) throw new Error('SESSION_CHANGED')
  }

  private async rotate(generation: number): Promise<boolean> {
    this.assertGeneration(generation)
    if (this.rotation?.generation === generation) return this.rotation.request
    if (!this.refresh) return false
    const refresh = this.refresh
    const request = (async () => {
      const response = await this.raw('/sync/v1/auth/refresh', {
        method: 'POST', body: JSON.stringify({ refresh_token: refresh }),
      }, false)
      this.assertGeneration(generation)
      if (!response.ok) return false
      const session = await safeJson<Session>(response)
      this.assertGeneration(generation)
      if (!session) return false
      this.accept(session)
      return true
    })()
    this.rotation = { generation, request }
    try { return await request }
    finally { if (this.rotation?.request === request) this.rotation = undefined }
  }

  private async request<T>(path: string, init: RequestInit = {}, allowRotate = true): Promise<T> {
    const generation = this.generation, access = this.access
    const controller = new AbortController()
    const current = () => { this.assertGeneration(generation); if (controller.signal.aborted) throw new Error('REQUEST_TIMEOUT') }
    const request = async (): Promise<T> => {
      let response = await this.raw(path, { ...init, signal: controller.signal })
      current()
      if (response.status === 401 && allowRotate && (this.access !== access || await this.rotate(generation))) {
        current()
        response = await this.raw(path, { ...init, signal: controller.signal })
      }
      current()
      if (!response.ok) {
        const payload = await safeJson<ErrorPayload>(response)
        current()
        if (response.status === 401) this.clear()
        throw new SyncApiError(payload?.error?.code ?? `HTTP_${response.status}`, response.status, payload?.error?.details)
      }
      if (response.status === 204) return undefined as T
      const payload = await safeJson<T>(response)
      current()
      if (payload === null) throw new Error('INVALID_RESPONSE')
      return payload
    }
    let timer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { reject(new Error('REQUEST_TIMEOUT')); controller.abort() }, 30_000)
    })
    try { return await Promise.race([request(), deadline]) }
    finally { clearTimeout(timer) }
  }

  private accept(session: Session) {
    this.access = session.access_token
    this.refresh = session.refresh_token
    this.deviceId = session.device_id
    this.mustChangeCredentials = Boolean(session.must_change_credentials)
  }

  async status(): Promise<ServiceStatus> {
    const [health, ready, handshake] = await Promise.allSettled([
      this.raw('/health', {}, false),
      this.raw('/ready', {}, false),
      this.raw('/sync/v1/handshake?protocol=1', {}, false),
    ])
    let protocol = 1
    let maxObjectSize = 100 * 1024 * 1024
    if (handshake.status === 'fulfilled' && handshake.value.ok) {
      const payload = await safeJson<{ protocol: number; max_object_size: number }>(handshake.value)
      protocol = payload?.protocol ?? protocol
      maxObjectSize = payload?.max_object_size ?? maxObjectSize
    }
    return {
      health: health.status === 'fulfilled' && health.value.ok,
      ready: ready.status === 'fulfilled' && ready.value.ok,
      protocol,
      maxObjectSize,
    }
  }

  async login(username: string, password: string, deviceName: string): Promise<boolean> {
    this.clear()
    const generation = this.generation
    const response = await this.raw('/sync/v1/auth/sessions', {
      method: 'POST',
      body: JSON.stringify({ username, password, device_name: deviceName }),
    }, false)
    this.assertGeneration(generation)
    if (!response.ok) {
      const payload = await safeJson<ErrorPayload>(response)
      throw new Error(payload?.error?.code ?? `HTTP_${response.status}`)
    }
    const session = await safeJson<Session>(response)
    this.assertGeneration(generation)
    if (!session) throw new Error('INVALID_RESPONSE')
    this.accept(session)
    return this.mustChangeCredentials
  }

  async changeCredentials(currentPassword: string, username: string, password: string) {
    const result = await this.request<{ username: string; credentials_fixed: boolean }>(
      '/sync/v1/account/credentials', {
        method: 'PUT', body: JSON.stringify({ current_password: currentPassword, username, password }),
      }, false,
    )
    this.mustChangeCredentials = false
    return result
  }

  vaults() { return this.request<{ items: Vault[] }>('/sync/v1/vaults') }
  operatorAccess() { return this.request<OperatorAccess>('/sync/v1/admin/access') }
  adminAccounts(before?: string | null) {
    const query = new URLSearchParams({ limit: '20' })
    if (before) query.set('before', before)
    return this.request<AdminAccounts>('/sync/v1/admin/accounts?' + query)
  }
  adminAccount(user: string, options: { vaultBefore?: string | null; deviceBefore?: string | null } = {}) {
    const query = new URLSearchParams({ limit: '20' })
    if (options.vaultBefore) query.set('vault_before', options.vaultBefore)
    if (options.deviceBefore) query.set('device_before', options.deviceBefore)
    return this.request<AdminDetails>(`/sync/v1/admin/accounts/${encodeURIComponent(user)}?${query}`)
  }
  adminDiagnostics() { return this.request<AdminDiagnostics>('/sync/v1/admin/diagnostics') }
  async reclamationPlans(options: { before?: string | null; vault?: string; state?: PlanState | '' } = {}) {
    if (options.before && !planCursor(options.before) || options.vault && !planId.test(options.vault) || options.state && !['preview', 'running', 'complete'].includes(options.state)) throw new Error('INVALID_REQUEST')
    const query = new URLSearchParams({ limit: '20' })
    if (options.before) query.set('before', options.before)
    if (options.vault) query.set('vault_id', options.vault)
    if (options.state) query.set('state', options.state)
    const page = parseReclamationPage(await this.request<unknown>('/sync/v1/admin/reclamation/plans?' + query), options.before ?? null)
    if (page.items.some(item => options.vault && item.vault_id !== options.vault || options.state && item.state !== options.state)) invalid()
    return page
  }
  async reclamationPlan(id: string) {
    if (!planId.test(id)) throw new Error('INVALID_REQUEST')
    return parseReclamationDetail(await this.request<unknown>('/sync/v1/admin/reclamation/plans/' + id), id)
  }
  createAccount(body: Readonly<AccountCreate>) {
    return this.request<AdminReceipt>('/sync/v1/admin/accounts', { method: 'POST', body: JSON.stringify(body) })
  }
  defaultQuota(user: string, body: Readonly<DefaultQuota>) {
    return this.request<AdminReceipt>(`/sync/v1/admin/accounts/${encodeURIComponent(user)}/policy`, { method: 'PUT', body: JSON.stringify(body) })
  }
  vaultQuota(user: string, vault: string, body: Readonly<VaultQuota>) {
    return this.request<AdminReceipt>(`/sync/v1/admin/accounts/${encodeURIComponent(user)}/vaults/${encodeURIComponent(vault)}/quota`, { method: 'PUT', body: JSON.stringify(body) })
  }
  revokeAccountDevice(user: string, device: string, body: Readonly<AdminOperation>) {
    return this.request<AdminReceipt>(`/sync/v1/admin/accounts/${encodeURIComponent(user)}/devices/${encodeURIComponent(device)}/revoke`, { method: 'POST', body: JSON.stringify(body) })
  }
  adminOperation(operation: string) {
    return this.request<AdminReceipt>(`/sync/v1/admin/operations/${encodeURIComponent(operation)}`)
  }
  files(vault: string, options: { q?: string; includeDeleted?: boolean; before?: number | null; boundary?: number; limit?: number } = {}) {
    const query = new URLSearchParams({ limit: String(options.limit ?? 30), q: options.q ?? '', include_deleted: String(options.includeDeleted ?? false) })
    if (options.before != null) query.set('before', String(options.before))
    if (options.boundary != null) query.set('boundary', String(options.boundary))
    return this.request<FilePage>(`/sync/v1/vaults/${encodeURIComponent(vault)}/files?${query}`)
  }
  file(vault: string, file: string) {
    return this.request<Revision>(`/sync/v1/vaults/${encodeURIComponent(vault)}/files/${encodeURIComponent(file)}`)
  }
  history(vault: string, file: string, options: { before?: number | null; boundary?: number; limit?: number } = {}) {
    const query = new URLSearchParams({ limit: String(options.limit ?? 30) })
    if (options.before != null) query.set('before', String(options.before))
    if (options.boundary != null) query.set('boundary', String(options.boundary))
    return this.request<HistoryPage>(`/sync/v1/vaults/${encodeURIComponent(vault)}/history/${encodeURIComponent(file)}?${query}`)
  }
  preview(vault: string, file: string, sequence: number) {
    return this.request<Preview>(`/sync/v1/vaults/${encodeURIComponent(vault)}/history/${encodeURIComponent(file)}/${sequence}/preview`)
  }
  restoreTarget(vault: string, file: string, path: string) {
    const query = new URLSearchParams({ file_id: file, path })
    return this.request<RestoreTarget>(`/sync/v1/vaults/${encodeURIComponent(vault)}/restore-target?${query}`)
  }
  restore(vault: string, file: string, intent: Readonly<RestoreRequest>) {
    return this.request<Revision>(`/sync/v1/vaults/${encodeURIComponent(vault)}/files/${encodeURIComponent(file)}/restore`, {
      method: 'POST', body: JSON.stringify(intent),
    })
  }
  restoreResult(vault: string, file: string, operation: string) {
    return this.request<Revision>(`/sync/v1/vaults/${encodeURIComponent(vault)}/files/${encodeURIComponent(file)}/restore-results/${encodeURIComponent(operation)}`)
  }
  devices() { return this.request<{ items: Device[] }>('/sync/v1/devices') }
  usage(vault: string) { return this.request<Usage>(`/sync/v1/vaults/${encodeURIComponent(vault)}/usage`) }
  uploads(vault: string, before?: string | null) {
    const query = new URLSearchParams({ limit: '20' })
    if (before) query.set('before', before)
    return this.request<UploadPage>(`/sync/v1/vaults/${encodeURIComponent(vault)}/uploads?${query}`)
  }
  uploadResult(vault: string, upload: string) {
    return this.request<UploadResult>(`/sync/v1/vaults/${encodeURIComponent(vault)}/uploads/${encodeURIComponent(upload)}/result`)
  }
  cancelUpload(vault: string, upload: string) {
    return this.request<UploadResult>(`/sync/v1/vaults/${encodeURIComponent(vault)}/uploads/${encodeURIComponent(upload)}/cancel`, { method: 'POST' })
  }
  operations(limit = 20, before?: number | null) {
    const query = new URLSearchParams({ limit: String(limit) })
    if (before != null) query.set('before', String(before))
    return this.request<OperationsPage>('/sync/v1/operations?' + query)
  }
  createVault(name: string) {
    return this.request<{ vault_id: string; name: string }>('/sync/v1/vaults', {
      method: 'POST', body: JSON.stringify({ name }),
    })
  }
  revokeDevice(id: string) {
    return this.request<void>(`/sync/v1/devices/${encodeURIComponent(id)}`, { method: 'DELETE' })
  }
  async logout(): Promise<void> {
    try {
      if (this.access) await this.request<void>('/sync/v1/auth/sessions', { method: 'DELETE' }, false)
    } finally { this.clear() }
  }
  clear() {
    this.generation++
    this.access = ''
    this.refresh = ''
    this.deviceId = ''
    this.mustChangeCredentials = false
  }
}
