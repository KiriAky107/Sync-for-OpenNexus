export interface CleanupCounts {
  kind: 'uploads'; started_at: number; finished_at: number; duration_ms: number
  selected: number; removed: number; released_bytes: number; filesystem_failures: number; metadata_failures: number
}
export interface CleanupItem extends CleanupCounts { id: string }
export interface CleanupSummary extends CleanupCounts { total_removed: number; total_failures: number }
export interface BackupReminder {
  finished_utc: string; seconds_ago: number | null; state: 'recent_receipt' | 'stale_receipt' | 'clock_ahead'
  recorded_backup_age_seconds: number | null
}
export interface Capacity {
  vaults: number; blocked: number; near_quota: number; warning_percent: 80
  basis: 'charged_objects_and_active_upload_reservations'
}
export interface BackupReminders {
  configured: boolean; threshold_seconds: 86400; archive_association_known: false
  unfinished_operations: number; failed_operations: number; backup: BackupReminder | null; verify: BackupReminder | null
}
interface ObservationSummary {
  schema_version: 1; observed_at: number; history_retention: 'indefinite'; cache_seconds: 0
  capacity: Capacity; accounting_mismatches: number; pending_reclamation_objects: number; backup_receipts: BackupReminders
}
export interface Observability extends ObservationSummary {
  snapshot_only: true; latest_cleanup: CleanupSummary | null
  history: { items: CleanupItem[]; next_before: string | null; retention_limit: 1000; idle_checks_recorded: false }
}
export interface RedactedExport extends ObservationSummary {
  redacted: true; cleanup_retention_limit: 1000; cleanup_page: CleanupCounts[]
}
const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const integer = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const timestamp = (v: unknown) => integer(v) && v <= 8640000000000
function invalid(): never { throw new Error('INVALID_RESPONSE') }
export const validCleanupCursor = (v: unknown): v is string => typeof v === 'string'
  && /^[0-9]{1,16}:[a-f0-9]{32}$/.test(v) && Number.isSafeInteger(Number(v.split(':')[0]))
function cleanup(v: unknown): CleanupCounts {
  if (!isObject(v) || v.kind !== 'uploads' || !timestamp(v.started_at) || !timestamp(v.finished_at)
    || !['duration_ms','selected','removed','released_bytes','filesystem_failures','metadata_failures'].every(k => integer(v[k]))
    || (v.removed as number) > (v.selected as number)) invalid()
  // Explicit projection also keeps unrecognized fields out of diagnostic exports.
  return { kind: 'uploads', started_at: v.started_at as number, finished_at: v.finished_at as number,
    duration_ms: v.duration_ms as number, selected: v.selected as number, removed: v.removed as number,
    released_bytes: v.released_bytes as number, filesystem_failures: v.filesystem_failures as number, metadata_failures: v.metadata_failures as number }
}
function reminder(v: unknown, kind: 'backup' | 'verify'): BackupReminder | null {
  if (v === null) return null
  if (!isObject(v) || typeof v.finished_utc !== 'string' || v.finished_utc.length > 40
    || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$/.test(v.finished_utc) || !Number.isFinite(Date.parse(v.finished_utc))
    || !['recent_receipt','stale_receipt','clock_ahead'].includes(String(v.state))
    || !integer(v.seconds_ago) || v.state === 'clock_ahead' && v.seconds_ago !== 0
    || v.state === 'recent_receipt' && (v.seconds_ago as number) > 86400
    || v.state === 'stale_receipt' && (v.seconds_ago as number) <= 86400
    || v.recorded_backup_age_seconds !== null && !integer(v.recorded_backup_age_seconds)
    || kind === 'backup' && v.recorded_backup_age_seconds !== null) invalid()
  return { finished_utc: v.finished_utc as string, seconds_ago: v.seconds_ago as number | null,
    state: v.state as BackupReminder['state'], recorded_backup_age_seconds: v.recorded_backup_age_seconds as number | null }
}
function summary(v: unknown): ObservationSummary {
  if (!isObject(v) || v.schema_version !== 1 || !integer(v.observed_at) || v.observed_at > 8640000000000
    || v.history_retention !== 'indefinite' || v.cache_seconds !== 0
    || !integer(v.accounting_mismatches) || !integer(v.pending_reclamation_objects) || !isObject(v.capacity)
    || !isObject(v.backup_receipts)) invalid()
  const c = v.capacity, b = v.backup_receipts
  if (!['vaults','near_quota','blocked'].every(k => integer(c[k])) || c.warning_percent !== 80
    || c.basis !== 'charged_objects_and_active_upload_reservations'
    || (c.near_quota as number) + (c.blocked as number) > (c.vaults as number)
    || typeof b.configured !== 'boolean' || b.threshold_seconds !== 86400 || b.archive_association_known !== false
    || !integer(b.unfinished_operations) || !integer(b.failed_operations)) invalid()
  const backup = reminder(b.backup, 'backup'), verify = reminder(b.verify, 'verify')
  if (!b.configured && (backup || verify || b.unfinished_operations || b.failed_operations)) invalid()
  return { schema_version: 1, observed_at: v.observed_at as number, history_retention: 'indefinite', cache_seconds: 0,
    accounting_mismatches: v.accounting_mismatches as number, pending_reclamation_objects: v.pending_reclamation_objects as number,
    capacity: { vaults: c.vaults as number, near_quota: c.near_quota as number, blocked: c.blocked as number,
      warning_percent: 80, basis: 'charged_objects_and_active_upload_reservations' },
    backup_receipts: { configured: b.configured as boolean, threshold_seconds: 86400, archive_association_known: false,
      unfinished_operations: b.unfinished_operations as number, failed_operations: b.failed_operations as number, backup, verify } }
}
export function parseObservability(v: unknown, before: string | null = null): Observability {
  const result = summary(v)
  if (!isObject(v) || v.snapshot_only !== true || !isObject(v.history) || v.history.retention_limit !== 1000
    || v.history.idle_checks_recorded !== false || !Array.isArray(v.history.items) || v.history.items.length > 20) invalid()
  let previous = before
  const items: CleanupItem[] = v.history.items.map((item: unknown) => {
    const counts = cleanup(item)
    if (!isObject(item) || typeof item.id !== 'string' || !/^[a-f0-9]{32}$/.test(item.id)) invalid()
    const current = `${counts.finished_at}:${item.id}`
    if (previous) { const [time, id] = previous.split(':'); if (counts.finished_at > Number(time) || counts.finished_at === Number(time) && item.id >= id!) invalid() }
    previous = current; return { ...counts, id: item.id }
  })
  if (v.history.next_before !== null && (!items.length || !validCleanupCursor(v.history.next_before) || v.history.next_before !== previous)) invalid()
  let latest: CleanupSummary | null = null
  if (v.latest_cleanup !== null) {
    const counts = cleanup(v.latest_cleanup)
    if (!isObject(v.latest_cleanup) || !integer(v.latest_cleanup.total_removed) || !integer(v.latest_cleanup.total_failures)
      || v.latest_cleanup.total_removed < counts.removed) invalid()
    latest = { ...counts, total_removed: v.latest_cleanup.total_removed, total_failures: v.latest_cleanup.total_failures }
  }
  return { ...result, snapshot_only: true, latest_cleanup: latest, history: { items, next_before: v.history.next_before as string | null,
    retention_limit: 1000, idle_checks_recorded: false } }
}
export function parseRedactedExport(v: unknown): RedactedExport {
  const result = summary(v)
  if (!isObject(v) || v.redacted !== true || v.cleanup_retention_limit !== 1000 || !Array.isArray(v.cleanup_page) || v.cleanup_page.length > 20) invalid()
  return { ...result, redacted: true, cleanup_retention_limit: 1000, cleanup_page: v.cleanup_page.map(cleanup) }
}
