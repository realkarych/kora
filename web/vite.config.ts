import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/kora/api/static',
    emptyOutDir: true,
    manifest: 'console-manifest.json',
    license: { fileName: 'THIRD_PARTY_LICENSES.md' },
  },
})
