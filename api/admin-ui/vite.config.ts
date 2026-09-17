import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `base` is relative so the bundle works wherever the app is mounted: the API
// can be served under a rootPath prefix, and absolute asset URLs would 404
// there while working perfectly in every local test.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  test: { environment: "node" },
});
