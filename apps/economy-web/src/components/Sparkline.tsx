import type { PricePoint } from '../types'

export function Sparkline({ points, ariaLabel }: { points: PricePoint[]; ariaLabel: string }) {
  if (points.length < 2) return <span className="text-muted-foreground">—</span>
  const values = points.map((point) => point.value)
  const minimum = Math.min(...values)
  const range = Math.max(...values) - minimum || 1
  const path = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 118 + 1
    const y = 31 - ((value - minimum) / range) * 28
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  return <svg className="h-8 w-30" viewBox="0 0 120 34" preserveAspectRatio="none" aria-label={ariaLabel}><polyline points={path} fill="none" stroke="currentColor" strokeWidth="2" className="text-sky-400" /></svg>
}
