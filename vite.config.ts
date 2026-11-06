import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const runtimeProcess = (globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } }).process
const apiPort = Number(runtimeProcess?.env?.DEXTER_PORT ?? '8000')

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
  preview: { host: '127.0.0.1' },
})
