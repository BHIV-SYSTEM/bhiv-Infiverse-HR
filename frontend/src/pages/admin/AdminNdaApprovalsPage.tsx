import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import { useAuth } from '../../context/AuthContext'
import {
  fetchNdaAdminQueue,
  fetchNdaDocumentBlob,
  reviewNda,
  type NdaQueueItem,
} from '../../services/api'

/**
 * Platform admin: NDA approvals.
 *
 * Manual-review fallback for the candidate NDA gate: auto-verification
 * (PyPDF2) honestly reports NEEDS_REVIEW for scanned/hand-signed PDFs and
 * REJECTED for unusable uploads — a human decides here.
 *
 *   GET  /v1/nda/admin/queue                     (service/admin auth)
 *   GET  /v1/nda/document?candidate_id=...       (view the actual PDF)
 *   POST /v1/nda/admin/{candidate_id}/review     {decision, note}
 *
 * VERIFIED unlocks the candidate's gate immediately; REJECTED asks them
 * to re-upload from the wall screen.
 */
export default function AdminNdaApprovalsPage() {
  const { signOut } = useAuth()
  const navigate = useNavigate()
  const [queue, setQueue] = useState<NdaQueueItem[]>([])
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
      const result = await fetchNdaAdminQueue()
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

  const handleLogout = async () => {
    await signOut()
    navigate('/admin/login', { replace: true })
  }

  const handleView = async (item: NdaQueueItem) => {
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
    item: NdaQueueItem,
    decision: 'VERIFIED' | 'REJECTED'
  ) => {
    setActingId(item.candidate_id)
    try {
      const result = await reviewNda(item.candidate_id, decision, notes[item.candidate_id])
      setQueue((prev) => prev.filter((q) => q.candidate_id !== item.candidate_id))
      setNotes((prev) => {
        const next = { ...prev }
        delete next[item.candidate_id]
        return next
      })
      if (result.gate_passed) {
        toast.success(
          `NDA approved — ${shortId(item.candidate_id)} is unlocked and can use the platform.`
        )
      } else {
        toast.success(
          `NDA rejected — ${shortId(item.candidate_id)} must re-upload from the wall screen.`
        )
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
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-gray-100">
      {/* Header */}
      <header className="border-b border-slate-700/70 bg-slate-900/70 backdrop-blur sticky top-0 z-20">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold text-white">NDA Approvals</h1>
            <p className="text-xs text-gray-400">
              manual review fallback for the candidate NDA gate · VERIFIED unlocks, REJECTED asks
              for re-upload
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                setLoading(true)
                load()
              }}
              className="px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm text-gray-300"
            >
              Refresh
            </button>
            <button
              onClick={() => navigate('/admin/tenants')}
              className="px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm text-gray-300"
            >
              Tenant Hierarchy
            </button>
            <button
              onClick={() => navigate('/control')}
              className="px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm text-gray-300"
            >
              Control Center
            </button>
            <button
              onClick={handleLogout}
              className="px-3 py-2 rounded-lg bg-red-500/10 hover:bg-red-500 text-red-500 hover:text-white border border-red-500/40 text-sm"
            >
              Logout
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
        {/* Stats */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          {[
            { label: 'Awaiting review', value: loading ? '—' : awaiting },
            { label: 'Rejected (may re-upload)', value: loading ? '—' : rejected },
            { label: 'Queue total', value: loading ? '—' : queue.length },
            { label: 'Scope', value: 'Platform' },
          ].map((stat) => (
            <div
              key={stat.label}
              className="bg-slate-800/60 border border-slate-700 rounded-xl px-4 py-3"
            >
              <p className="text-xs text-gray-400">{stat.label}</p>
              <p className="text-xl font-bold text-white">{stat.value}</p>
            </div>
          ))}
        </div>

        {loading ? (
          <div className="flex flex-col items-center gap-4 py-16">
            <div className="w-10 h-10 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            <p className="text-sm text-gray-400">Loading the review queue…</p>
          </div>
        ) : queue.length === 0 ? (
          <div className="bg-slate-800/60 border border-slate-700 rounded-xl px-6 py-12 text-center">
            <p className="text-lg font-semibold text-white">All caught up</p>
            <p className="text-sm text-gray-400 mt-1">
              No NDAs are waiting for manual review. Submissions that auto-verification cannot
              confirm will appear here.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {queue.map((item) => {
              const busy = actingId === item.candidate_id
              const viewing = viewingId === item.candidate_id
              const reasons = item.verification?.reasons || []
              return (
                <div
                  key={item.candidate_id}
                  className="bg-slate-800/60 border border-slate-700 rounded-xl px-5 py-4"
                >
                  {/* Row 1: identity + status */}
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-mono text-sm text-white truncate">
                        {item.candidate_id}
                      </p>
                      <p className="text-xs text-gray-400 mt-0.5 truncate">
                        {item.filename || 'nda.pdf'}
                        {item.size_bytes
                          ? ` · ${Math.round(item.size_bytes / 1024)} KB`
                          : ''}
                        {item.uploaded_at ? ` · uploaded ${item.uploaded_at}` : ''}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <span
                        className={`px-2.5 py-1 rounded-full text-xs font-semibold border ${
                          item.status === 'NEEDS_REVIEW'
                            ? 'bg-amber-500/10 text-amber-400 border-amber-500/40'
                            : 'bg-rose-500/10 text-rose-400 border-rose-500/40'
                        }`}
                      >
                        {item.status}
                      </span>
                    </div>
                  </div>

                  {/* Row 2: verification evidence */}
                  {reasons.length > 0 && (
                    <details className="mt-3 bg-slate-900/60 border border-slate-700 rounded-lg px-4 py-2.5">
                      <summary className="cursor-pointer text-xs text-gray-400">
                        Automatic verification details
                        {item.verification?.method
                          ? ` · ${item.verification.method}`
                          : ''}
                        {item.verification?.checked_at
                          ? ` · checked ${item.verification.checked_at}`
                          : ''}
                      </summary>
                      <ul className="mt-2 space-y-1 text-xs font-mono text-gray-400 list-disc list-inside">
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
                    className="mt-3 w-full bg-slate-900/60 border border-slate-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                  />

                  {/* Row 4: actions */}
                  <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
                    <button
                      onClick={() => handleView(item)}
                      disabled={viewing || busy}
                      className="px-3.5 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 disabled:opacity-60 text-sm text-gray-200 border border-slate-600"
                    >
                      {viewing ? 'Opening…' : 'View PDF'}
                    </button>
                    <button
                      onClick={() => handleDecision(item, 'REJECTED')}
                      disabled={busy}
                      className="px-3.5 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 hover:text-red-300 border border-red-500/40 disabled:opacity-60 text-sm font-medium"
                    >
                      {busy && actingId === item.candidate_id ? 'Saving…' : 'Reject'}
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
          Endpoint reference: GET /v1/nda/admin/queue · POST /v1/nda/admin/{'{'}candidate_id
          {'}'}/review · auth = platform admin session or service API key.
        </p>
      </main>
    </div>
  )
}

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 14)}…` : id
}
