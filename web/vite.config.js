import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const health = () => ({
  name: 'health',
  configureServer(server) {
    server.middlewares.use('/health', (_req, res) => {
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify({ service: 'web', status: 'ok' }))
    })
  },
})

export default defineConfig({
  plugins: [react(), health()],
  test: { include: ['tests/**/*.test.jsx'] },
})
