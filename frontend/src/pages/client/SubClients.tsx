import { useCallback, useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import {
  fetchTenantInfo,
  fetchTenantSubClients,
  createTenantSubClient,
  type SubClient,
  type TenantClientAccount,
  type TenantInfo,
} from '../../services/api'

interface CreatedCredentials {
  client_id: string
  client_code?: string
  connection_id?: string
  email: string
  password?: string
  generated_password?: boolean
}

/**
 * Key account (e.g. SVA) view of its own tenanted subset: list and create the
 * clients inside it. Plain clients (not key accounts) see an honest empty
 * state — the backend rejects the API calls for them with 403.
 */
export default function SubClients() {
  const [info, setInfo] = useState<TenantInfo | null>(null)
  const [subClients, setSubClients] = useState<SubClient[]>([])
  const [visibleIds, setVisibleIds] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [created, setCreated] = useState<CreatedCredentials | null>(null)

  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [company, setCompany] = useState('')

  const load = useCallback(async () => {
    try {
      const tenantInfo = await fetchTenantInfo()
      setInfo(tenantInfo)
      if (tenantInfo.is_key_account) {
        const sub = await fetchTenantSubClients()
        setSubClients(sub.sub_clients || [])
        setVisibleIds(sub.visible_client_ids || [])
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to load tenant information')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim() || !email.trim()) {
      toast.error('Name and login email are required')
      return
    }
    setSubmitting(true)
    try {
      const result = await createTenantSubClient({
        name: name.trim(),
        email: email.trim(),
        ...(password ? { password } : {}),
        ...(company.trim() ? { company_name: company.trim() } : {}),
      })
      const account = result.client_account || {}
      setCreated({
        client_id: account.client_id,
        client_code: account.client_code,
        connection_id: account.connection_id,
        email: account.email,
        password: account.password,
        generated_password: account.generated_password,
      })
      toast.success(`Client "${result.tenant?.name}" created inside your subset`)
      setName('')
      setEmail('')
      setPassword('')
      setCompany('')
      setShowForm(false)
      await load()
    } catch (err: any) {
      toast.error(
        err?.response?.data?.detail || err?.response?.data?.error || 'Failed to create client'
      )
    } finally {
      setSubmitting(false)
    }
  }

  const copyText = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.success(`${label} copied`)
    } catch {
      toast.error('Clipboard unavailable')
    }
  }

  const inputCls =
    'w-full px-3 py-2.5 bg-white dark:bg-slate-900 border border-gray-300 dark:border-slate-700 rounded-lg text-gray-900 dark:text-white text-sm placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-purple-500 transition-all'

  const accountsOf = (sub: SubClient): TenantClientAccount[] =>
    sub.client_accounts?.length
      ? sub.client_accounts
      : sub.client_ids.map((id) => ({ client_id: id }))

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <div className="w-10 h-10 border-4 border-purple-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const isKeyAccount = info?.is_key_account === true

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Sub-Clients</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {isKeyAccount
              ? `Your tenanted subset — ${info?.name || ''} (${info?.path || ''})`
              : 'Clients inside your organization'}
          </p>
        </div>
        {isKeyAccount && (
          <button
            onClick={() => setShowForm((v) => !v)}
            className="px-4 py-2.5 rounded-xl bg-gradient-to-r from-purple-500 to-pink-500 text-white text-sm font-medium shadow-lg shadow-purple-500/20 hover:opacity-90 transition-opacity"
          >
            {showForm ? 'Cancel' : '+ New client'}
          </button>
        )}
      </div>

      {!isKeyAccount ? (
        <div className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-2xl p-10 text-center">
          <p className="text-gray-600 dark:text-gray-300 font-medium">
            This area is available for key accounts.
          </p>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
            Your account sees only its own data. Key accounts (parent organizations) manage the
            clients inside their subset from this page.
          </p>
        </div>
      ) : (
        <>
          {/* Isolation summary */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-xl px-4 py-3">
              <p className="text-xs text-gray-500 dark:text-gray-400">Clients in subset</p>
              <p className="text-xl font-bold text-gray-900 dark:text-white">{subClients.length}</p>
            </div>
            <div className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-xl px-4 py-3">
              <p className="text-xs text-gray-500 dark:text-gray-400">Accounts you can read</p>
              <p className="text-xl font-bold text-gray-900 dark:text-white">{visibleIds.length}</p>
            </div>
            <div className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-xl px-4 py-3">
              <p className="text-xs text-gray-500 dark:text-gray-400">Isolation</p>
              <p className="text-xl font-bold text-emerald-500">Enforced</p>
            </div>
          </div>

          {/* Created credentials */}
          {created && (
            <div className="bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-300 dark:border-emerald-800 rounded-2xl p-4 space-y-2">
              <div className="flex items-center justify-between">
                <p className="font-semibold text-emerald-700 dark:text-emerald-300 text-sm">
                  Client created — share these credentials once
                </p>
                <button
                  onClick={() => setCreated(null)}
                  className="text-emerald-600 dark:text-emerald-400 text-xs"
                >
                  Dismiss
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                <div className="flex items-center justify-between gap-2 bg-white dark:bg-slate-900 rounded-lg px-3 py-2 border border-emerald-200 dark:border-emerald-900">
                  <span className="text-gray-500">client_id</span>
                  <code className="font-mono text-xs">{created.client_id}</code>
                  <button
                    onClick={() => copyText(created.client_id || '', 'client_id')}
                    className="text-purple-500 text-xs"
                  >
                    Copy
                  </button>
                </div>
                {created.connection_id && (
                  <div className="flex items-center justify-between gap-2 bg-white dark:bg-slate-900 rounded-lg px-3 py-2 border border-emerald-200 dark:border-emerald-900">
                    <span className="text-gray-500">connection_id</span>
                    <code className="font-mono text-xs">{created.connection_id}</code>
                    <button
                      onClick={() => copyText(created.connection_id!, 'connection_id')}
                      className="text-purple-500 text-xs"
                    >
                      Copy
                    </button>
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 bg-white dark:bg-slate-900 rounded-lg px-3 py-2 border border-emerald-200 dark:border-emerald-900">
                  <span className="text-gray-500">email</span>
                  <code className="font-mono text-xs">{created.email}</code>
                </div>
                {created.password && (
                  <div className="flex items-center justify-between gap-2 bg-white dark:bg-slate-900 rounded-lg px-3 py-2 border border-emerald-200 dark:border-emerald-900">
                    <span className="text-gray-500">password</span>
                    <code className="font-mono text-xs">{created.password}</code>
                    <button
                      onClick={() => copyText(created.password!, 'password')}
                      className="text-purple-500 text-xs"
                    >
                      Copy
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Create form */}
          {showForm && (
            <form
              onSubmit={handleCreate}
              className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-2xl p-5 grid grid-cols-1 sm:grid-cols-2 gap-3"
            >
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Client company name *"
                className={inputCls}
                required
              />
              <input
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                placeholder="Display name (optional)"
                className={inputCls}
              />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Login email *"
                className={inputCls}
                required
              />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Password (auto-generated if empty)"
                className={inputCls}
              />
              <div className="sm:col-span-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowForm(false)}
                  className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-slate-700 text-gray-700 dark:text-gray-200 text-sm"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-4 py-2 rounded-lg bg-gradient-to-r from-purple-500 to-pink-500 text-white text-sm font-medium disabled:opacity-60"
                >
                  {submitting ? 'Creating…' : 'Create client'}
                </button>
              </div>
            </form>
          )}

          {/* List */}
          {subClients.length === 0 ? (
            <div className="bg-white dark:bg-slate-800 border border-dashed border-gray-300 dark:border-slate-600 rounded-2xl p-10 text-center">
              <p className="text-gray-500 dark:text-gray-400">
                No clients inside your subset yet.
              </p>
              <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
                Create one to give them their own isolated portal inside your account.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {subClients.map((sub) => (
                <div
                  key={sub.tenant_id}
                  className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-xl px-4 py-3 flex flex-wrap items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-semibold text-gray-900 dark:text-white text-sm truncate">
                        {sub.name}
                      </p>
                      <span
                        className={`text-[10px] px-2 py-0.5 rounded-full ${
                          sub.status === 'active'
                            ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                            : 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
                        }`}
                      >
                        {sub.status}
                      </span>
                      <span className="text-[11px] font-mono text-gray-400">{sub.tenant_id}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-2">
                      {accountsOf(sub).map((acc) => (
                        <span
                          key={acc.client_id}
                          className="text-xs bg-gray-100 dark:bg-slate-900 border border-gray-200 dark:border-slate-700 rounded px-2 py-1 font-mono"
                          title={acc.email || acc.client_id}
                        >
                          {acc.company_name || acc.client_id}
                        </span>
                      ))}
                    </div>
                  </div>
                  <span className="text-xs text-gray-400 font-mono">{sub.path}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
