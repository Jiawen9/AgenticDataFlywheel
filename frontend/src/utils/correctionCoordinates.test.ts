import { describe, expect, it } from 'vitest'
import { changesCoordinates, fromImageFraction, parseActionForm, serializeActionForm, toImagePoint } from './correctionCoordinates'

const pixel = { space: 'pixel' as const, width: 1440, height: 3200 }
describe('correction coordinate contract', () => {
  it('round trips source pixels through a scaled image and preserves points over 999', () => {
    const point = fromImageFraction({ x: .75, y: .5 }, pixel)!
    expect(point).toEqual({ x: 1080, y: 1600 })
    expect(toImagePoint(point, { x: 360, y: 800 }, pixel)).toEqual({ x: 270, y: 400 })
    expect(fromImageFraction({ x: 1, y: 1 }, pixel)).toEqual({ x: 1439, y: 3199 })
    expect(fromImageFraction({ x: -.1, y: 1.2 }, pixel)).toEqual({ x: 0, y: 3199 })
  })
  it('keeps the existing NewAgent 999 editor convention and cannot pick unknown points', () => {
    expect(fromImageFraction({ x: 1, y: 1 }, { space: 'normalized' })).toEqual({ x: 999, y: 999 })
    expect(toImagePoint({ x: 999, y: 999 }, { x: 1080, y: 2340 }, { space: 'normalized' })).toEqual({ x: 1080, y: 2340 })
    expect(fromImageFraction({ x: .5, y: .5 }, { space: 'unknown' })).toBeNull()
    expect(toImagePoint({ x: 1, y: 2 }, { x: 1080, y: 2340 }, { space: 'pixel' })).toBeNull()
  })
  it('preserves original bytes and extra fields when an action is untouched', () => {
    const raw = '{ "action":"click", "coordinate":[1080,1600], "duration":2, "extra":{"a":1} }'
    const original = parseActionForm(raw)
    expect(serializeActionForm(raw, original, { ...original })).toBe(raw)
    expect(serializeActionForm('not json', parseActionForm('not json'), parseActionForm('not json'))).toBe('not json')
    const changed = JSON.parse(serializeActionForm(raw, original, { ...original, y: 2000 }))
    expect(changed).toEqual({ action: 'click', coordinate: [1080, 2000], duration: 2, extra: { a: 1 } })
  })
  it('preserves swipe metadata and both source pixel endpoints', () => {
    const raw = '{"action":"swipe","start_coordinate":[1200,2000],"end_coordinate":[100,300],"time":0.4}'
    const original = parseActionForm(raw)
    const updated = { ...original, sy: 2400 }
    expect(JSON.parse(serializeActionForm(raw, original, updated))).toEqual({ action: 'swipe', start_coordinate: [1200, 2400], end_coordinate: [100, 300], time: .4 })
  })
  it('allows text and non-coordinate changes without coordinate metadata', () => {
    const raw = '{"action":"click","coordinate":[1200,2000],"extra":true}'
    const original = parseActionForm(raw)
    const updated = { ...original, action: 'type', text: '中文' }
    expect(changesCoordinates(updated, original)).toBe(false)
    expect(JSON.parse(serializeActionForm(raw, original, updated))).toEqual({ action: 'type', text: '中文', extra: true })
    expect(changesCoordinates({ ...original, y: 2 }, original)).toBe(true)
  })
})
