import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8000 and Vite proxies /api to it, so the browser sees one origin
// (matching production, where the API serves the built client and CSP is default-src 'self').
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
});
