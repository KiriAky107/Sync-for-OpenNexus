import { messages } from './messages.ts'

export type Locale = 'zh-CN' | 'en'
export const preferenceKey = 'opennexus-sync-language'
export function isLocale(value: unknown): value is Locale { return value === 'zh-CN' || value === 'en' }
export function preferredLocale(read: () => string | null, browserLanguage: string): Locale {
  try { const stored = read(); if (isLocale(stored)) return stored } catch { /* Storage may be disabled. */ }
  return /^zh(?:-|$)/i.test(browserLanguage) ? 'zh-CN' : 'en'
}
export function translate(key: string, locale: Locale, parameters: Record<string, string | number> = {}): string {
  const pair = Object.hasOwn(messages, key) ? messages[key] : undefined
  const text = pair ? pair[locale === 'en' ? 1 : 0] : key
  // One pass: an author's text supplied as a parameter is never treated as another message.
  return text.replace(/\{([a-z]+)\}/g, (token, name: string) => Object.hasOwn(parameters, name) ? String(parameters[name]) : token)
}
export function localizedDate(value: number | string, locale: Locale): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(locale === 'en' ? 'en-US' : 'zh-CN', { hour12: false })
}

export const localizedNumber = (value: number, locale: Locale): string => value.toLocaleString(locale === 'en' ? 'en-US' : 'zh-CN')

export function localizedAdminLabel(kind: string, label: string, locale: Locale): string {
  const suffix = '新知识库默认配额'
  return kind === 'default_quota' && label.endsWith(suffix)
    ? label.slice(0, -suffix.length) + translate(suffix, locale)
    : label
}
