import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import toast from 'react-hot-toast'
import { useAuth } from '../../context/AuthContext'
import { fetchInvite, registerViaInvite } from '../../services/api'

/**
 * Public tenanted invite link redemption (/invite/:token).
 *
 * The central admin never creates credentials for another company — it mints
 * a single-use, 7-day share link. Here the recipient creates their OWN
 * Client-Portal account, which the backend attaches to the tenant node in the
 * same call, and signs straight in.
 */
type InviteState =
  | { kind: 'loading' }
  | { kind: 'invalid' }
  | { kind: 'expired' }
  | { kind: 'error'; message: string }
  | {
      kind: 'ready'
      tenantName: string
      tenantLevel: string
      expiresAt: string | null
    }

export default function InviteSignupPage() {
  const { token = '' } = useParams<{ token: string }>()
  const { signIn } = useAuth()
  const navigate = useNavigate()

  const [state, setState] = useState<InviteState>({ kind: 'loading' })
  const [companyName, setCompanyName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [created, setCreated] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setState({ kind: 'loading' })
      try {
        const data = await fetchInvite(token)
        if (cancelled) return
        const tenant = data.invite?.tenant
        if (!tenant?.name) {
          setState({ kind: 'invalid' })
          return
        }
        setState({
          kind: 'ready',
          tenantName: tenant.name,
          tenantLevel: tenant.level || 'account',
          expiresAt: data.invite.expires_at,
        })
        setCompanyName((prev) => prev || tenant.name)
      } catch (err: any) {
        if (cancelled) return
        const status = err?.response?.status
        if (status === 404) setState({ kind: 'invalid' })
        else if (status === 410) setState({ kind: 'expired' })
        else
          setState({
            kind: 'error',
            message: err?.response?.data?.detail || 'Could not load this invite link.',
          })
      }
    }
    if (token) load()
    else setState({ kind: 'invalid' })
    return () => {
      cancelled = true
    }
  }, [token])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    const emailNorm = email.trim().toLowerCase()
    if (!companyName.trim()) {
      setError('Company name is required')
      return
    }
    if (!emailNorm || !emailNorm.includes('@')) {
      setError('A valid email is required')
      return
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters')
      return
    }
    setIsSubmitting(true)
    try {
      await registerViaInvite(token, {
        email: emailNorm,
        password,
        company_name: companyName.trim(),
      })
      setCreated(true)
      const { error: signInError } = await signIn(emailNorm, password, 'client')
      if (signInError) {
        toast.success('Account created — sign in to continue')
        setIsSubmitting(false)
        return
      }
      toast.success('Welcome aboard!')
      navigate('/client', { replace: true })
    } catch (err: any) {
      const status = err?.response?.status
      const detail = err?.response?.data?.detail
      if (status === 404) {
        setState({ kind: 'invalid' })
      } else if (status === 410) {
        setState({ kind: 'expired' })
      } else if (status === 409) {
        setError('This email is already registered — sign in instead.')
      } else if (typeof detail === 'string' && detail) {
        setError(detail)
      } else if (status === 422) {
        setError('Check the form: password must be at least 8 characters.')
      } else {
        setError(err?.message || 'Could not create the account. Please try again.')
      }
      setIsSubmitting(false)
    }
  }

  const gradient = 'from-emerald-500 to-teal-500'

  const banner = (
    <div className="text-center mb-8">
      <div className="inline-flex items-center gap-3 mb-6">
        <div
          className={`w-12 h-12 rounded-2xl bg-gradient-to-br ${gradient} p-0.5 shadow-lg shadow-emerald-500/20`}
        >
          <div className="w-full h-full rounded-2xl bg-slate-900 flex items-center justify-center">
            <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z"
              />
            </svg>
          </div>
        </div>
        <span className="text-2xl font-bold block text-left">
          <span className={`bg-gradient-to-r ${gradient} bg-clip-text text-transparent`}>Sampada</span>
          <span className="text-gray-400 text-sm font-normal block mt-0.5">Company Invite</span>
        </span>
      </div>
    </div>
  )

  if (state.kind === 'loading') {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center px-4">
        <div className="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (state.kind === 'invalid' || state.kind === 'expired' || state.kind === 'error') {
    const isExpired = state.kind === 'expired'
    const message =
      state.kind === 'invalid'
        ? 'This invite link is no longer valid. Ask your company admin for a fresh link.'
        : isExpired
          ? 'This invite link has expired (links are valid for 7 days). Ask your company admin for a fresh link.'
          : state.message
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center px-4 py-8">
        <div className="relative z-10 w-full max-w-md">
          {banner}
          <div className="bg-slate-800/60 backdrop-blur-xl border border-slate-700/50 rounded-3xl p-8 shadow-2xl text-center">
            <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center">
              <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M12 9v2m0 4h.01M5.07 19h13.86a2 2 0 001.74-3L13.74 4a2 2 0 00-3.48 0L3.34 16a2 2 0 001.73 3z"
                />
              </svg>
            </div>
            <h1 className="text-xl font-bold text-white mb-2">
              {isExpired ? 'Invite Expired' : state.kind === 'invalid' ? 'Invalid Invite' : 'Something went wrong'}
            </h1>
            <p className="text-gray-400 text-sm mb-6">{message}</p>
            <Link
              to="/"
              className="inline-block px-5 py-2.5 rounded-xl text-sm font-medium text-gray-300 border border-slate-600 hover:text-white hover:border-slate-500 transition-colors"
            >
              ← Back to Sampada
            </Link>
          </div>
        </div>
      </div>
    )
  }

  if (created) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center px-4 py-8">
        <div className="relative z-10 w-full max-w-md">
          {banner}
          <div className="bg-slate-800/60 backdrop-blur-xl border border-slate-700/50 rounded-3xl p-8 shadow-2xl text-center">
            <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
              <svg className="w-7 h-7 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <h1 className="text-xl font-bold text-white mb-2">Account created for {state.tenantName}</h1>
            <p className="text-gray-400 text-sm mb-6">
              Sign in with the email and password you just chose to open your company dashboard.
            </p>
            <Link
              to="/auth"
              className="inline-block w-full py-3 rounded-xl font-semibold text-white bg-gradient-to-r from-emerald-500 to-teal-500 shadow-lg shadow-emerald-500/20 hover:opacity-90 transition-all"
            >
              Go to Sign In
            </Link>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center px-4 py-8">
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className={`absolute top-20 right-20 w-[500px] h-[500px] bg-gradient-to-br ${gradient} rounded-full mix-blend-multiply filter blur-3xl opacity-20 animate-blob`} />
        <div className="absolute bottom-20 left-20 w-[400px] h-[400px] bg-emerald-500 rounded-full mix-blend-multiply filter blur-3xl opacity-10 animate-blob animation-delay-2000" />
      </div>

      <div className="relative z-10 w-full max-w-md">
        {banner}
        <div className="bg-slate-800/60 backdrop-blur-xl border border-slate-700/50 rounded-3xl p-8 shadow-2xl">
          <div className="text-center mb-6">
            <h1 className="text-2xl font-bold text-white mb-1">
              Join <span className={`bg-gradient-to-r ${gradient} bg-clip-text text-transparent`}>{state.tenantName}</span>
            </h1>
            <p className="text-gray-400 text-sm">
              You were invited to create a company account
              {state.tenantLevel === 'key_account' ? ' (key account)' : ''}.
              {state.expiresAt && (
                <>
                  {' '}
                  Link valid until{' '}
                  {new Date(state.expiresAt).toLocaleDateString(undefined, {
                    day: 'numeric',
                    month: 'short',
                    year: 'numeric',
                  })}
                  .
                </>
              )}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">
                Company Name <span className="text-red-400">*</span>
              </label>
              <input
                type="text"
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
                placeholder={state.tenantName}
                className="w-full px-4 py-3 bg-slate-900/60 border border-slate-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500 transition-all"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">
                Your Email <span className="text-red-400">*</span>
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="founder@company.com"
                autoComplete="username"
                className="w-full px-4 py-3 bg-slate-900/60 border border-slate-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500 transition-all"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">
                Password <span className="text-red-400">*</span>
                <span className="text-gray-500 font-normal ml-2">min 8 characters</span>
              </label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  className="w-full px-4 py-3 pr-12 bg-slate-900/60 border border-slate-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500 transition-all"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-white text-sm"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? 'Hide' : 'Show'}
                </button>
              </div>
            </div>

            {error && (
              <div className="px-4 py-3 bg-red-500/10 border border-red-500/30 rounded-xl text-red-400 text-sm">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={isSubmitting}
              className={`w-full py-3.5 rounded-xl font-semibold text-white bg-gradient-to-r ${gradient} shadow-lg shadow-emerald-500/20 hover:opacity-90 transition-all disabled:opacity-60 disabled:cursor-not-allowed`}
            >
              {isSubmitting ? (
                <span className="inline-flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  Creating account…
                </span>
              ) : (
                'Create Company Account'
              )}
            </button>
          </form>

          <p className="mt-6 text-center text-xs text-gray-500">
            Single-use invite — after you create your account, invite your HR team and candidates
            from your dashboard.
          </p>
        </div>

        <div className="mt-6 text-center">
          <Link to="/" className="text-sm text-gray-400 hover:text-white transition-colors">
            ← Back to Sampada
          </Link>
        </div>
      </div>
    </div>
  )
}
