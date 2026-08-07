/** @type {import('next').NextConfig} */
const nextConfig = {
    reactStrictMode: true,
    // Ship a self-contained server bundle so the production Docker
    // image only needs the standalone output + .next/static, not the
    // full node_modules tree. Cuts the runtime image significantly.
    output: "standalone",
    // Image optimization defaults — modern formats, lazy-loaded by
    // default. The customer favicon path goes through the backend
    // proxy (api/contacts.py:proxy_favicon), so no remote patterns
    // beyond the backend proxy itself are needed.
    images: {
        formats: ["image/avif", "image/webp"],
    },
    // Long, immutable cache for the build artifacts so the browser
    // doesn't re-download chunks on every visit. Build hashes change
    // per release so this is safe.
    async headers() {
        return [
            {
                source: "/_next/static/:path*",
                headers: [
                    {
                        key: "Cache-Control",
                        value: "public, max-age=31536000, immutable",
                    },
                ],
            },
            {
                // The embeddable live-call widget is meant to be
                // framed by customer origins (dialers/CRMs embedding
                // /embed/live/[sessionId] in an iframe). Every other
                // route keeps the platform default (no explicit
                // frame-ancestors / X-Frame-Options set here).
                source: "/embed/:path*",
                headers: [
                    {
                        key: "Content-Security-Policy",
                        value: "frame-ancestors *",
                    },
                ],
            },
        ];
    },
    async rewrites() {
        // Proxy the SPA's /api/* calls to the FastAPI backend.
        const backend = process.env.LINDA_BACKEND_URL || "http://localhost:8000";
        return [
            { source: "/api/:path*", destination: `${backend}/api/:path*` },
        ];
    },
};

export default nextConfig;
