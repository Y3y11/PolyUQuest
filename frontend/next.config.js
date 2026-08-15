/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Required by the pinned Next.js 14 runtime to load instrumentation.ts.
  experimental: { instrumentationHook: true },
};

module.exports = nextConfig;
