import { describe, expect, it } from 'vitest'
import { fallbackSwipePoints, normalizedPoint, parseBBox, swipeDirection } from './actionOverlay'

describe('action overlay coordinates', () => {
  it('parses and normalizes bbox coordinates', () => {
    expect(parseBBox('click(bbox=<bbox>[810,180,1034,418]</bbox>)')).toEqual({
      x1: 810,
      y1: 180,
      x2: 1034,
      y2: 418,
    })
    expect(parseBBox('click(bbox=<bbox>[4,5,4,8]</bbox>)')).toBeNull()
  })

  it('maps VLA coordinates independently on both axes', () => {
    expect(normalizedPoint([500, 250], 1080, 2340)).toEqual({ x: 540, y: 585 })
  })

  it('builds a fallback arrow from swipe bbox direction', () => {
    expect(swipeDirection('swipe_screen(bbox=<bbox>[0,0,100,200]</bbox>, direction=left)')).toBe('left')
    const [start, end] = fallbackSwipePoints({ x1: 0, y1: 0, x2: 100, y2: 200 }, 'left')
    expect(start.x).toBeGreaterThan(end.x)
  })
})
