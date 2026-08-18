// Explicit App Router 404 — without this, Next prerenders /404 through the
// pages-router fallback `_error`, which tripped "<Html> should not be
// imported" under Next 15 in this build environment. Also just a better 404.
export default function NotFound() {
  return (
    <div style={{
      minHeight: '100vh', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center', gap: '0.75rem',
      background: '#0a0a14', color: '#e2e8f0',
    }}>
      <div style={{ fontSize: '2rem', fontWeight: 700 }}>404</div>
      <div style={{ color: '#94a3b8' }}>This page could not be found.</div>
      <a href="/" style={{ color: '#818cf8', fontSize: '0.9rem' }}>← Back to dashboard</a>
    </div>
  )
}
