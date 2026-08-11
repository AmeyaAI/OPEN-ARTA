'use client'

type DiffLine = {
  type: 'add' | 'remove' | 'context'
  content: string
  oldLineNumber?: number
  newLineNumber?: number
}

type DiffRendererProps = {
  lines: DiffLine[]
  filename?: string
  language?: string
  showLineNumbers?: boolean
}

const LINE_BG: Record<DiffLine['type'], string> = {
  add: 'rgba(16, 185, 129, 0.1)',
  remove: 'rgba(239, 68, 68, 0.1)',
  context: 'transparent',
}

const LINE_TEXT: Record<DiffLine['type'], string> = {
  add: '#34d399',
  remove: '#fb7185',
  context: '#94a3b8',
}

const LINE_PREFIX: Record<DiffLine['type'], string> = {
  add: '+',
  remove: '-',
  context: ' ',
}

export default function DiffRenderer({ lines, filename, language, showLineNumbers = true }: DiffRendererProps) {
  const additions = lines.filter(l => l.type === 'add').length
  const deletions = lines.filter(l => l.type === 'remove').length

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: '#0a0a14' }}>
      {/* Optional header */}
      {filename && (
        <div className="px-4 py-2.5 flex items-center justify-between"
             style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}>
          <div className="flex items-center gap-3">
            <span className="text-xs" style={{ color: '#a5b4fc', fontFamily: "'JetBrains Mono', monospace" }}>
              {filename}
            </span>
            {language && (
              <span className="text-[10px] px-2 py-0.5 rounded"
                    style={{ background: 'rgba(99,102,241,0.12)', color: '#818cf8' }}>
                {language}
              </span>
            )}
          </div>
          <span className="text-xs" style={{ fontFamily: "'Space Mono', monospace" }}>
            <span style={{ color: '#34d399' }}>+{additions}</span>
            <span style={{ color: '#94a3b8' }}> / </span>
            <span style={{ color: '#fb7185' }}>-{deletions}</span>
          </span>
        </div>
      )}

      {/* Lines */}
      <div className="overflow-x-auto">
        {lines.map((line, i) => (
          <div
            key={i}
            className="flex text-xs leading-6"
            style={{ background: LINE_BG[line.type] }}
          >
            {/* Line numbers */}
            {showLineNumbers && (
              <>
                <span
                  className="w-12 text-right pr-2 select-none flex-shrink-0"
                  style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace", fontSize: 10 }}
                >
                  {line.oldLineNumber ?? ''}
                </span>
                <span
                  className="w-12 text-right pr-3 select-none flex-shrink-0"
                  style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace", fontSize: 10 }}
                >
                  {line.newLineNumber ?? ''}
                </span>
              </>
            )}

            {/* Prefix */}
            <span
              className="w-4 text-center flex-shrink-0 select-none"
              style={{ color: LINE_TEXT[line.type], fontFamily: "'JetBrains Mono', monospace" }}
            >
              {LINE_PREFIX[line.type]}
            </span>

            {/* Content */}
            <span
              className="flex-1 whitespace-pre pr-4"
              style={{
                color: line.type === 'context' ? '#94a3b8' : '#e2e8f0',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: 12,
              }}
            >
              {line.content}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export type { DiffLine, DiffRendererProps }
