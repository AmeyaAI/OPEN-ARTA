'use client'

import React from 'react'

function Skeleton({ className = '', style }: { className?: string; style?: React.CSSProperties }) {
  return (
    <div
      className={`animate-pulse rounded ${className}`}
      style={{ background: '#1e1e3a', ...style }}
    />
  )
}

export function MetricCardSkeleton() {
  return (
    <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <Skeleton className="h-3 w-20 mb-3" />
      <Skeleton className="h-8 w-24 mb-2" />
      <Skeleton className="h-2 w-16" />
    </div>
  )
}

export function TableRowSkeleton({ cols = 5 }: { cols?: number }) {
  return (
    <div className="flex items-center gap-4 px-5 py-3" style={{ borderBottom: '1px solid #1e1e3a' }}>
      {Array.from({ length: cols }).map((_, i) => (
        <Skeleton key={i} className="h-4 flex-1" style={{ maxWidth: i === 0 ? 100 : undefined }} />
      ))}
    </div>
  )
}

export function ChartSkeleton({ height = 200 }: { height?: number }) {
  return (
    <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <Skeleton className="h-3 w-32 mb-4" />
      <Skeleton style={{ height }} />
    </div>
  )
}

export function CardSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className={`h-4 mb-3 ${i === 0 ? 'w-3/4' : i === lines - 1 ? 'w-1/2' : 'w-full'}`} />
      ))}
    </div>
  )
}

export function PageSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => <MetricCardSkeleton key={i} />)}
      </div>
      <div className="grid md:grid-cols-2 gap-4">
        <ChartSkeleton />
        <ChartSkeleton />
      </div>
      <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
        {Array.from({ length: 6 }).map((_, i) => <TableRowSkeleton key={i} />)}
      </div>
    </div>
  )
}

export default Skeleton
