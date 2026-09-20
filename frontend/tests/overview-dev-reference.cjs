// Render the checked-in DEV SFCs unchanged. Only transport and chart observation
// are adapted, so the production dashboard can be compared with one fixture.
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const { pathToFileURL } = require('node:url')

exports.buildReference = async function buildReference() {
  const frontend = path.resolve(__dirname, '..')
  const reference = path.resolve(frontend, '../黄区平台前后端/DataVue/DataVue/frontend/src')
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'adf-dev-overview-'))
  const cleanup = async () => {
    if (path.dirname(temporary) !== path.resolve(os.tmpdir())) throw new Error('Unsafe temporary reference cleanup')
    await fs.rm(temporary, { recursive: true, force: true })
  }
  const source = value => JSON.stringify(value.replaceAll('\\', '/'))
  const vite = await import(pathToFileURL(require.resolve('vite')).href)
  const vue = (await import(pathToFileURL(require.resolve('@vitejs/plugin-vue')).href)).default
  const echarts = path.join(frontend, 'node_modules/echarts/index.js')
  await fs.writeFile(path.join(temporary, 'index.html'), '<div id="app"></div><script type="module" src="/main.js"></script>')
  await fs.writeFile(path.join(temporary, 'main.js'), `
    import { createApp } from 'vue'; import { createRouter, createWebHistory } from 'vue-router';
    import SceneView from ${source(path.join(reference, 'views/SceneView.vue'))};
    import AppView from ${source(path.join(reference, 'views/AppView.vue'))};
    import ${source(path.join(reference, 'style.css'))};
    const router = createRouter({history:createWebHistory(),routes:[{path:'/',component:SceneView},{path:'/app-view',component:AppView}]});
    createApp({template:'<router-view />'}).use(router).mount('#app');
  `)
  await fs.writeFile(path.join(temporary, 'api.js'), `
    export function useDataApi() {
      const get = async (name, params={}) => (await fetch('/api/'+name+'?'+new URLSearchParams(params))).json();
      return Object.fromEntries(Object.entries({fetchSources:'sources',fetchDateRange:'date_range',fetchScenes:'scenes',fetchApps:'apps',fetchData:'data',fetchSceneStats:'scene_stats',fetchDistributions:'distributions'}).map(([key,name])=>[key,params=>get(name,params)]));
    }
  `)
  await fs.writeFile(path.join(temporary, 'charts.js'), `
    import * as real from ${source(echarts)};
    export function init(host, ...args) {
      const chart=real.init(host,...args), original=chart.setOption.bind(chart);
      chart.setOption=(option,...rest)=>{
        const hosts=[...host.closest('.scene-view,.app-view').querySelectorAll('.chart-container,.pie-chart')];
        (window.__devOptions??=[])[hosts.indexOf(host)]=JSON.parse(JSON.stringify(option));
        return original(option,...rest);
      }; return chart;
    }
  `)
  try {
    await vite.build({ configFile: false, root: temporary, logLevel: 'error', plugins: [vue()], resolve: { alias: [
      { find: '@/composables/useDataApi', replacement: path.join(temporary, 'api.js') },
      { find: /^echarts$/, replacement: path.join(temporary, 'charts.js') },
      { find: /^vue$/, replacement: path.join(frontend, 'node_modules/vue/dist/vue.esm-bundler.js') },
      { find: /^vue-router$/, replacement: path.join(frontend, 'node_modules/vue-router/dist/vue-router.mjs') },
      { find: '@vue', replacement: path.join(frontend, 'node_modules/@vue') },
    ] }, build: { outDir: path.join(temporary, 'dist'), emptyOutDir: true } })
    return { directory: path.join(temporary, 'dist'), cleanup }
  } catch (error) { await cleanup(); throw error }
}
