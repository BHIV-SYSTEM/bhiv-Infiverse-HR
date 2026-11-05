import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import { useAuth } from '../../context/AuthContext'
import {
  fetchAdminTenants,
  fetchAdminClients,
  createAdminTenant,
  updateAdminTenant,
  linkTenantClient,
  type AdminTenantsResponse,
  type AdminClientAccount,
  type TenantClientAccount,
  type TenantNode,
} from '../../services/api'

/**
 * Platform admin: tenant hierarchy management.
 *
 * platform (BHIV/Sampada) -> key accounts (e.g. SVA) -> accounts (SVA's
 * clients). Every action here is platform-scoped; key accounts manage their
 * own subset from the client portal (Sub-Clients page).
 */
export default function AdminTenantsPage() {
  const { signOut } = useAuth()
  const navigate = useNavigate()
  const [data, setData] = useState<AdminTenantsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // Create key-account form
  const [newName, setNewName] = useState('')
  const [newTenantId, setNewTenantId] = useState('')
  const [provisionMode, setProvisionMode] = useState<'none' | 'link' | 'new'>('none')
  const [newClientId, setNewClientId] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newCompany, setNewCompany] = useState('')

  // Inline "add client under key account" state: key account tenant_id -> form fields
  const [addUnder, setAddUnder] = useState<string | null>(null)
  const [childName, setChildName] = useState('')
  const [childEmail, setChildEmail] = useState('')
  const [childPassword, setChildPassword] = useState('')
  const [childCompany, setChildCompany] = useState('')

  // Inline "link existing CP account" state
  const [linkFor, setLinkFor] = useState<string | null>(null)
  const [linkClientId, setLinkClientId] = useState('')
  const [linkPicked, setLinkPicked] = useState(false)
  const [linkFocused, setLinkFocused] = useState(false)

  // All Client-Portal accounts — discovery panel so the owner never opens MongoDB
  const [clients, setClients] = useState<AdminClientAccount[]>([])

  const load = useCallback(async () => {
    try {
      const result = await fetchAdminTenants()
      setData(result)
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to load tenants')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadClients = useCallback(async () => {
    try {
      const result = await fetchAdminClients()
      setClients(result.clients || [])
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to load client accounts')
    }
  }, [])

  useEffect(() => {
    load()
    loadClients()
  }, [load, loadClients])

  const refreshAll = useCallback(async () => {
    await Promise.all([load(), loadClients()])
  }, [load, loadClients])

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`Copied ${text}`)
    } catch {
      toast.error('Could not copy to clipboard')
    }
  }

  const errorMessage = (err: any, fallback: string) =>
    err?.response?.data?.detail || err?.response?.data?.error || err?.message || fallback

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) {
      toast.error('Name is required')
      return
    }
    setSubmitting(true)
    try {
      const payload: Parameters<typeof createAdminTenant>[0] = {
        name: newName.trim(),
        level: 'key_account',
      }
      if (newTenantId.trim()) payload.tenant_id = newTenantId.trim().toLowerCase()
      if (provisionMode === 'link' && newClientId.trim()) {
        payload.client_id = newClientId.trim()
      } else if (provisionMode === 'new' && newEmail.trim()) {
        payload.provision_client = {
          email: newEmail.trim(),
          company_name: newCompany.trim() || newName.trim(),
        }
        if (newPassword) payload.provision_client.password = newPassword
      }
      const result = await createAdminTenant(payload)
      toast.success(`Key account "${result.tenant?.name}" created`)
      setShowCreate(false)
      setNewName('')
      setNewTenantId('')
      setNewClientId('')
      setNewEmail('')
      setNewPassword('')
      setNewCompany('')
      setProvisionMode('none')
      await refreshAll()
    } catch (err: any) {
      toast.error(errorMessage(err, 'Failed to create key account'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleAddClient = async (e: React.FormEvent, keyAccountId: string) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      const result = await createAdminTenant({
        name: childName.trim(),
        level: 'account',
        parent_tenant_id: keyAccountId,
        ...(childEmail.trim()
          ? {
              provision_client: {
                email: childEmail.trim(),
                company_name: childCompany.trim() || childName.trim(),
                ...(childPassword ? { password: childPassword } : {}),
              },
            }
          : {}),
      })
      toast.success(`Client "${result.tenant?.name}" created under ${keyAccountId}`)
      setAddUnder(null)
      setChildName('')
      setChildEmail('')
      setChildPassword('')
      setChildCompany('')
      await refreshAll()
    } catch (err: any) {
      toast.error(errorMessage(err, 'Failed to create client'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleLinkClient = async (e: React.FormEvent, tenantId: string) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await linkTenantClient(tenantId, linkClientId.trim())
      toast.success(`Client account linked to ${tenantId}`)
      setLinkFor(null)
      setLinkClientId('')
      setLinkPicked(false)
      await refreshAll()
    } catch (err: any) {
      toast.error(errorMessage(err, 'Failed to link client account'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleToggleStatus = async (node: TenantNode) => {
    const next = node.status === 'active' ? 'suspended' : 'active'
    try {
      await updateAdminTenant(node.tenant_id, { status: next })
      toast.success(`${node.tenant_id} ${next === 'active' ? 'activated' : 'suspended'}`)
      await refreshAll()
    } catch (err: any) {
      toast.error(errorMessage(err, 'Failed to update status'))
    }
  }

  const handleLogout = async () => {
    await signOut()
    navigate('/admin/login', { replace: true })
  }

  const inputCls =
    'w-full px-3 py-2.5 bg-slate-900/70 border border-slate-700 rounded-lg text-white text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all'

  const renderClientAccounts = (node: TenantNode) => {
    if (!node.client_accounts?.length && !node.client_ids.length) {
      return <p className="text-xs text-gray-500">No Client-Portal account linked</p>
    }
    const accounts: TenantClientAccount[] = node.client_accounts?.length
      ? node.client_accounts
      : node.client_ids.map((id) => ({ client_id: id }))
    return (
      <div className="space-y-1">
        {accounts.map((acc) => (
          <div
            key={acc.client_id}
            className="flex items-center justify-between gap-2 text-xs bg-slate-900/50 border border-slate-700/50 rounded-lg px-3 py-2"
          >
            <div className="min-w-0">
              <p className="text-gray-300 font-medium truncate">
                {acc.company_name || acc.client_id}
              </p>
              <p className="text-gray-500 truncate">{acc.email || `id: ${acc.client_id}`}</p>
            </div>
            <span className="text-gray-500 font-mono shrink-0">{acc.client_id}</span>
          </div>
        ))}
      </div>
    )
  }

  const renderAccount = (node: TenantNode) => (
    <div
      key={node.tenant_id}
      className="flex items-start gap-3 bg-slate-900/40 border border-slate-700/60 rounded-xl px-4 py-3"
    >
      <span
        className={`mt-1 w-2 h-2 rounded-full shrink-0 ${
          node.status === 'active' ? 'bg-emerald-400' : 'bg-amber-400'
        }`}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-semibold text-white text-sm truncate">{node.name}</p>
          <span className="text-[10px] uppercase tracking-wide bg-slate-700/70 text-gray-300 px-2 py-0.5 rounded-full">
            {node.level}
          </span>
          <span
            className={`text-[10px] px-2 py-0.5 rounded-full ${
              node.status === 'active'
                ? 'bg-emerald-500/15 text-emerald-400'
                : 'bg-amber-500/15 text-amber-400'
            }`}
          >
            {node.status}
          </span>
          <span className="text-[11px] text-gray-500 font-mono">{node.tenant_id}</span>
        </div>
        <div className="mt-2">{renderClientAccounts(node)}</div>
      </div>
      <div className="flex flex-col gap-1.5 shrink-0">
        <button
          onClick={() => handleToggleStatus(node)}
          className="text-[11px] px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-gray-300 border border-slate-700 transition-colors"
        >
          {node.status === 'active' ? 'Suspend' : 'Activate'}
        </button>
        <button
          onClick={() => {
            setLinkFor(linkFor === node.tenant_id ? null : node.tenant_id)
            setLinkClientId('')
            setLinkPicked(false)
            setAddUnder(null)
          }}
          className="text-[11px] px-2.5 py-1 rounded-md bg-slate-800 hover:bg-slate-700 text-gray-300 border border-slate-700 transition-colors"
        >
          Link CP account
        </button>
      </div>
    </div>
  )

  const renderKeyAccount = (node: TenantNode) => (
    <div
      key={node.tenant_id}
      className="bg-slate-800/50 border border-slate-700 rounded-2xl p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-bold text-white truncate">{node.name}</h3>
            <span className="text-[10px] uppercase tracking-wide bg-blue-500/15 text-blue-300 px-2 py-0.5 rounded-full">
              key account
            </span>
            <span
              className={`text-[10px] px-2 py-0.5 rounded-full ${
                node.status === 'active'
                  ? 'bg-emerald-500/15 text-emerald-400'
                  : 'bg-amber-500/15 text-amber-400'
              }`}
            >
              {node.status}
            </span>
            <span className="text-xs text-gray-500 font-mono">{node.tenant_id}</span>
          </div>
          <p className="text-xs text-gray-500 mt-1 font-mono">{node.path}</p>
          <div className="mt-3 max-w-xl">{renderClientAccounts(node)}</div>
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            onClick={() => handleToggleStatus(node)}
            className="text-xs px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-700 text-gray-300 border border-slate-700 transition-colors"
          >
            {node.status === 'active' ? 'Suspend' : 'Activate'}
          </button>
          <button
            onClick={() => {
              setLinkFor(linkFor === node.tenant_id ? null : node.tenant_id)
              setLinkClientId('')
              setLinkPicked(false)
              setAddUnder(null)
            }}
            className="text-xs px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-700 text-gray-300 border border-slate-700 transition-colors"
          >
            Link CP account
          </button>
          <button
            onClick={() => {
              setAddUnder(addUnder === node.tenant_id ? null : node.tenant_id)
              setLinkFor(null)
            }}
            className="text-xs px-3 py-1.5 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-500 text-white hover:opacity-90 transition-opacity"
          >
            + Add client
          </button>
        </div>
      </div>

      {linkFor === node.tenant_id && (() => {
        const q = linkClientId.trim().toLowerCase()
        const pickerAccounts = clients.filter(
          (c) =>
            !c.linked_tenant_id &&
            (!q ||
              `${c.client_id} ${c.company_name} ${c.email}`.toLowerCase().includes(q))
        )
        const showPicker = linkFocused && !linkPicked
        return (
          <form
            onSubmit={(e) => handleLinkClient(e, node.tenant_id)}
            className="mt-4 flex flex-col sm:flex-row gap-2 bg-slate-900/60 border border-slate-700 rounded-xl p-3 relative"
          >
            <div className="relative flex-1">
              <input
                value={linkClientId}
                onChange={(e) => {
                  setLinkClientId(e.target.value)
                  setLinkPicked(false)
                }}
                onFocus={() => setLinkFocused(true)}
                onBlur={() => setLinkFocused(false)}
                placeholder="Search CP accounts, or type an existing client_id *"
                className={inputCls}
                required
              />
              {linkPicked && (
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-300 pointer-events-none">
                  selected
                </span>
              )}
              {showPicker && (
                <div className="absolute z-20 left-0 right-0 mt-1 max-h-60 overflow-auto bg-slate-900 border border-slate-700 rounded-lg shadow-2xl">
                  {pickerAccounts.length === 0 ? (
                    <p className="px-3 py-2.5 text-xs text-gray-500">
                      {clients.filter((c) => !c.linked_tenant_id).length === 0
                        ? 'All CP accounts are already linked.'
                        : 'No unlinked account matches — keep typing to enter a client_id manually.'}
                    </p>
                  ) : (
                    pickerAccounts.map((acc) => (
                      <button
                        key={acc.client_id}
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          setLinkClientId(acc.client_id)
                          setLinkPicked(true)
                        }}
                        className="w-full text-left px-3 py-2 hover:bg-slate-800 transition-colors border-b border-slate-800/60 last:border-b-0"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <p className="text-xs font-medium text-gray-200 truncate">
                              {acc.company_name || acc.client_id}
                            </p>
                            <p className="text-[11px] text-gray-500 truncate">
                              {acc.email || 'no email'}
                            </p>
                          </div>
                          <span className="text-[11px] font-mono text-blue-300 shrink-0">
                            {acc.client_id}
                          </span>
                        </div>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-60 shrink-0"
            >
              Link
            </button>
          </form>
        )
      })()}

      {addUnder === node.tenant_id && (
        <form
          onSubmit={(e) => handleAddClient(e, node.tenant_id)}
          className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 bg-slate-900/60 border border-slate-700 rounded-xl p-4"
        >
          <input
            value={childName}
            onChange={(e) => setChildName(e.target.value)}
            placeholder="Client company name *"
            className={inputCls}
            required
          />
          <input
            value={childCompany}
            onChange={(e) => setChildCompany(e.target.value)}
            placeholder="Display company (optional)"
            className={inputCls}
          />
          <input
            type="email"
            value={childEmail}
            onChange={(e) => setChildEmail(e.target.value)}
            placeholder="Client login email (provisions CP account)"
            className={inputCls}
          />
          <input
            type="password"
            value={childPassword}
            onChange={(e) => setChildPassword(e.target.value)}
            placeholder="Password (auto-generated if empty)"
            className={inputCls}
          />
          <div className="sm:col-span-2 flex gap-2 justify-end">
            <button
              type="button"
              onClick={() => setAddUnder(null)}
              className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-gray-300 text-sm border border-slate-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-500 text-white text-sm font-medium disabled:opacity-60"
            >
              {submitting ? 'Creating…' : 'Create client'}
            </button>
          </div>
        </form>
      )}

      {node.children && node.children.length > 0 && (
        <div className="mt-4 space-y-3">
          <p className="text-xs uppercase tracking-wide text-gray-500">
            Clients inside {node.name}'s subset ({node.children.length})
          </p>
          <div className="space-y-3">{node.children.map(renderAccount)}</div>
        </div>
      )}
    </div>
  )

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-gray-100">
      {/* Header */}
      <header className="border-b border-slate-700/70 bg-slate-900/70 backdrop-blur sticky top-0 z-20">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold text-white">Tenant Hierarchy</h1>
            <p className="text-xs text-gray-400">
              platform → key accounts → clients · account isolation enforced per subset
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => refreshAll()}
              className="px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm text-gray-300"
            >
              Refresh
            </button>
            <button
              onClick={() => navigate('/admin/ndas')}
              className="px-3 py-2 rounded-lg bg-emerald-600/15 hover:bg-emerald-600/30 border border-emerald-600/50 text-sm text-emerald-300"
            >
              NDA Approvals
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
            { label: 'Key accounts', value: data?.counts.key_accounts ?? '—' },
            { label: 'Clients (accounts)', value: data?.counts.accounts ?? '—' },
            { label: 'Linked CP logins', value: data?.counts.linked_clients ?? '—' },
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

        {/* Create key account */}
        <div className="mb-6">
          {!showCreate ? (
            <button
              onClick={() => setShowCreate(true)}
              className="px-4 py-2.5 rounded-xl bg-gradient-to-r from-blue-500 to-cyan-500 text-white font-medium shadow-lg shadow-blue-500/20 hover:opacity-90 transition-opacity"
            >
              + New key account
            </button>
          ) : (
            <form
              onSubmit={handleCreate}
              className="bg-slate-800/60 border border-slate-700 rounded-2xl p-5 space-y-3"
            >
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-white">New key account</h3>
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="text-gray-400 hover:text-white text-sm"
                >
                  ✕
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="Company name (e.g. SVA) *"
                  className={inputCls}
                  required
                />
                <input
                  value={newTenantId}
                  onChange={(e) => setNewTenantId(e.target.value)}
                  placeholder="tenant_id (auto-generated if empty)"
                  className={`${inputCls} font-mono`}
                />
              </div>
              <div className="flex flex-wrap gap-3 text-sm">
                <label className="flex items-center gap-2 text-gray-300">
                  <input
                    type="radio"
                    checked={provisionMode === 'none'}
                    onChange={() => setProvisionMode('none')}
                  />
                  No CP login yet
                </label>
                <label className="flex items-center gap-2 text-gray-300">
                  <input
                    type="radio"
                    checked={provisionMode === 'link'}
                    onChange={() => setProvisionMode('link')}
                  />
                  Link existing CP account
                </label>
                <label className="flex items-center gap-2 text-gray-300">
                  <input
                    type="radio"
                    checked={provisionMode === 'new'}
                    onChange={() => setProvisionMode('new')}
                  />
                  Provision new CP login
                </label>
              </div>
              {provisionMode === 'link' && (
                <input
                  value={newClientId}
                  onChange={(e) => setNewClientId(e.target.value)}
                  placeholder="Existing client_id from their shared CP details *"
                  className={`${inputCls} font-mono`}
                  required
                />
              )}
              {provisionMode === 'new' && (
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <input
                    type="email"
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    placeholder="Login email *"
                    className={inputCls}
                    required
                  />
                  <input
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="Password (auto if empty)"
                    className={inputCls}
                  />
                  <input
                    value={newCompany}
                    onChange={(e) => setNewCompany(e.target.value)}
                    placeholder="Company display name"
                    className={inputCls}
                  />
                </div>
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-gray-300 text-sm"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-4 py-2 rounded-lg bg-gradient-to-r from-blue-500 to-cyan-500 text-white font-medium text-sm disabled:opacity-60"
                >
                  {submitting ? 'Creating…' : 'Create key account'}
                </button>
              </div>
            </form>
          )}
        </div>

        {/* Client Accounts discovery panel — every CP login + client_id, no MongoDB needed */}
        <div className="mb-6 bg-slate-800/60 border border-slate-700 rounded-2xl p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <div>
              <h3 className="font-semibold text-white">Client Accounts ({clients.length})</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                Every Client-Portal login and its unique client_id — copy to link, no database access
              </p>
            </div>
            <button
              onClick={() => loadClients()}
              className="px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-700 border border-slate-700 text-xs text-gray-300"
            >
              Refresh
            </button>
          </div>
          {clients.length === 0 ? (
            <p className="text-xs text-gray-500 py-2">
              No Client-Portal accounts yet — use “Provision new CP login” when creating a tenant.
            </p>
          ) : (
            <div className="space-y-1.5">
              {clients.map((acc) => (
                <div
                  key={acc.client_id}
                  className="flex flex-wrap items-center justify-between gap-2 bg-slate-900/50 border border-slate-700/50 rounded-lg px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-gray-200 font-medium truncate">
                      {acc.company_name || acc.client_id}
                    </p>
                    <p className="text-xs text-gray-500 truncate">
                      {acc.email || '—'}
                      {acc.status && acc.status !== 'active' ? ` · ${acc.status}` : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span
                      className={`text-[10px] px-2 py-0.5 rounded-full ${
                        acc.linked_tenant_id
                          ? 'bg-emerald-500/15 text-emerald-400'
                          : 'bg-amber-500/15 text-amber-400'
                      }`}
                      title={
                        acc.linked_tenant_id
                          ? `Linked to tenant ${acc.linked_tenant_id}`
                          : 'Not linked to any tenant'
                      }
                    >
                      {acc.linked_tenant_id ? `Linked → ${acc.linked_tenant_id}` : 'Unlinked'}
                    </span>
                    <button
                      onClick={() => copyToClipboard(acc.client_id)}
                      className="flex items-center gap-1.5 text-[11px] font-mono px-2 py-1 rounded-md bg-slate-800 hover:bg-slate-700 border border-slate-700 text-blue-300 transition-colors"
                      title="Copy client_id"
                    >
                      {acc.client_id}
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
                        />
                      </svg>
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Tree */}
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="w-10 h-10 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : !data || data.tree.length === 0 ? (
          <div className="bg-slate-800/40 border border-dashed border-slate-700 rounded-2xl p-10 text-center">
            <p className="text-gray-400">No tenants yet.</p>
            <p className="text-sm text-gray-500 mt-1">
              Create your first key account (e.g. SVA) to start the hierarchy.
            </p>
          </div>
        ) : (
          <div className="space-y-6">{data.tree.map(renderKeyAccount)}</div>
        )}
      </main>
    </div>
  )
}
