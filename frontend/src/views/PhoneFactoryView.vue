<template>
  <div class="page phone-factory-view">
    <header class="page-hero"><div><span class="eyebrow">PHONE FACTORY</span><h1>手机工厂监控</h1><p>查看设备连接、运行状态、实时画面与采集日志。</p></div><router-link to="/collection/phone-factory">进入手机工厂采集</router-link></header>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
    <section class="factory-overview">
      <div class="overview-card"><span>已登记设备</span><strong>{{ registeredCount }}</strong></div>
      <div class="overview-card"><span>在线设备</span><strong>{{ connectedCount }}</strong></div>
      <div class="overview-card"><span>运行中设备</span><strong>{{ runningCount }}</strong></div>
    </section>
    <div class="toolbar"><span>{{ updatedAt ? `最近更新：${updatedAt}` : '正在读取设备状态' }}</span><el-button :loading="loading" @click="poller.start()">刷新</el-button></div>
    <section class="device-grid" data-testid="factory-device-grid">
      <button v-for="device in devices" :key="device.id" class="device-card" @click="selectDevice(device.id)">
        <div><strong>{{ device.model || '未知型号' }}</strong><el-tag :type="device.connected ? 'success' : 'info'">{{ device.connected ? '已连接' : '离线' }}</el-tag></div>
        <p>{{ device.id }}</p><div><span>{{ device.status }}</span><span>电量 {{ device.battery == null ? '—' : `${device.battery}%` }}</span></div>
        <small>查看画面与日志</small>
      </button>
    </section>
    <el-empty v-if="!loading && !devices.length" description="暂无手机设备，请先在手机工厂采集中新增" />
    <el-dialog :model-value="Boolean(selectedId)" :title="`手机监控 · ${selectedId}`" width="min(1100px, 95vw)" destroy-on-close @close="closeMonitor">
      <el-alert v-if="monitorError" :title="monitorError" type="warning" :closable="false" />
      <div class="monitor-layout">
        <div class="screen"><img v-if="monitor?.screenshot" :src="`data:image/png;base64,${monitor.screenshot}`" alt="手机实时画面" data-testid="phone-live-screen" /><p v-else>{{ monitorLoading ? '正在读取画面…' : '暂未取得设备画面' }}</p><small v-if="monitor?.device_size">{{ monitor.device_size.width }} × {{ monitor.device_size.height }}</small></div>
        <div class="log-panel"><div class="log-heading"><strong>采集日志</strong><span>{{ monitor ? (monitor.running ? '运行中' : '空闲') : '状态待查询' }}</span></div><pre data-testid="phone-live-log">{{ monitor?.log || '暂无日志' }}</pre></div>
      </div>
    </el-dialog>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { phoneFactoryApi, type PhoneMonitor } from '@/phoneFactoryApi'
import { useFactoryPolling } from '@/composables/useFactoryPolling'
onMounted(() => document.body.classList.add('factory-responsive'))
onBeforeUnmount(() => document.body.classList.remove('factory-responsive'))
interface Device { id: string; model: string; battery: number | null; connected: boolean; registered: boolean; status: string }
const devices = ref<Device[]>([]), error = ref(''), loading = ref(true), updatedAt = ref('')
const selectedId = ref(''), monitor = ref<PhoneMonitor | null>(null), monitorError = ref(''), monitorLoading = ref(false)
const registeredCount = computed(() => devices.value.filter(item => item.registered).length)
const connectedCount = computed(() => devices.value.filter(item => item.connected).length)
const runningCount = computed(() => devices.value.filter(item => item.status === '运行中').length)
const poller = useFactoryPolling(async (signal, current) => {
  loading.value = true
  try {
    const state = await phoneFactoryApi.state(signal)
    const adb = await phoneFactoryApi.adbDevices(signal)
    const ids = [...new Set([...state.phones, ...state.phoneApps.map(item => item.phone_id), ...adb.devices.map(item => item.serial)])]
    const statuses = await phoneFactoryApi.remoteStatus(ids, signal)
    if (!current()) return
    devices.value = ids.map(id => {
      const connected = adb.devices.find(item => item.serial === id)
      return { id, model: connected?.model || '', battery: connected?.battery ?? null, connected: Boolean(connected), registered: state.phones.includes(id), status: statuses.statuses.find(item => item.phone_id === id)?.status || '未知' }
    })
    error.value = ''; updatedAt.value = new Date().toLocaleTimeString()
    if (selectedId.value && !ids.includes(selectedId.value)) { selectedId.value = ''; monitor.value = null }
  } catch (cause) { if (current()) { error.value = `设备状态读取失败：${(cause as Error).message}`; devices.value = devices.value.map(item => ({ ...item, status: '未知' })) } }
  finally { if (current()) loading.value = false }
  if (!current() || !selectedId.value) return
  const id = selectedId.value
  monitorLoading.value = true
  try {
    const result = await phoneFactoryApi.monitor(id, signal)
    if (current() && selectedId.value === id) { monitor.value = result; monitorError.value = '' }
  } catch (cause) { if (current() && selectedId.value === id) { monitorError.value = `监控读取失败：${(cause as Error).message}`; monitor.value = null } }
  finally { if (current() && selectedId.value === id) monitorLoading.value = false }
}, 3000)
function selectDevice(id: string) { selectedId.value = id; monitor.value = null; monitorError.value = ''; monitorLoading.value = true; poller.start() }
function closeMonitor() { selectedId.value = ''; monitor.value = null; monitorError.value = ''; monitorLoading.value = false; poller.start() }
</script>
<style scoped>
:global(body.factory-responsive) { min-width: 0; }
.phone-factory-view { min-height:100vh; }.factory-overview { display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0; }.overview-card { display:grid;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px; }.overview-card span,.toolbar { color:var(--muted);font-size:13px; }.overview-card strong { font-size:28px;color:var(--accent-deep); }.toolbar { display:flex;justify-content:space-between;align-items:center;margin-bottom:18px; }.device-grid { display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px; }.device-card { padding:20px;border:1px solid var(--line);border-radius:14px;background:var(--panel);text-align:left;color:var(--ink);cursor:pointer; }.device-card:hover { border-color:var(--accent); }.device-card div { display:flex;justify-content:space-between;gap:12px;align-items:center; }.device-card p { overflow-wrap:anywhere; }.device-card small { display:block;color:var(--accent-deep);margin-top:16px; }.monitor-layout { display:grid;grid-template-columns:minmax(240px,1fr) 2fr;gap:20px;min-height:360px; }.screen { display:grid;justify-items:center;align-content:start;background:#f1f5f9;border-radius:12px;padding:12px; }.screen img { max-width:100%;max-height:65vh;object-fit:contain; }.screen small { margin-top:10px;color:var(--muted); }.log-panel { min-width:0; }.log-heading { display:flex;justify-content:space-between; }.log-panel pre { white-space:pre-wrap;overflow-wrap:anywhere;height:65vh;overflow:auto;background:#0f172a;color:#e2e8f0;padding:16px;border-radius:10px;font-size:12px;line-height:1.6; }@media(max-width:760px) { .factory-overview,.monitor-layout { grid-template-columns:1fr; } }
</style>
