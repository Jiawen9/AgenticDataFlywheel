import type { ActionPayload } from '@/types'

export interface BBox {
  x1: number
  y1: number
  x2: number
  y2: number
}

export interface Point {
  x: number
  y: number
}

const BBOX_RE = /<bbox>\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]<\/bbox>/i
const DIRECTION_RE = /\bdirection\s*=\s*(left|right|up|down)\b/i

export function parseBBox(value: string): BBox | null {
  const match = BBOX_RE.exec(value || '')
  if (!match) return null
  const [xa, ya, xb, yb] = match.slice(1).map(Number)
  const x1 = Math.min(xa, xb)
  const y1 = Math.min(ya, yb)
  const x2 = Math.max(xa, xb)
  const y2 = Math.max(ya, yb)
  return x2 > x1 && y2 > y1 ? { x1, y1, x2, y2 } : null
}

export function normalizedPoint(value: unknown, width: number, height: number): Point | null {
  if (!Array.isArray(value) || value.length < 2) return null
  const x = Number(value[0])
  const y = Number(value[1])
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return { x: (x / 1000) * width, y: (y / 1000) * height }
}

export function actionKind(action: ActionPayload): string {
  return typeof action.action === 'string' ? action.action : 'unknown'
}

export function swipeDirection(actionsBox: string): 'left' | 'right' | 'up' | 'down' | null {
  const match = DIRECTION_RE.exec(actionsBox || '')
  return match ? (match[1].toLowerCase() as 'left' | 'right' | 'up' | 'down') : null
}

export function fallbackSwipePoints(box: BBox, direction: string | null): [Point, Point] {
  const cx = (box.x1 + box.x2) / 2
  const cy = (box.y1 + box.y2) / 2
  const dx = (box.x2 - box.x1) * 0.32
  const dy = (box.y2 - box.y1) * 0.32
  if (direction === 'left') return [{ x: cx + dx, y: cy }, { x: cx - dx, y: cy }]
  if (direction === 'right') return [{ x: cx - dx, y: cy }, { x: cx + dx, y: cy }]
  if (direction === 'down') return [{ x: cx, y: cy - dy }, { x: cx, y: cy + dy }]
  return [{ x: cx, y: cy + dy }, { x: cx, y: cy - dy }]
}
