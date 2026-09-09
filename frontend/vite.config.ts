import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API-сервер по умолчанию — http://localhost:8001 (локальный dev).
const API_TARGET = process.env.VITE_API_TARGET || "http://localhost:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 3000,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
      "/media": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});