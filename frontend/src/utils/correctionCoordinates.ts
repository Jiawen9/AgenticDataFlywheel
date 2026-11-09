import type { CoordinateInfo } from '@/types'
import type { Point } from './actionOverlay'

/** Keep the editor's historical 0..999 convention for NewAgent. */
export function coordinateExtent(info: CoordinateInfo): Point | null {
  if (info.space === 'normalized') return { x: 999, y: 999 }
  if (info.space === 'pixel' && Number.isFinite(info.width) && Number.isFinite(info.height)
      && (info.width ?? 0) > 0 && (info.height ?? 0) > 0) return { x: info.width!, y: info.height! }
  return null
}

export function toImagePoint(point: Point, image: Point, info: CoordinateInfo): Point | null {
  const extent = coordinateExtent(info)
  return extent ? { x: point.x / extent.x * image.x, y: point.y / extent.y * image.y } : null
}

export function fromImageFraction(point: Point, info: CoordinateInfo): Point | null {
  const extent = coordinateExtent(info)
  if (!extent) return null
  const maxX = info.space === 'pixel' ? Math.max(0, extent.x - 1) : extent.x
  const maxY = info.space === 'pixel' ? Math.max(0, extent.y - 1) : extent.y
  return { x: Math.min(maxX, Math.max(0, Math.round(point.x * extent.x))),
    y: Math.min(maxY, Math.max(0, Math.round(point.y * extent.y))) }
}

export interface ActionForm {
  action: string; x: number; y: number; sx: number; sy: number; ex: number; ey: number
  text: string; button: string; status: string
}

export function emptyActionForm(): ActionForm {
  return { action: 'click', x: 500, y: 500, sx: 250, sy: 500, ex: 750, ey: 500, text: '', button: 'back', status: 'success' }
}

export function parseActionForm(raw: string): ActionForm {
  const form = emptyActionForm()
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>
    if (typeof parsed.action === 'string') form.action = parsed.action
    for (const [name, x, y] of [['coordinate', 'x', 'y'], ['start_coordinate', 'sx', 'sy'], ['end_coordinate', 'ex', 'ey']] as const) {
      const point = parsed[name]
      if (Array.isArray(point)) { form[x] = Number(point[0]) || 0; form[y] = Number(point[1]) || 0 }
    }
    for (const name of ['text', 'button', 'status'] as const) if (typeof parsed[name] === 'string') form[name] = parsed[name]
  } catch { /* Allow repairing malformed legacy actions without changing them on load. */ }
  return form
}

export function changesCoordinates(form: ActionForm, original: ActionForm): boolean {
  if (['click', 'long_press'].includes(form.action)) return form.action !== original.action || form.x !== original.x || form.y !== original.y
  if (form.action === 'swipe') return form.action !== original.action || (['sx', 'sy', 'ex', 'ey'] as const).some(key => form[key] !== original[key])
  return false
}

/** A no-op is byte-preserving. Editing supported fields keeps unrelated action metadata. */
export function serializeActionForm(raw: string, original: ActionForm, form: ActionForm): string {
  if (JSON.stringify(original) === JSON.stringify(form)) return raw
  let value: Record<string, unknown> = {}
  try { const parsed = JSON.parse(raw); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) value = { ...parsed } } catch { /* User is repairing an action. */ }
  if (form.action !== original.action) {
    for (const key of ['coordinate', 'start_coordinate', 'end_coordinate', 'text', 'button', 'status']) delete value[key]
  }
  value.action = form.action
  if (['click', 'long_press'].includes(form.action) && changesCoordinates(form, original)) value.coordinate = [form.x, form.y]
  if (form.action === 'swipe' && changesCoordinates(form, original)) {
    value.start_coordinate = [form.sx, form.sy]
    value.end_coordinate = [form.ex, form.ey]
  }
  if (['type', 'open', 'answer'].includes(form.action)) value.text = form.text
  if (form.action === 'system_button') value.button = form.button
  if (form.action === 'terminate') value.status = form.status
  return JSON.stringify(value, null, 2)
}
