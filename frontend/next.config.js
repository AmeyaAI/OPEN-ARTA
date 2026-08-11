/** @type {import('next').NextConfig} */
const nextConfig = {
  // F4-2: standalone output ships a self-contained server.js + node_modules
  // subset, so the runtime Docker image doesn't need npm or .next/cache.
  output: 'standalone',
  // R28.2 — production assetPrefix so dynamic chunks resolve at the
  // because Next.js had no manifest entry for the deployed asset host.
  // Operators on localhost don't need to set ARTA_FRONTEND_ASSET_PREFIX;
  // when unset, Next.js falls back to relative chunk URLs (the default
  // dev/local-prod behavior).
  assetPrefix: process.env.ARTA_FRONTEND_ASSET_PREFIX || undefined,
  // F8-14: ignoreBuildErrors removed — F7-3 fixed the 26 cascading TS errors
  // that originally needed this escape hatch. Letting `next build` enforce
  // types prevents silent regressions; the F4-7 `npx tsc --noEmit` CI gate
  // was redundant safety, not a substitute for build-time checking.
  eslint: { ignoreDuringBuilds: true },
  // Increase proxy timeout for LLM-heavy endpoints (test generation, discovery)
  // Default is 30s which is too short for Ollama with complex prompts
  experimental: {
    proxyTimeout: 120000, // 2 minutes
  },
  // Consolidation: /quality-score merged into the single canonical SUT Quality
  // page (its SUT-regression panel + test-quality/credibility content moved to
  // /sut-quality). 308-redirect so existing deep links / bookmarks don't 404.
  async redirects() {
    return [
      { source: '/quality-score', destination: '/sut-quality', permanent: true },
      // WS5: /diff-view removed (orphaned/redundant — test-explorer diffs versions
      // inline). Redirect any bookmark to the canonical version-history surface.
      { source: '/diff-view', destination: '/test-explorer', permanent: true },
      // R306.E: /exploratory deprovisioned — a manual SBTM prototype that was
      // broken in DB mode (datetime-500), unscoped to project, and disconnected
      // from every ARTA pillar (no mission-report / traceability / test-gen). ARTA
      // is autonomous; there's no human-tester loop for these SUTs. Redirect any
      // bookmark to run-history. (Backend router unregistered in api/main.py.)
      { source: '/exploratory', destination: '/run-history', permanent: true },
    ]
  },
  // Proxy API requests through the Next.js server so the browser
  // only needs to reach port 3001 (works with VS Code Remote SSH,
  // SSH tunnels, and any single-port forwarding setup).
  async rewrites() {
    const apiUrl = process.env.ARTA_API_INTERNAL_URL || 'http://arta-api:8000'
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
      {
        source: '/health',
        destination: `${apiUrl}/health`,
      },
      {
        source: '/docs',
        destination: `${apiUrl}/docs`,
      },
      {
        source: '/openapi.json',
        destination: `${apiUrl}/openapi.json`,
      },
      {
        source: '/artifacts/:path*',
        destination: `${apiUrl}/artifacts/:path*`,
      },
    ]
  },
}

module.exports = nextConfig
