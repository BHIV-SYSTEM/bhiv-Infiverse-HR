import { useCallback, useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import {
  fetchNdaDocumentBlob,
  fetchTenantNdaQueue,
  reviewTenantNda,
  type TenantNdaQueueItem,
} from '../../services/api'

/**
 * Company NDA approvals (tenant dashboard at /client/ndas).
 *
 * The platform admin has NO access to NDA submissions (tenancy audit) — the
 * owning company reviews candidates inside its own tenant subset:
 *
 *   GET  /v1/nda/tenant/queue                     (company JWT, own subset)
 *   GET  /v1/nda/document?candidate_id=...        (scoped to the subset)
 *   POST /v1/nda/tenant/{candidate_id}/review     {decision, note}
 *
 * VERIFIED unlocks the candidate's gate immediately; REJECTED asks them to
 * re-upload from the wall screen. Candidates outside the subset are
 * unreachable (403), and independent candidates never appear here at all
 * (their unusable uploads are auto-rejected with machine-readable reasons).
 */
export default function NdaApprovalsPage() {
  const [queue, setQueue] = useState<TenantNdaQueueItem[]>([])
  const [loading, setLoading] = useState(true)
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [actingId, setActingId] = useState<string | null>(null)
  const [viewingId, setViewingId] = useState<string | null>(null)

  const errorMessage = (err: any, fallback: string) => {
    const detail = err?.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (detail?.message) return detail.message
    if (typeof err?.response?.data?.error === 'string') return err.response.data.error
    return err?.message || fallback
  }

  const load = useCallback(async () => {
    try {
      const result = await fetchTenantNdaQueue()
      setQueue(result.queue || [])
    } catch (err: any) {
      toast.error(errorMessage(err, 'Failed to load the NDA queue'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleView = async (item: TenantNdaQueueItem) => {
    setViewingId(item.candidate_id)
    try {
      const blob = await fetchNdaDocumentBlob(item.candidate_id)
      const url = URL.createObjectURL(blob)
      const win = window.open(url, '_blank', 'noopener')
      if (!win) {
        const a = document.createElement('a')
        a.href = url
        a.target = '_blank'
        a.rel = 'noopener'
        a.click()
      }
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    } catch (err: any) {
      toast.error(errorMessage(err, 'Could not open the NDA document'))
    } finally {
      setViewingId(null)
    }
  }

  const handleDecision = async (
    item: TenantNdaQueueItem,
    decision: 'VERIFIED' | 'REJECTED'
  ) => {
    setActingId(item.candidate_id)
    try {
      const result = await reviewTenantNda(item.candidate_id, decision, notes[item.candidate_id])
      setQueue((prev) => prev.filter((q) => q.candidate_id !== item.candidate_id))
      setNotes((prev) => {
        const next = { ...prev }
        delete next[item.candidate_id]
        return next
      })
      const who = item.candidate_name || shortId(item.candidate_id)
      if (result.gate_passed) {
        toast.success(`NDA approved — ${who} is unlocked and can use the platform.`)
      } else {
        toast.success(`NDA rejected — ${who} must re-upload from the wall screen.`)
      }
    } catch (err: any) {
      toast.error(errorMessage(err, 'Review decision failed'))
    } finally {
      setActingId(null)
    }
  }

  const awaiting = queue.filter((q) => q.status === 'NEEDS_REVIEW').length
  const rejected = queue.filter((q) => q.status === 'REJECTED').length

  return (
    <div>
      {/* Title */}
      <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-white">NDA Approvals</h1>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            your company subset only · VERIFIED unlocks the candidate, REJECTED asks for re-upload
          </p>
        </div>
        <button
          onClick={() => {
            setLoading(true)
            load()
          }}
          className="px-3 py-2 rounded-lg bg-white dark:bg-slate-800 hover:bg-slate-50 dark:hover:bg-slate-700 border border-slate-300 dark:border-slate-700 text-sm text-gray-700 dark:text-gray-300"
        >
          Refresh
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
        {[
          { label: 'Awaiting review', value: loading ? '—' : awaiting },
          { label: 'Rejected (may re-upload)', value: loading ? '—' : rejected },
          { label: 'Queue total', value: loading ? '—' : queue.length },
          { label: 'Scope', value: 'Company subset' },
        ].map((stat) => (
          <div
            key={stat.label}
            className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3"
          >
            <p className="text-xs text-gray-500 dark:text-gray-400">{stat.label}</p>
            <p className="text-xl font-bold text-gray-900 dark:text-white">{stat.value}</p>
          </div>
        ))}
      </div>

      {loading ? (
        <div className="flex flex-col items-center gap-4 py-16">
          <div className="w-10 h-10 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-gray-500 dark:text-gray-400">Loading the review queue…</p>
        </div>
      ) : queue.length === 0 ? (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-6 py-12 text-center">
          <p className="text-lg font-semibold text-gray-900 dark:text-white">All caught up</p>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            No NDAs from your subset are waiting for review. Submissions that auto-verification
            cannot confirm will appear here.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {queue.map((item) => {
            const busy = actingId === item.candidate_id
            const viewing = viewingId === item.candidate_id
            const reasons = item.verification?.reasons || []
            const who = item.candidate_name || item.candidate_id
            return (
              <div
                key={item.candidate_id}
                className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-5 py-4"
              >
                {/* Row 1: identity + status */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-gray-900 dark:text-white truncate">
                      {who}
                    </p>
                    <p className="font-mono text-[11px] text-gray-500 dark:text-gray-400 truncate">
                      {item.candidate_id}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate">
                      {item.filename || 'nda.pdf'}
                      {item.size_bytes ? ` · ${Math.round(item.size_bytes / 1024)} KB` : ''}
                      {item.uploaded_at ? ` · uploaded ${item.uploaded_at}` : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`px-2.5 py-1 rounded-full text-xs font-semibold border ${
                        item.status === 'NEEDS_REVIEW'
                          ? 'bg-amber-500/10 text-amber-500 border-amber-500/40'
                          : 'bg-rose-500/10 text-rose-500 border-rose-500/40'
                      }`}
                    >
                      {item.status}
                    </span>
                  </div>
                </div>

                {/* Row 2: verification evidence */}
                {reasons.length > 0 && (
                  <details className="mt-3 bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 rounded-lg px-4 py-2.5">
                    <summary className="cursor-pointer text-xs text-gray-500 dark:text-gray-400">
                      Automatic verification details
                      {item.verification?.method ? ` · ${item.verification.method}` : ''}
                      {item.verification?.checked_at
                        ? ` · checked ${item.verification.checked_at}`
                        : ''}
                    </summary>
                    <ul className="mt-2 space-y-1 text-xs font-mono text-gray-500 dark:text-gray-400 list-disc list-inside">
                      {reasons.map((r) => (
                        <li key={r}>{r}</li>
                      ))}
                    </ul>
                  </details>
                )}

                {/* Row 3: review note */}
                <textarea
                  value={notes[item.candidate_id] || ''}
                  onChange={(e) =>
                    setNotes((prev) => ({ ...prev, [item.candidate_id]: e.target.value }))
                  }
                  placeholder="Review note (optional) — e.g. 'signed + stamped copy verified by hand'"
                  rows={2}
                  className="mt-3 w-full bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm text-gray-800 dark:text-gray-200 placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                />

                {/* Row 4: actions */}
                <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
                  <button
                    onClick={() => handleView(item)}
                    disabled={viewing || busy}
                    className="px-3.5 py-2 rounded-lg bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 disabled:opacity-60 text-sm text-gray-700 dark:text-gray-200 border border-slate-300 dark:border-slate-600"
                  >
                    {viewing ? 'Opening…' : 'View PDF'}
                  </button>
                  <button
                    onClick={() => handleDecision(item, 'REJECTED')}
                    disabled={busy}
                    className="px-3.5 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-500 hover:text-red-400 border border-red-500/40 disabled:opacity-60 text-sm font-medium"
                  >
                    {busy ? 'Saving…' : 'Reject'}
                  </button>
                  <button
                    onClick={() => handleDecision(item, 'VERIFIED')}
                    disabled={busy}
                    className="px-3.5 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white text-sm font-medium"
                  >
                    {busy ? 'Saving…' : 'Approve'}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <p className="text-xs text-gray-500 mt-6">
        Endpoint reference: GET /v1/nda/tenant/queue · POST /v1/nda/tenant/
        {'{'}candidate_id{'}'}/review · auth = your company account — scoped to candidates who
        applied to your subset's jobs.
      </p>
    </div>
  )
}

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 14)}…` : id
}
