'use client'

import { useState } from 'react'
import GherkinHighlighter from './GherkinHighlighter'

interface ATDDSplitViewProps {
  requirement: {
    id: string
    title: string
    description?: string
    priority?: string
    risk_score?: number
    acceptance_criteria?: { id: string; statement: string; covered?: boolean }[]
  }
  gherkin?: string
  scriptContent?: string
  testData?: { columns: string[]; rows: string[][] } | null
  onClose?: () => void
}

const TAB_COLORS: Record<string, string> = {
  Gherkin: '#6366f1',
  Playwright: '#06b6d4',
  Newman: '#f59e0b',
  k6: '#10b981',
}

export default function ATDDSplitView({ requirement, gherkin, scriptContent, testData: _testData, onClose }: ATDDSplitViewProps) {
  const [activeTab, setActiveTab] = useState<'Gherkin' | 'Playwright' | 'Newman' | 'k6'>('Gherkin')

  const acs = requirement.acceptance_criteria || []
  const priority = requirement.priority || 'P1'
  const riskScore = requirement.risk_score || 5.0

  const priorityColor = priority === 'P0' ? '#fb7185' : priority === 'P1' ? '#fbbf24' : priority === 'P2' ? '#a5b4fc' : '#94a3b8'

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2" style={{ borderBottom: '1px solid #1e1e3a' }}>
        <span className="text-xs font-semibold" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          ATDD: REQUIREMENT {'\u2192'} TESTS
        </span>
        {onClose && (
          <button onClick={onClose} className="text-xs" style={{ color: '#64748b' }}>{'\u2715'}</button>
        )}
      </div>

      <div className="grid grid-cols-2" style={{ minHeight: 300 }}>
        {/* ── LEFT: Requirement Input ──────────────────────────── */}
        <div className="p-4" style={{ borderRight: '1px solid #1e1e3a' }}>
          <div className="text-[10px] font-bold mb-3" style={{ color: '#6366f1', fontFamily: 'Space Mono, monospace' }}>
            INPUT: REQUIREMENT
          </div>

          <h4 className="text-sm font-bold mb-2" style={{ color: '#e2e8f0' }}>
            {requirement.id}: {requirement.title}
          </h4>

          {requirement.description && (
            <p className="text-xs mb-3" style={{ color: '#94a3b8', lineHeight: 1.5 }}>
              {requirement.description.slice(0, 200)}{requirement.description.length > 200 ? '...' : ''}
            </p>
          )}

          {acs.length > 0 && (
            <>
              <div className="text-xs font-bold mb-1.5" style={{ color: '#e2e8f0' }}>
                Acceptance Criteria:
              </div>
              <div className="space-y-1 mb-3">
                {acs.map(ac => (
                  <div key={ac.id} className="flex items-start gap-1.5 text-xs" style={{ color: '#94a3b8' }}>
                    <span style={{ color: ac.covered ? '#10b981' : '#f59e0b' }}>
                      {ac.covered ? '\u2713' : '\u2022'}
                    </span>
                    <span>{ac.statement}</span>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="flex items-center gap-2 mt-3">
            <span className="text-[10px] font-bold px-2 py-0.5 rounded"
                  style={{ background: `${priorityColor}18`, color: priorityColor, border: `1px solid ${priorityColor}30` }}>
              {priority}
            </span>
            <span className="text-[10px] font-bold px-2 py-0.5 rounded"
                  style={{ background: '#f59e0b18', color: '#fbbf24', border: '1px solid #f59e0b30' }}>
              RISK: {riskScore}
            </span>
          </div>

          <div className="text-center mt-4 text-xs font-bold" style={{ color: '#6366f1', fontFamily: 'Space Mono, monospace' }}>
            {'\u25BC'} ARTA Generates {'\u25BC'}
          </div>
        </div>

        {/* ── RIGHT: Generated Output ─────────────────────────── */}
        <div className="p-4">
          <div className="text-[10px] font-bold mb-3" style={{ color: '#10b981', fontFamily: 'Space Mono, monospace' }}>
            OUTPUT: AUTO-GENERATED TESTS
          </div>

          {/* Tab bar */}
          <div className="flex gap-1 mb-3">
            {(['Gherkin', 'Playwright', 'Newman', 'k6'] as const).map(tab => {
              const active = activeTab === tab
              const color = TAB_COLORS[tab]
              return (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  className="text-[10px] px-2 py-1 rounded font-medium transition"
                  style={{
                    background: active ? `${color}20` : 'transparent',
                    color: active ? color : '#94a3b8',
                    border: active ? `1px solid ${color}40` : '1px solid transparent',
                  }}
                >
                  {tab}
                </button>
              )
            })}
          </div>

          {/* Content */}
          {activeTab === 'Gherkin' && (
            <GherkinHighlighter code={gherkin || ''} maxHeight={250} />
          )}

          {activeTab === 'Playwright' && (
            <pre className="text-[10px] p-3 rounded-lg overflow-auto" style={{
              background: '#0a0a14', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace',
              maxHeight: 250,
            }}>
              {scriptContent || 'No Playwright script generated yet'}
            </pre>
          )}

          {activeTab === 'Newman' && (
            <div className="text-xs p-3 rounded-lg" style={{ background: '#0a0a14', color: '#64748b' }}>
              Newman collection available (run via Test Explorer)
            </div>
          )}

          {activeTab === 'k6' && (
            <div className="text-xs p-3 rounded-lg" style={{ background: '#0a0a14', color: '#64748b' }}>
              k6 performance script available (run via NFR Assessment)
            </div>
          )}

          {/* Stats */}
          {gherkin && (
            <div className="flex gap-2 mt-3 flex-wrap">
              {[
                { label: `${(gherkin.match(/Scenario:/g) || []).length} scenarios`, color: '#10b981' },
                { label: `${acs.length} ACs`, color: '#06b6d4' },
              ].map(({ label, color }) => (
                <span key={label} className="text-[9px] px-2 py-0.5 rounded-full font-medium"
                      style={{ background: `${color}15`, color, border: `1px solid ${color}25` }}>
                  {label}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Footer */}
      <div className="text-center py-2 text-[10px]" style={{ color: '#94a3b8', borderTop: '1px solid #1e1e3a' }}>
        Supports: Playwright {'\u00b7'} Newman {'\u00b7'} k6 {'\u00b7'} Cypress {'\u00b7'} Selenium {'\u00b7'} Appium {'\u00b7'} OWASP ZAP
      </div>
    </div>
  )
}
