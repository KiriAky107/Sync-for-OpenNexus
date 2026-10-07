export interface DiffLine { kind: 'same' | 'remove' | 'add'; text: string; current: number | null; source: number | null }
export type TextComparison = { kind: 'equal' } | { kind: 'large' } | { kind: 'diff'; lines: DiffLine[] }

// Bound both work and rendered rows; the two complete previews remain available.
export function compareText(current: string, source: string): TextComparison {
  if (current === source) return { kind: 'equal' }
  if (current.length + source.length > 128 * 1024) return { kind: 'large' }
  const left = current.split('\n'), right = source.split('\n')
  if (left.length + right.length > 600) return { kind: 'large' }
  const width = right.length + 1
  const lengths = new Uint16Array((left.length + 1) * width)
  for (let i = left.length - 1; i >= 0; i--) {
    for (let j = right.length - 1; j >= 0; j--) {
      lengths[i * width + j] = left[i] === right[j]
        ? 1 + lengths[(i + 1) * width + j + 1]
        : Math.max(lengths[(i + 1) * width + j], lengths[i * width + j + 1])
    }
  }
  const lines: DiffLine[] = []
  let i = 0, j = 0
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) {
      lines.push({ kind: 'same', text: left[i], current: ++i, source: ++j })
    } else if (i < left.length && (j === right.length || lengths[(i + 1) * width + j] >= lengths[i * width + j + 1])) {
      lines.push({ kind: 'remove', text: left[i], current: ++i, source: null })
    } else {
      lines.push({ kind: 'add', text: right[j], current: null, source: ++j })
    }
  }
  return { kind: 'diff', lines }
}
