import type { TaskGenerationTreeNode } from '@/types'
import { appNodes } from './scenarioTree'

export interface ScenarioMetrics {
  sceneCount: number
  capabilityCount: number
  subCapabilityCount: number
  appCount: number
}

export interface ScenarioDomainSummary {
  scene: TaskGenerationTreeNode
  capabilities: TaskGenerationTreeNode[]
  capabilityCount: number
  subCapabilityCount: number
}

export interface ScenarioSpotlightSummary {
  scene: TaskGenerationTreeNode
  capabilities: TaskGenerationTreeNode[]
  visibleCapabilities: TaskGenerationTreeNode[]
  capabilityCount: number
  remainingCapabilityCount: number
  subCapabilityCount: number
}

export interface CapabilityTreePosition {
  id: string
  x: number
  y: number
}

export interface CapabilityTreeGeometry {
  width: number
  height: number
  compact: boolean
  nodeWidth: number
  center: { x: number; y: number }
  positions: CapabilityTreePosition[]
}

export function scenarioMetrics(nodes: TaskGenerationTreeNode[]): ScenarioMetrics {
  const metrics: ScenarioMetrics = { sceneCount: 0, capabilityCount: 0, subCapabilityCount: 0, appCount: 0 }
  const apps = new Set<string>()
  const visit = (items: TaskGenerationTreeNode[]) => items.forEach(node => {
    if (node.kind === 'scene') metrics.sceneCount += 1
    if (node.kind === 'capability') metrics.capabilityCount += 1
    if (node.kind === 'sub_capability') {
      metrics.subCapabilityCount += 1
      appNodes(node).forEach(app => {
        const identifier = (app.app || app.label).trim()
        if (identifier) apps.add(identifier)
      })
    }
    if (node.kind === 'app') {
      const identifier = (node.app || node.label).trim()
      if (identifier) apps.add(identifier)
    }
    visit(node.children || [])
  })
  visit(nodes)
  metrics.appCount = apps.size
  return metrics
}

export function scenarioDomains(nodes: TaskGenerationTreeNode[]): ScenarioDomainSummary[] {
  return nodes.filter(node => node.kind === 'scene').map(scene => {
    const capabilities = (scene.children || []).filter(node => node.kind === 'capability')
    const subCapabilityCount = capabilities.reduce((count, capability) => count + (capability.children || []).filter(node => node.kind === 'sub_capability').length, 0)
    return { scene, capabilities, capabilityCount: capabilities.length, subCapabilityCount }
  })
}

export function scenarioSpotlight(domain: ScenarioDomainSummary | undefined, limit = 6): ScenarioSpotlightSummary | null {
  if (!domain) return null
  const safeLimit = Math.max(1, limit)
  return {
    scene: domain.scene,
    capabilities: domain.capabilities,
    visibleCapabilities: domain.capabilities.slice(0, safeLimit),
    capabilityCount: domain.capabilityCount,
    remainingCapabilityCount: Math.max(0, domain.capabilityCount - safeLimit),
    subCapabilityCount: domain.subCapabilityCount,
  }
}

// Coordinates are CSS pixels, so more nodes grow the canvas rather than shrink text.
// Alternating left/right placement preserves source order without creating categories.
export function capabilityNodeHeight(domain: ScenarioDomainSummary, expanded = false): number {
  return expanded ? 160 + Math.min(domain.capabilityCount, 3) * 14 + (domain.capabilityCount > 3 ? 14 : 0) : 88
}

// Presentation hints only: neither icons nor display ordinals enter saved tree data.
export function scenarioIcon(label: string) {
  if (/短视频/.test(label)) return 'shortVideo'
  if (/视频|影音|电影/.test(label) && !/音乐/.test(label)) return 'video'
  if (/音乐|音频|播客/.test(label)) return 'music'
  if (/购物|买卖|电商/.test(label)) return 'shopping'
  if (/安装|运维|设置/.test(label)) return 'tools'
  if (/手游|游戏/.test(label)) return 'game'
  if (/运营商|通信/.test(label)) return 'network'
  if (/社交|分享/.test(label)) return 'social'
  if (/新闻|阅读/.test(label)) return 'reading'
  if (/票务|交通/.test(label)) return 'ticket'
  if (/出行|打车|导航/.test(label)) return 'travel'
  return 'tree'
}

export function capabilityTreeGeometry(domains: ScenarioDomainSummary[], availableWidth = 1000, expandedId = ''): CapabilityTreeGeometry {
  const width = Number.isFinite(availableWidth) ? Math.max(1, availableWidth) : 1000
  const compact = width < 680
  const nodeWidth = compact ? Math.max(1, width - 42) : Math.min(258, width * .28)
  const rows = Math.ceil(domains.length / 2)
  const expandedIndex = domains.findIndex(domain => domain.scene.id === expandedId)
  const extra = expandedIndex < 0 ? 0 : capabilityNodeHeight(domains[expandedIndex]!, true) - 88
  const height = compact ? Math.max(220, 108 + domains.length * 108 + extra) : Math.max(340, rows * 96 + 140)
  const center = { x: width / 2, y: compact ? 43 : height / 2 }
  const positions = domains.map((domain, index) => {
    if (compact) return { id: domain.scene.id, x: 34 + nodeWidth / 2, y: 150 + index * 108 + (expandedIndex >= 0 && index >= expandedIndex ? index === expandedIndex ? extra / 2 : extra : 0) }
    const left = index % 2 === 0
    const countOnSide = left ? rows : Math.floor(domains.length / 2)
    return {
      id: domain.scene.id,
      x: left ? 30 + nodeWidth / 2 : width - 30 - nodeWidth / 2,
      // Keep the hovered card anchored; siblings on its side make room for its details.
      y: height / 2 + (Math.floor(index / 2) - (countOnSide - 1) / 2) * 96
        + (expandedIndex >= 0 && index % 2 === expandedIndex % 2 ? Math.sign(index - expandedIndex) * extra / 2 : 0),
    }
  })
  return { width, height, compact, nodeWidth, center, positions }
}

export function capabilityTreeLayout(domains: ScenarioDomainSummary[]): CapabilityTreePosition[] {
  return capabilityTreeGeometry(domains).positions
}
