import { readonly, ref } from 'vue'
import { isLocale, localizedAdminLabel, localizedNumber, localizedDate, preferenceKey, preferredLocale, translate } from './localeCore.ts'
const selected = ref<'zh-CN' | 'en'>('zh-CN')
export const locale = readonly(selected)
export function initializeLocale(): void {
  selected.value = preferredLocale(() => localStorage.getItem(preferenceKey), navigator.language)
  document.documentElement.lang = selected.value
}
export function setLocale(value: unknown): void {
  if (!isLocale(value)) return
  selected.value = value
  document.documentElement.lang = value
  try { localStorage.setItem(preferenceKey, value) } catch { /* The selected language still works in this page. */ }
}
export const t = (key: string, parameters?: Record<string, string | number>): string => translate(key, selected.value, parameters)
export const formatDate = (value: number | string): string => localizedDate(value, selected.value)

export const formatNumber = (value: number): string => localizedNumber(value, selected.value)
export const formatAdminLabel = (action: { kind: string; label: string }): string => localizedAdminLabel(action.kind, action.label, selected.value)
