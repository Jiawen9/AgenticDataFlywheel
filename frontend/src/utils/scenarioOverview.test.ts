import { describe, expect, it } from 'vitest'
import type { TaskGenerationTreeNode } from '@/types'
import { capabilityNodeHeight, capabilityTreeGeometry, capabilityTreeLayout, scenarioDomains, scenarioIcon, scenarioMetrics, scenarioSpotlight } from './scenarioOverview'

const fixture = (): TaskGenerationTreeNode[] => [
  {
    id: 'scene-a', kind: 'scene', label: '电商购物', children: [
      { id: 'cap-a', kind: 'capability', label: '商品搜索', children: [
        { id: 'sub-a', kind: 'sub_capability', label: '关键词搜索', children: [
          { id: 'app-a', kind: 'app', label: '天猫', app: '天猫' },
          { id: 'app-b', kind: 'app', label: '淘宝', app: '淘宝' },
        ] },
      ] },
      { id: 'cap-b', kind: 'capability', label: '订单管理', children: [
        { id: 'sub-b', kind: 'sub_capability', label: '订单查询', children: [{ id: 'app-c', kind: 'app', label: '天猫', app: '天猫' }] },
      ] },
    ],
  },
]

describe('scenario overview selectors', () => {
  it('counts four levels and deduplicates Apps across bindings', () => {
    expect(scenarioMetrics(fixture())).toEqual({ sceneCount: 1, capabilityCount: 2, subCapabilityCount: 2, appCount: 2 })
  })

  it('summarizes an L1 domain without expanding every descendant', () => {
    expect(scenarioDomains(fixture())[0]).toMatchObject({ capabilityCount: 2, subCapabilityCount: 2 })
    expect(scenarioDomains(fixture())[0]?.capabilities.map(node => node.label)).toEqual(['商品搜索', '订单管理'])
  })

  it('returns a spotlight with visible L2 capabilities and remaining count', () => {
    const domain = scenarioDomains(fixture())[0]
    expect(scenarioSpotlight(domain, 1)).toMatchObject({
      scene: { label: '电商购物' },
      visibleCapabilities: [{ label: '商品搜索' }],
      capabilityCount: 2,
      remainingCapabilityCount: 1,
      subCapabilityCount: 2,
    })
  })

  it('returns empty domain and spotlight results for an empty tree', () => {
    expect(scenarioDomains([])).toEqual([])
    expect(scenarioSpotlight(undefined)).toBeNull()
  })

  it('handles a scene without capabilities', () => {
    const domain = scenarioDomains([{ id: 'scene-empty', kind: 'scene', label: '空场景', children: [] }])[0]
    expect(scenarioSpotlight(domain)).toMatchObject({ capabilityCount: 0, visibleCapabilities: [], remainingCapabilityCount: 0, subCapabilityCount: 0 })
  })

  it('returns stable positions in source order, including more than twelve scenes', () => {
    const domains = scenarioDomains(fixture())
    expect(capabilityTreeLayout(domains)).toEqual(capabilityTreeLayout(domains))
    expect(capabilityTreeLayout(domains)[0]).toMatchObject({ id: 'scene-a', x: 159, y: 170 })
    const manyDomains = Array.from({ length: 13 }, (_, index) => ({ id: `scene-${index}`, kind: 'scene' as const, label: `场景 ${index}`, children: [] }))
    expect(capabilityTreeLayout(scenarioDomains(manyDomains))).toEqual(capabilityTreeLayout(scenarioDomains(manyDomains)))
    expect(capabilityTreeLayout(scenarioDomains(manyDomains)).map(node => node.id)).toEqual(manyDomains.map(node => node.id))
  })

  it('uses balanced sides and grows vertically without shrinking desktop cards', () => {
    const scenes = Array.from({ length: 24 }, (_, i) => ({ id: `s-${i}`, kind: 'scene' as const, label: `场景 ${i}` }))
    const small = capabilityTreeGeometry(scenarioDomains(scenes.slice(0, 5)), 1000)
    const large = capabilityTreeGeometry(scenarioDomains(scenes), 1000)
    expect(large.height).toBeGreaterThan(small.height)
    expect(large.nodeWidth).toBe(small.nodeWidth)
    expect(large.positions.filter(node => node.x < large.center.x)).toHaveLength(12)
    expect(large.positions.filter(node => node.x > large.center.x)).toHaveLength(12)
    for (const node of large.positions) {
      expect(node.x - large.nodeWidth / 2).toBeGreaterThanOrEqual(0)
      expect(node.x + large.nodeWidth / 2).toBeLessThanOrEqual(large.width)
      expect(node.y - 40).toBeGreaterThanOrEqual(0)
      expect(node.y + 40).toBeLessThanOrEqual(large.height)
    }
  })

  it('uses a vertical tree at narrow container widths', () => {
    const scenes = Array.from({ length: 13 }, (_, i) => ({ id: `s-${i}`, kind: 'scene' as const, label: `场景 ${i}` }))
    const layout = capabilityTreeGeometry(scenarioDomains(scenes), 220)
    expect(layout.compact).toBe(true)
    expect(new Set(layout.positions.map(node => node.x)).size).toBe(1)
    expect(layout.positions.map(node => node.id)).toEqual(scenes.map(node => node.id))
    layout.positions.forEach((node, index) => {
      expect(node.x + layout.nodeWidth / 2).toBeLessThanOrEqual(layout.width)
      if (index) expect(node.y - layout.positions[index - 1]!.y).toBeGreaterThanOrEqual(80)
    })
  })

  it('handles empty and temporarily unmeasured containers deterministically', () => {
    expect(capabilityTreeGeometry([]).positions).toEqual([])
    expect(capabilityTreeGeometry([], 0).width).toBeGreaterThan(0)
    expect(capabilityTreeGeometry([], Number.NaN).width).toBe(1000)
  })

  it('limits hover details to three real L2 nodes without changing metrics', () => {
    const scenes = fixture()
    const scene = scenes[0]!
    scene.children = Array.from({ length: 7 }, (_, i) => ({ id: `c-${i}`, kind: 'capability', label: `能力 ${i}`, children: [] }))
    const preview = scenarioSpotlight(scenarioDomains(scenes)[0], 3)
    expect(preview?.visibleCapabilities.map(node => node.id)).toEqual(['c-0', 'c-1', 'c-2'])
    expect(preview?.remainingCapabilityCount).toBe(4)
    expect(preview?.subCapabilityCount).toBe(0)
    expect(scenarioMetrics(scenes).capabilityCount).toBe(7)
  })

  it('uses presentation-only icon hints and a safe fallback for new scenes', () => {
    expect(scenarioIcon('影音视频--音乐')).toBe('music')
    expect(scenarioIcon('影音视频--短视频媒体')).toBe('shortVideo')
    expect(scenarioIcon('打车/导航出行场景')).toBe('travel')
    expect(scenarioIcon('新业务场景')).toBe('tree')
    const tree = fixture(), original = JSON.stringify(tree)
    tree.forEach(node => scenarioIcon(node.label))
    expect(JSON.stringify(tree)).toBe(original)
  })

  it('keeps the expanded node anchored and reserves room without overlapping siblings', () => {
    const scenes = Array.from({ length: 18 }, (_, i) => ({ ...fixture()[0]!, id: `scene-${i}`, children: Array.from({ length: 7 }, (_, j) => ({ id: `c-${i}-${j}`, kind: 'capability' as const, label: '代表性能力' })) }))
    const domains = scenarioDomains(scenes)
    for (const width of [220, 390, 680, 1000, 1400]) {
      const idle = capabilityTreeGeometry(domains, width)
      domains.forEach((domain, activeIndex) => {
        const layout = capabilityTreeGeometry(domains, width, domain.scene.id)
        if (!layout.compact) expect(layout.positions[activeIndex]).toEqual(idle.positions[activeIndex])
        const boxes = layout.positions.map((pos, i) => {
          const height = capabilityNodeHeight(domains[i]!, i === activeIndex)
          const cardWidth = layout.nodeWidth + (!layout.compact && i === activeIndex ? 24 : 0)
          return { left: pos.x - cardWidth / 2, right: pos.x + cardWidth / 2, top: pos.y - height / 2, bottom: pos.y + height / 2 }
        })
        boxes.forEach((box, i) => {
          expect(box.left).toBeGreaterThanOrEqual(0)
          expect(box.right).toBeLessThanOrEqual(layout.width)
          expect(box.top).toBeGreaterThanOrEqual(0)
          expect(box.bottom).toBeLessThanOrEqual(layout.height)
          for (const other of boxes.slice(i + 1)) expect(box.right <= other.left || box.left >= other.right || box.bottom <= other.top || box.top >= other.bottom).toBe(true)
        })
      })
      expect(capabilityTreeGeometry(domains, width, 'removed-id')).toEqual(idle)
    }
  })
})
