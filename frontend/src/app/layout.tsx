import type { Metadata } from 'next'
import './globals.css'
import AppShell from '@/components/AppShell'

export const metadata: Metadata = {
  title: 'ARTA: AI Requirements & Test Architect',
  description: 'Autonomous ATDD platform powered by BMAD TEA methodology',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ background: '#05050a', color: '#e2e8f0' }}>
        {/* Ambient gradient orbs */}
        <div className="ambient-orb" style={{ top: '-100px', left: '-100px', width: 400, height: 400, background: 'radial-gradient(circle, rgba(99,102,241,0.3), transparent 70%)' }} />
        <div className="ambient-orb" style={{ bottom: '-80px', right: '-80px', width: 300, height: 300, background: 'radial-gradient(circle, rgba(139,92,246,0.2), transparent 70%)' }} />
        <div className="ambient-orb" style={{ top: '40%', left: '50%', width: 250, height: 250, background: 'radial-gradient(circle, rgba(6,182,212,0.15), transparent 70%)' }} />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  )
}
