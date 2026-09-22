<template>
  <div class="page phone-factory-view">
    <header class="page-hero"><div><span class="eyebrow">PHONE FACTORY</span><h1>手机工厂监控</h1><p>查看设备连接、运行状态、实时画面与采集日志。</p></div><router-link to="/collection/phone-factory">进入手机工厂采集</router-link></header>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
    <section class="factory-overview">
      <div class="overview-card"><span>已连接手机</span><strong data-testid="factory-connected-count">{{ connectedCount }}</strong></div>
      <div class="overview-card"><span>全部手机</span><strong data-testid="factory-phone-count">{{ devices.length }}</strong></div>
      <div class="overview-card"><span>采集任务</span><strong data-testid="factory-task-count">{{ taskCount }}</strong></div>
    </section>
    <div class="toolbar"><span>{{ updatedAt ? `最近更新：${updatedAt} · 设备状态每 10 秒刷新` : '正在读取设备状态' }}</span><el-button :loading="loading" data-testid="factory-devices-refresh" @click="refreshDevices">刷新</el-button></div>
    <section class="device-grid" data-testid="factory-device-grid">
      <button v-for="(device, index) in devices" :key="device.id" class="device-card" :data-device-id="device.id" @click="selectDevice(device.id)">
        <div class="device-heading"><strong>#{{ String(index + 1).padStart(3, '0') }} · {{ device.model || '未知型号' }}</strong><el-tag :type="device.connected ? 'success' : 'info'">{{ device.connected ? '已连接' : '离线' }}</el-tag></div>
        <dl class="device-info">
          <dt>序列号</dt><dd>{{ device.id }}</dd><dt>资产编号</dt><dd>{{ device.id }}</dd>
          <dt>手机型号</dt><dd>{{ device.model || '—' }}</dd><dt>运行 App</dt><dd>{{ device.apps.join('、') || '—' }}</dd>
          <dt>电量</dt><dd>{{ device.battery == null ? '—' : `${device.battery}%` }}</dd><dt>状态</dt><dd>{{ device.status }}</dd>
        </dl>
        <small>查看画面与日志</small>
      </button>
    </section>
    <el-empty v-if="!loading && !devices.length" description="暂无手机设备，请先在手机工厂采集中新增" />
    <el-dialog :model-value="Boolean(selectedId)" :title="`手机监控 · ${selectedId}`" width="min(1100px, 95vw)" destroy-on-close @close="closeMonitor">
      <template v-if="selectedDevice">
        <div class="monitor-heading">
          <el-tag :type="selectedDevice.connected ? 'success' : 'info'">{{ selectedDevice.connected ? '已连接' : '离线' }}</el-tag>
          <el-tag :type="monitor?.running ? 'warning' : 'info'">{{ monitor ? (monitor.running ? '运行中' : '空闲') : '状态待查询' }}</el-tag>
          <span>运行 App：{{ selectedDevice.apps.join('、') || '—' }}</span>
          <el-button :loading="monitorLoading" data-testid="factory-monitor-refresh" @click="refreshMonitor">刷新</el-button>
        </div>
        <el-alert v-if="monitorError" :title="monitorError" type="warning" :closable="false" />
        <div class="monitor-layout">
          <div class="screen"><img v-if="monitor?.screenshot" :src="`data:image/png;base64,${monitor.screenshot}`" alt="手机实时画面" data-testid="phone-live-screen" /><p v-else>{{ monitorLoading ? '正在读取画面…' : '暂未取得设备画面' }}</p><small v-if="monitor?.device_size">{{ monitor.device_size.width }} × {{ monitor.device_size.height }}</small><small>画面与日志约每 1 秒刷新</small></div>
          <div class="log-panel"><div class="log-heading"><strong>采集日志</strong><span>{{ monitor ? (monitor.running ? '运行中' : '空闲') : '状态待查询' }}</span></div><pre ref="logElement" data-testid="phone-live-log">{{ logText }}</pre></div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, onMounted, onBeforeUnmount } from 'vue'
import { phoneFactoryApi, type PhoneMonitor } from '@/phoneFactoryApi'
import { createSerialPoller, useFactoryPolling } from '@/composables/useFactoryPolling'

interface Device { id: string; apps: string[]; model: string; battery: number | null; connected: boolean; status: string }
const devices = ref<Device[]>([]), taskCount = ref(0), error = ref(''), loading = ref(true), updatedAt = ref('')
const selectedId = ref(''), monitor = ref<PhoneMonitor | null>(null), monitorError = ref(''), monitorLoading = ref(false)
const logElement = ref<HTMLPreElement | null>(null)
const selectedDevice = computed(() => devices.value.find(device => device.id === selectedId.value))
const connectedCount = computed(() => devices.value.filter(item => item.connected).length)
const logText = computed(() => monitor.value?.log || (monitor.value ? (monitor.value.running ? '采集中，暂无日志输出' : '暂无采集日志') : monitorError.value || '正在加载采集日志…'))

const devicePoller = useFactoryPolling(async (signal, current) => {
  loading.value = true
  try {
    const state = await phoneFactoryApi.state(signal)
    if (!current()) return
    const adb = await phoneFactoryApi.adbDevices(signal)
    if (!current()) return
    const ids = [...new Set([...state.phones, ...state.phoneApps.map(item => item.phone_id), ...adb.devices.map(item => item.serial)])]
    const statuses = await phoneFactoryApi.remoteStatus(ids, signal)
    if (!current()) return
    devices.value = ids.map(id => {
      const connected = adb.devices.find(item => item.serial === id)
      const apps = [...new Set(state.phoneApps.filter(item => item.phone_id === id).map(item => item.app))]
      return { id, apps, model: connected?.model || '', battery: connected?.battery ?? null, connected: Boolean(connected), status: statuses.statuses.find(item => item.phone_id === id)?.status || '未知' }
    })
    taskCount.value = state.tasks.length
    error.value = ''; updatedAt.value = new Date().toLocaleTimeString()
    if (selectedId.value && !ids.includes(selectedId.value)) closeMonitor()
  } catch (cause) {
    if (current()) { error.value = `设备状态读取失败：${(cause as Error).message}`; devices.value = devices.value.map(item => ({ ...item, status: '未知' })) }
  } finally { if (current()) loading.value = false }
}, 10000)

const monitorPoller = createSerialPoller(async (signal, current) => {
  const id = selectedId.value
  if (!id || document.hidden) return
  monitorLoading.value = true
  try {
    const result = await phoneFactoryApi.monitor(id, signal)
    if (!current() || selectedId.value !== id) return
    const previousLog = monitor.value?.log
    monitor.value = result; monitorError.value = ''
    if (previousLog !== result.log) {
      await nextTick()
      if (current() && selectedId.value === id && logElement.value) logElement.value.scrollTop = logElement.value.scrollHeight
    }
  } catch (cause) {
    if (current() && selectedId.value === id) { monitorError.value = `监控读取失败：${(cause as Error).message}`; monitor.value = null }
  } finally { if (current() && selectedId.value === id) monitorLoading.value = false }
}, 1000)

function refreshDevices() { if (!document.hidden) devicePoller.start() }
function refreshMonitor() { if (selectedId.value && !document.hidden) monitorPoller.start() }
function selectDevice(id: string) {
  if (!devices.value.some(device => device.id === id)) return
  selectedId.value = id; monitor.value = null; monitorError.value = ''; monitorLoading.value = true
  refreshMonitor()
}
function closeMonitor() {
  monitorPoller.stop()
  selectedId.value = ''; monitor.value = null; monitorError.value = ''; monitorLoading.value = false
}
function handleVisibility() {
  if (document.hidden) { monitorPoller.stop(); monitorLoading.value = false }
  else refreshMonitor()
}
onMounted(() => {
  document.body.classList.add('factory-responsive')
  document.addEventListener('visibilitychange', handleVisibility)
  window.addEventListener('focus', handleVisibility)
})
onBeforeUnmount(() => {
  monitorPoller.dispose()
  document.removeEventListener('visibilitychange', handleVisibility)
  window.removeEventListener('focus', handleVisibility)
  document.body.classList.remove('factory-responsive')
})
</script>
<style scoped>
:global(body.factory-responsive) { min-width: 0; }
.phone-factory-view { min-height:100vh; }.factory-overview { display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0; }.overview-card { display:grid;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px; }.overview-card span,.toolbar { color:var(--muted);font-size:13px; }.overview-card strong { font-size:28px;color:var(--accent-deep); }.toolbar { display:flex;justify-content:space-between;align-items:center;margin-bottom:18px; }.device-grid { display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px; }.device-card { padding:20px;border:1px solid var(--line);border-radius:14px;background:var(--panel);text-align:left;color:var(--ink);cursor:pointer; }.device-card:hover { border-color:var(--accent); }.device-heading { display:flex;justify-content:space-between;gap:12px;align-items:center; }.device-info { display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px 12px;font-size:13px;line-height:1.5; }.device-info dt { color:var(--muted); }.device-info dd { margin:0;overflow-wrap:anywhere; }.device-card small { display:block;color:var(--accent-deep);margin-top:16px; }.monitor-heading { display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px; }.monitor-heading>span { color:var(--muted);font-size:13px;overflow-wrap:anywhere; }.monitor-heading>.el-button { margin-left:auto; }.monitor-layout { display:grid;grid-template-columns:minmax(240px,1fr) 2fr;gap:20px;min-height:360px; }.screen { display:grid;justify-items:center;align-content:start;background:#f1f5f9;border-radius:12px;padding:12px; }.screen img { max-width:100%;max-height:65vh;object-fit:contain; }.screen small { margin-top:10px;color:var(--muted); }.log-panel { min-width:0; }.log-heading { display:flex;justify-content:space-between; }.log-panel pre { white-space:pre-wrap;overflow-wrap:anywhere;height:65vh;overflow:auto;background:#0f172a;color:#e2e8f0;padding:16px;border-radius:10px;font-size:12px;line-height:1.6; }@media(max-width:760px) { .factory-overview,.monitor-layout { grid-template-columns:1fr; } }
</style>
