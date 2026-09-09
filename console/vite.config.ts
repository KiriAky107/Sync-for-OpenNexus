import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/console/',
  plugins: [vue()],
  build: {
    outDir: '../sync_server/static',
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0,
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/health': 'http://127.0.0.1:18081',
      '/ready': 'http://127.0.0.1:18081',
      '/sync': 'http://127.0.0.1:18081',
    },
  },
})
