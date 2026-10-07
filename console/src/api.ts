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
