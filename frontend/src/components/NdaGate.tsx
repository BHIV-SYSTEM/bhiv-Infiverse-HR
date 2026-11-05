import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import {
  NdaStatus,
  NdaStatusValue,
  fetchNdaStatus,
  openNdaTemplate,
  uploadNda,
} from '../services/api'

interface NdaGateProps {
  children: React.ReactNode
}

const STATUS_COPY: Record<
  NdaStatusValue,
  { title: string; body: string; action: string }
> = {
  NOT_SUBMITTED: {
    title: 'Sign your NDA to continue',
    body: 'Before you can apply to jobs or use the platform, your Non-Disclosure Agreement must be uploaded and verified. Download the official template, print and sign it (stamp it if your institute requires), then upload the signed PDF below.',
    action: 'Upload signed NDA',
  },
  NEEDS_REVIEW: {
    title: 'Your NDA is being reviewed',
    body: 'We received your NDA, but automatic verification could not confirm the signature and stamp (for example, scanned documents). An administrator will review it shortly. You can also upload a clearer copy to speed things up.',
    action: 'Upload a clearer copy',
  },
  REJECTED: {
    title: 'Your NDA needs to be re-uploaded',
    body: 'The uploaded document was not a valid signed NDA (unreadable, encrypted, or incomplete). Please open the official template, sign and stamp it, and upload the PDF again.',
    action: 'Re-upload signed NDA',
  },
  VERIFIED: {
    title: 'NDA verified',
    body: 'Your NDA has been verified.',
    action: '',
  },
}

/**
 * NdaGate — post-login candidate wall.
 *
 * Renders `children` only when the candidate's NDA is VERIFIED (or the user
 * is a guest / non-candidate, which the backend gate also bypasses). The
 * backend independently blocks apply / profile-update / document-upload /
 * CV-export with 403 NDA_NOT_VERIFIED, so this wall is UX, not the only
 * barrier.
 */
export default function NdaGate({ children }: NdaGateProps) {
  const { user, userRole, loading: authLoading } = useAuth()
  const isGuest = user?.isGuest === true
  const isCandidate = userRole === 'candidate' || user?.role === 'candidate'
  const shouldGate = isCandidate && !isGuest

  const [status, setStatus] = useState<NdaStatus | null>(null)
  const [checking, setChecking] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const loadStatus = useCallback(async () => {
    try {
      const next = await fetchNdaStatus()
      setStatus(next)
      setError('')
      return next
    } catch (e: any) {
      setError(
        e?.response?.data?.detail?.message ||
          'Could not check your NDA status. Please try again.'
      )
      return null
    }
  }, [])

  useEffect(() => {
    if (authLoading) return
    if (!shouldGate) {
      setChecking(false)
      return
    }
    let cancelled = false
    ;(async () => {
      await loadStatus()
      if (!cancelled) setChecking(false)
    })()
    return () => {
      cancelled = true
    }
  }, [authLoading, shouldGate, loadStatus])

  const handlePick = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (file.type !== 'application/pdf') {
      setError('The NDA must be uploaded as a PDF file.')
      return
    }
    setUploading(true)
    setError('')
    setNotice('')
    try {
      const result = await uploadNda(file)
      setNotice(result.message)
      await loadStatus()
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setError(
        typeof detail === 'string'
          ? detail
          : detail?.message || 'Upload failed. Please try again.'
      )
    } finally {
      setUploading(false)
    }
  }

  const handleOpenTemplate = async () => {
    setError('')
    try {
      await openNdaTemplate()
    } catch {
      setError('Could not open the template. Please try again.')
    }
  }

  // Loading / bypass paths render the app as usual.
  if (authLoading || (shouldGate && checking)) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-slate-400">Checking your NDA status...</p>
        </div>
      </div>
    )
  }
  if (!shouldGate || status?.gate_passed) {
    return <>{children}</>
  }

  const current: NdaStatusValue = status?.status ?? 'NOT_SUBMITTED'
  const copy = STATUS_COPY[current] || STATUS_COPY.NOT_SUBMITTED
  const reasons = status?.verification?.reasons || []

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-2xl">
        <div className="bg-slate-800 border border-slate-700 rounded-xl shadow-2xl overflow-hidden">
          <div className="bg-gradient-to-r from-emerald-600 to-teal-600 px-6 py-4">
            <h1 className="text-lg font-semibold text-white">
              Sampada — Candidate Onboarding
            </h1>
            <p className="text-emerald-50 text-sm">Non-Disclosure Agreement required</p>
          </div>

          <div className="p-6 space-y-5">
            <div>
              <h2 className="text-xl font-semibold text-white">{copy.title}</h2>
              <p className="mt-2 text-sm leading-relaxed text-slate-300">{copy.body}</p>
            </div>

            {status?.document && (
              <div className="bg-slate-900/60 border border-slate-700 rounded-lg px-4 py-3 text-sm text-slate-300">
                <span className="text-slate-400">On file:</span>{' '}
                <span className="font-medium text-slate-200">
                  {status.document.filename}
                </span>{' '}
                <span className="text-slate-500">
                  · {Math.round((status.document.size_bytes || 0) / 1024)} KB ·{' '}
                  {status.document.uploaded_at}
                </span>
                <div className="mt-1">
                  <span className="text-slate-400">Status:</span>{' '}
                  <span
                    className={
                      current === 'VERIFIED'
                        ? 'text-emerald-400 font-medium'
                        : current === 'REJECTED'
                          ? 'text-rose-400 font-medium'
                          : 'text-amber-400 font-medium'
                    }
                  >
                    {current}
                  </span>
                </div>
              </div>
            )}

            {notice && (
              <div className="border border-emerald-600/50 bg-emerald-600/10 text-emerald-300 rounded-lg px-4 py-3 text-sm">
                {notice}
              </div>
            )}
            {error && (
              <div className="border border-rose-600/50 bg-rose-600/10 text-rose-300 rounded-lg px-4 py-3 text-sm">
                {error}
              </div>
            )}

            {reasons.length > 0 && (
              <details className="bg-slate-900/60 border border-slate-700 rounded-lg px-4 py-3">
                <summary className="cursor-pointer text-sm text-slate-400">
                  Automatic verification details
                </summary>
                <ul className="mt-2 space-y-1 text-xs font-mono text-slate-400 list-disc list-inside">
                  {reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </details>
            )}

            <div className="flex flex-col sm:flex-row gap-3 pt-1">
              <button
                type="button"
                onClick={handleOpenTemplate}
                className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-sm font-medium transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                Open official template
              </button>

              <button
                type="button"
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
                className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors"
              >
                {uploading ? (
                  <>
                    <span className="w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" />
                    Verifying...
                  </>
                ) : (
                  copy.action
                )}
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/pdf"
                className="hidden"
                onChange={handlePick}
              />
            </div>

            <ol className="text-xs text-slate-400 space-y-1 list-decimal list-inside pt-2 border-t border-slate-700/70">
              <li>Open the official NDA template and print it to PDF.</li>
              <li>Sign it (and stamp it if your institute requires).</li>
              <li>Upload the signed PDF — verification takes a few seconds.</li>
            </ol>
          </div>
        </div>

        <p className="text-center text-xs text-slate-500 mt-4">
          Your application stays locked until the NDA is verified. Need help?
          Contact your placement coordinator.
        </p>
      </div>
    </div>
  )
}
