import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // Two pages: the existing app (index.html, unchanged) and ReviewWorkspaceV2 (v2.html).
    // Paths are relative to the project root (the build runs from frontend/).
    rollupOptions: {
      input: {
        main: "index.html",
        v2: "v2.html",
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
