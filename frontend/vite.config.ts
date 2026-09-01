import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { phoneFactoryDevServer } from './vite-plugin-phone-factory'

export default defineConfig({
  plugins: [vue(), phoneFactoryDevServer()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
