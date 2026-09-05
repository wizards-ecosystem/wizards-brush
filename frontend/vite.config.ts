/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy API, static files, and the WebSocket to the FastAPI backend on :8000.
export default defineConfig({
  plugins: [react()],
  cacheDir: "../.runtime/cache/vite",
  server: {
    host: "127.0.0.1",
    port: 5173,
    headers: {
      "Content-Security-Policy": "frame-ancestors 'none'",
      "X-Frame-Options": "DENY",
    },
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true, ws: true },
      "/files": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});
