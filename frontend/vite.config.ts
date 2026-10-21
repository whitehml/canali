import { defineConfig, loadEnv } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig(({ mode }) => ({
  plugins: [svelte()],
  // Hosted builds contain only application files; local demos serve generated data too.
  publicDir: loadEnv(mode, process.cwd(), "VITE_").VITE_PUBLICATION_BASE_URL ? false : "public",
}));
