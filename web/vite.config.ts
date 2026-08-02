import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into web/dist/, which is committed and served by FastAPI so that
// `haat-lister serve` works on a machine with no Node at all.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, assetsDir: "assets" },
  server: {
    port: 5173,
    // `haat-lister serve --dev` allows exactly this origin, and nothing else.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
