'use client'

import { useMemo } from 'react'

/**
 * Lightweight Gherkin syntax highlighter — no external dependencies.
 * Colors: Keywords=indigo, Given/When/Then=cyan, Strings=green, Comments=gray, Tags=violet
 */

const KEYWORD_COLORS: Record<string, string> = {
  'Feature:': '#6366f1',
  'Background:': '#6366f1',
  'Scenario:': '#818cf8',
  'Scenario Outline:': '#818cf8',
  'Examples:': '#818cf8',
  'Rule:': '#6366f1',
}

const STEP_KEYWORDS = ['Given', 'When', 'Then', 'And', 'But']
const STEP_COLOR = '#06b6d4'
const STRING_COLOR = '#10b981'
const COMMENT_COLOR = '#94a3b8'
const TAG_COLOR = '#8b5cf6'
const DEFAULT_COLOR = '#94a3b8'

function highlightLine(line: string): JSX.Element {
  const trimmed = line.trimStart()
  const indent = line.length - trimmed.length
  const pad = '\u00a0'.repeat(indent)

  // Comment
  if (trimmed.startsWith('#')) {
    return <span style={{ color: COMMENT_COLOR }}>{pad}{trimmed}</span>
  }

  // Tag (@tier1, @wip, etc.)
  if (trimmed.startsWith('@')) {
    return <span style={{ color: TAG_COLOR, fontWeight: 600 }}>{pad}{trimmed}</span>
  }

  // Empty line
  if (!trimmed) return <span>{'\u00a0'}</span>

  // Feature/Scenario/Background keywords
  for (const [kw, color] of Object.entries(KEYWORD_COLORS)) {
    if (trimmed.startsWith(kw)) {
      const rest = trimmed.slice(kw.length)
      return (
        <span>
          {pad}
          <span style={{ color, fontWeight: 700 }}>{kw}</span>
          <span style={{ color: '#e2e8f0' }}>{rest}</span>
        </span>
      )
    }
  }

  // Given/When/Then/And/But step keywords
  for (const kw of STEP_KEYWORDS) {
    if (trimmed.startsWith(kw + ' ') || trimmed === kw) {
      const rest = trimmed.slice(kw.length)
      // Highlight strings in quotes within the step
      const parts = highlightStrings(rest)
      return (
        <span>
          {pad}
          <span style={{ color: STEP_COLOR, fontWeight: 600 }}>{kw}</span>
          {parts}
        </span>
      )
    }
  }

  // Pipe-delimited table row
  if (trimmed.startsWith('|')) {
    return <span style={{ color: '#64748b' }}>{pad}{trimmed}</span>
  }

  // Default
  return <span style={{ color: DEFAULT_COLOR }}>{pad}{trimmed}</span>
}

function highlightStrings(text: string): JSX.Element[] {
  const parts: JSX.Element[] = []
  const regex = /"([^"]*?)"|'([^']*?)'/g
  let lastIndex = 0
  let match

  while ((match = regex.exec(text)) !== null) {
    // Text before the match
    if (match.index > lastIndex) {
      parts.push(<span key={lastIndex} style={{ color: DEFAULT_COLOR }}>{text.slice(lastIndex, match.index)}</span>)
    }
    // The quoted string
    parts.push(<span key={match.index} style={{ color: STRING_COLOR, fontWeight: 500 }}>{match[0]}</span>)
    lastIndex = match.index + match[0].length
  }

  // Remaining text
  if (lastIndex < text.length) {
    parts.push(<span key={lastIndex} style={{ color: DEFAULT_COLOR }}>{text.slice(lastIndex)}</span>)
  }

  if (parts.length === 0) {
    parts.push(<span key="all" style={{ color: DEFAULT_COLOR }}>{text}</span>)
  }

  return parts
}

export default function GherkinHighlighter({ code, maxHeight }: { code: string; maxHeight?: number }) {
  if (!code || code.trim().length === 0) {
    return (
      <div style={{ color: '#94a3b8', fontFamily: 'JetBrains Mono, Consolas, monospace', fontSize: 12, padding: 16 }}>
        No Gherkin scenario available
      </div>
    )
  }

  const lines = useMemo(() => code.split('\n'), [code])

  return (
    <pre
      style={{
        fontFamily: 'JetBrains Mono, Consolas, monospace',
        fontSize: 11,
        lineHeight: 1.7,
        padding: '12px 16px',
        margin: 0,
        background: '#0a0a14',
        borderRadius: 8,
        overflowX: 'auto',
        overflowY: maxHeight ? 'auto' : undefined,
        maxHeight: maxHeight || undefined,
        whiteSpace: 'pre',
      }}
    >
      {lines.map((line, i) => (
        <div key={`L${i}-${line.slice(0, 20)}`}>{highlightLine(line)}</div>
      ))}
    </pre>
  )
}
