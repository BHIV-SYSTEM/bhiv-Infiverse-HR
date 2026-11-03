import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import toast from 'react-hot-toast'
import { useAuth } from '../../context/AuthContext'

/**
 * Platform admin sign-in — the admin link target (/admin/login).
 *
 * Signs into the multi-tenant hierarchy at platform scope (BHIV / Sampada
 * operators managing key accounts such as SVA). Key accounts themselves keep
 * signing in through the regular client portal with their CP credentials.
 */
export default function AdminLoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const searchParams = useSearchParams()[0]

  // Allow /admin/login?next=/admin/tenants style deep links
  const nextPath = searchParams.get('next') || '/admin/tenants'

  useEffect(() => {
    const urlEmail = searchParams.get('email')
    if (urlEmail && !email) setEmail(urlEmail)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (!email.trim() || !password) {
      setError('Email and password are required')
      return
    }
    setIsLoading(true)
    try {
      const { error: signInError } = await signIn(email.trim(), password, 'admin')
      if (signInError) {
        const message =
          typeof signInError === 'string'
            ? signInError
            : signInError?.error || signInError?.message || 'Invalid credentials'
        setError(message)
        toast.error(message)
        setIsLoading(false)
        return
      }
      toast.success('Welcome, Platform Admin!')
      navigate(nextPath, { replace: true })
    } catch (err: any) {
      const message = err?.message || 'Sign in failed. Please try again.'
      setError(message)
      toast.error(message)
    } finally {
      setIsLoading(false)
    }
  }

  const gradient = 'from-blue-500 to-cyan-500'

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 flex items-center justify-center px-4 py-8">
      {/* Background effects (mirrors AuthPage) */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className={`absolute top-20 right-20 w-[500px] h-[500px] bg-gradient-to-br ${gradient} rounded-full mix-blend-multiply filter blur-3xl opacity-20 animate-blob`} />
        <div className="absolute bottom-20 left-20 w-[400px] h-[400px] bg-blue-500 rounded-full mix-blend-multiply filter blur-3xl opacity-10 animate-blob animation-delay-2000" />
      </div>

      <div className="relative z-10 w-full max-w-md">
        {/* Branding */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-3 mb-6">
            <div className={`w-12 h-12 rounded-2xl bg-gradient-to-br ${gradient} p-0.5 shadow-lg shadow-blue-500/20`}>
              <div className="w-full h-full rounded-2xl bg-slate-900 flex items-center justify-center">
                <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={1.5}
                    d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"
                  />
                </svg>
              </div>
            </div>
            <span className="text-2xl font-bold block text-left">
              <span className={`bg-gradient-to-r ${gradient} bg-clip-text text-transparent`}>Sampada</span>
              <span className="text-gray-400 text-sm font-normal block mt-0.5">Platform Administration</span>
            </span>
          </div>
          <h1 className="text-3xl font-bold text-white mb-2">
            Admin <span className={`bg-gradient-to-r ${gradient} bg-clip-text text-transparent`}>Sign In</span>
          </h1>
          <p className="text-gray-400">Manage the tenant hierarchy and key accounts</p>
        </div>

        {/* Form card */}
        <div className="bg-slate-800/60 backdrop-blur-xl border border-slate-700/50 rounded-3xl p-8 shadow-2xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">
                Admin Email <span className="text-red-400">*</span>
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="admin@sampada.hr"
                autoComplete="username"
                className="w-full px-4 py-3 bg-slate-900/60 border border-slate-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">
                Password <span className="text-red-400">*</span>
              </label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete="current-password"
                  className="w-full px-4 py-3 pr-12 bg-slate-900/60 border border-slate-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all"
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
              disabled={isLoading}
              className={`w-full py-3.5 rounded-xl font-semibold text-white bg-gradient-to-r ${gradient} shadow-lg shadow-blue-500/20 hover:opacity-90 transition-all disabled:opacity-60 disabled:cursor-not-allowed`}
            >
              {isLoading ? (
                <span className="inline-flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  Signing in…
                </span>
              ) : (
                'Sign In as Admin'
              )}
            </button>
          </form>

          <p className="mt-6 text-center text-xs text-gray-500">
            Platform scope — key accounts (e.g. SVA) and their clients sign in through the regular
            client portal.
          </p>
        </div>

        <div className="mt-6 text-center">
          <a href="/" className="text-sm text-gray-400 hover:text-white transition-colors">
            ← Back to Sampada
          </a>
        </div>
      </div>
    </div>
  )
}
