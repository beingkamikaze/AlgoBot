import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",          // the server image (infra/web.Dockerfile) runs .next/standalone/server.js
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
