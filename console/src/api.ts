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

interface ErrorPayload {
  error?: { code?: string }
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
    let response = await this.raw(path, init)
    this.assertGeneration(generation)
    if (response.status === 401 && allowRotate && (this.access !== access || await this.rotate(generation))) {
      this.assertGeneration(generation)
      response = await this.raw(path, init)
    }
    this.assertGeneration(generation)
    if (!response.ok) {
      const payload = await safeJson<ErrorPayload>(response)
      this.assertGeneration(generation)
      if (response.status === 401) this.clear()
      throw new Error(payload?.error?.code ?? `HTTP_${response.status}`)
    }
    if (response.status === 204) return undefined as T
    const payload = await safeJson<T>(response)
    this.assertGeneration(generation)
    if (payload === null) throw new Error('INVALID_RESPONSE')
    return payload
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
  devices() { return this.request<{ items: Device[] }>('/sync/v1/devices') }
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
