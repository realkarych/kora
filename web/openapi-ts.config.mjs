import { defineConfig } from '@hey-api/openapi-ts'

export default defineConfig({
  input: './openapi/run-events.openapi.json',
  output: './src/api/generated/run-events',
  parser: {
    filters: {
      operations: { include: ['GET /api/v1/namespaces/{ns}/runs/{run}/events'] },
    },
  },
  plugins: ['@hey-api/typescript', '@hey-api/sdk', '@hey-api/client-fetch'],
})
