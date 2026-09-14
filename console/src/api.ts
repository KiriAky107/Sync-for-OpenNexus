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

  get signedIn() { return Boolean(this.access) }

  private async raw(path: string, init: RequestInit = {}, authenticate = true): Promise<Response> {
    const headers = new Headers(init.headers)
    if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    if (authenticate && this.access) headers.set('Authorization', `Bearer ${this.access}`)
    return fetch(path, { ...init, headers, cache: 'no-store', credentials: 'same-origin' })
  }

  private async rotate(): Promise<boolean> {
    if (!this.refresh) return false
    const response = await this.raw('/sync/v1/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: this.refresh }),
    }, false)
    if (!response.ok) return false
    const session = await safeJson<Session>(response)
    if (!session) return false
    this.accept(session)
    return true
  }

  private async request<T>(path: string, init: RequestInit = {}, allowRotate = true): Promise<T> {
    let response = await this.raw(path, init)
    if (response.status === 401 && allowRotate && await this.rotate()) response = await this.raw(path, init)
    if (!response.ok) {
      const payload = await safeJson<ErrorPayload>(response)
      if (response.status === 401) this.clear()
      throw new Error(payload?.error?.code ?? `HTTP_${response.status}`)
    }
    if (response.status === 204) return undefined as T
    const payload = await safeJson<T>(response)
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
    const response = await this.raw('/sync/v1/auth/sessions', {
      method: 'POST',
      body: JSON.stringify({ username, password, device_name: deviceName }),
    }, false)
    if (!response.ok) {
      const payload = await safeJson<ErrorPayload>(response)
      throw new Error(payload?.error?.code ?? `HTTP_${response.status}`)
    }
    const session = await safeJson<Session>(response)
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
    this.access = ''
    this.refresh = ''
    this.deviceId = ''
    this.mustChangeCredentials = false
  }
}
