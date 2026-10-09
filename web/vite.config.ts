import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 开发时把 /api 转发到后端容器（compose 内部网络中的 api 服务）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.API_UPSTREAM ?? "http://api:8000", changeOrigin: true, ws: true } },
  },
  build: {
    chunkSizeWarningLimit: 2500,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ["echarts", "echarts-gl"],
          three: ["three", "@react-three/fiber", "@react-three/drei", "@react-three/postprocessing"],
        },
      },
    },
  },
});
