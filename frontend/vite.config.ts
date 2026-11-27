import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, '.', 'GPU_CONSOLE_').GPU_CONSOLE_DEV_API
  return {
    plugins: [
      vue(),
      AutoImport({ resolvers: [ElementPlusResolver()], dts: false }),
      Components({ dirs: [], resolvers: [ElementPlusResolver()], dts: false }),
    ],
    server: {
      proxy: backend ? { '/api': backend } : undefined,
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes('/node_modules/zrender/')) return 'chart-renderer'
          },
        },
      },
    },
  }
})
