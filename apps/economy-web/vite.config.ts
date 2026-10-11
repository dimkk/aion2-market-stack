import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  // Canonical deep links are served by the same private Function. Absolute
  // asset URLs keep /items/<id> reloads from resolving under /items/.../assets.
  base: '/',
  plugins: [react(), tailwindcss()],
  build: {
    sourcemap: false,
    target: 'es2022',
    assetsInlineLimit: 2048,
  },
})
