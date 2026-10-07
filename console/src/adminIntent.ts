import type { AccountCreate, AdminReceipt, DefaultQuota, SyncApi, VaultQuota } from './api'

export type AdminIntent = Readonly<
  { kind: 'create_account'; label: string; body: Readonly<AccountCreate> } |
  { kind: 'default_quota'; label: string; user: string; body: Readonly<DefaultQuota> } |
  { kind: 'vault_quota'; label: string; user: string; vault: string; body: Readonly<VaultQuota> } |
  { kind: 'revoke_device'; label: string; user: string; device: string; body: Readonly<{ operation_id: string }> }
>
export function operationId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
}
export function quotaBytes(value: string, unit: number): number {
  const text = value.trim()
  const match = /^(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d{1,2}))?$/.exec(text)
  if (!match || text.length > 64 || ![1, 1024, 1024**2, 1024**3].includes(unit)) throw new Error('INVALID_QUOTA')
  const fraction = match[2] ?? match[3] ?? ''
  let numerator = BigInt((match[1] ?? '0') + fraction) * BigInt(unit)
  const scale = fraction.length - Number(match[4] ?? 0)
  const denominator = scale >= 0 ? 10n**BigInt(scale) : 1n
  if (scale < 0) numerator *= 10n**BigInt(-scale)
  if (numerator % denominator !== 0n || numerator / denominator > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('INVALID_QUOTA')
  return Number(numerator / denominator)
}
export function execute(api: SyncApi, intent: AdminIntent): Promise<AdminReceipt> {
  if (intent.kind === 'create_account') return api.createAccount(intent.body)
  if (intent.kind === 'default_quota') return api.defaultQuota(intent.user, intent.body)
  if (intent.kind === 'vault_quota') return api.vaultQuota(intent.user, intent.vault, intent.body)
  return api.revokeAccountDevice(intent.user, intent.device, intent.body)
}
export function validateReceipt(result: AdminReceipt, intent: AdminIntent): void {
  let valid = result.schema_version === 1 && result.operation_id === intent.body.operation_id && Number.isSafeInteger(result.confirmed_at) && result.confirmed_at >= 0
  if (result.state === 'not_found') { if (!valid) throw new Error('INVALID_RESPONSE'); return }
  valid &&= result.state === 'completed' && result.kind === intent.kind
  if (intent.kind === 'create_account') valid &&= result.username === intent.body.username && /^[0-9a-f]{32}$/.test(result.user_id ?? '')
  else {
    valid &&= result.user_id === intent.user
    if (intent.kind === 'default_quota') valid &&= result.default_quota === intent.body.quota
    else if (intent.kind === 'vault_quota') valid &&= result.vault_id === intent.vault && result.quota === intent.body.quota
    else valid &&= result.device_id === intent.device && result.revoked === true
  }
  if (!valid) throw new Error('INVALID_RESPONSE')
}
