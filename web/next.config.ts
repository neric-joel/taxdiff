import type { NextConfig } from "next";

// Local runs only: the FastAPI server's origin (uvicorn on port 8000 under `next dev`). On Vercel the
// project's rewrites send /api/* to the Python service before a request reaches Next.js, so none is set.
const api = process.env.TAXDIFF_API_ORIGIN ?? (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : undefined);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  devIndicators: false, // the dev-only badge covers the run history
  async rewrites() {
    return api ? [{ source: "/api/:path*", destination: `${api}/api/:path*` }] : [];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
