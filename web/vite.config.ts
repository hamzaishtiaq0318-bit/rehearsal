import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `vite build` makes the console the Rehearsal server serves.
// `vite build --mode static` makes the website's browser demo at /try, which replays recorded runs.
export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    // The local console stays out of search engines; the website's browser demo is a public page.
    mode === "static" && {
      name: "public-demo-html",
      transformIndexHtml: (html: string) => html.replace(/\s*<meta name="robots" content="noindex" \/>/, ""),
    },
  ],
  base: mode === "static" ? "/try/" : "/",
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: {
    outDir: mode === "static" ? "../site/try" : "dist",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
  },
}));
