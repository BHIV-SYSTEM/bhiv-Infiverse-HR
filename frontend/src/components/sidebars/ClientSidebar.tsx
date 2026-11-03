import { useState, useEffect } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useSidebar } from '../../context/SidebarContext'
import { useAuth } from '../../context/AuthContext'
import { authStorage, clearAuthStorage } from '../../utils/authStorage'
import {
  getClientConnectedRecruiter,
  subscribeClientConnectionEvents,
  checkConnectionHealth,
  fetchTenantInfo,
  type ClientConnectedRecruiter,
} from '../../services/api'
import ApiStatus from '../ApiStatus'

function ClientConnectedRecruiterStatusBlock() {
  const { isCollapsed } = useSidebar()
  const [data, setData] = useState<ClientConnectedRecruiter | null>(null)

  // Initial fetch and SSE subscription
  useEffect(() => {
    let cancelled = false
    const abort = new AbortController()
    const run = async () => {
      try {
        const result = await getClientConnectedRecruiter()
        if (!cancelled) setData(result)
      } catch {
        if (!cancelled) setData({ connected_count: 0, status: 'none' })
      }
    }
    run()
    const unsubscribe = subscribeClientConnectionEvents((ev) => {
      if (cancelled) return
      if (ev.event === 'connected' || ev.event === 'disconnected') {
        const count = typeof ev.connected_count === 'number' ? ev.connected_count : 0
        setData({
          connected_count: count,
          status: count > 0 ? 'connected' : 'none',
        })
      }
    }, abort.signal)
    
    return () => {
      cancelled = true
      unsubscribe()
      abort.abort()
    }
  }, [])

  // Bidirectional health check - runs every 30 seconds ONLY when recruiters are connected
  useEffect(() => {
    const count = data?.connected_count ?? 0
    if (count === 0 || data?.status !== 'connected') {
      return
    }

    let cancelled = false

    const performHealthCheck = async () => {
      if (cancelled) return
      try {
        console.log('[Client Health Check] Starting check - recruiters connected:', count)
        const result = await checkConnectionHealth()
        if (cancelled) return
        
        console.log('[Client Health Check] Result:', result)
        
        // Update connection count if returned by health check
        if (result.healthy && typeof result.connected_count === 'number') {
          setData({
            connected_count: result.connected_count,
            status: result.connected_count > 0 ? 'connected' : 'none',
          })
          console.log('[Client Health Check] Updated connection count:', result.connected_count)
        } else if (!result.healthy) {
          console.warn('[Client Health Check] Connection unhealthy:', result.reason)
        }
      } catch (err) {
        console.error('[Client Health Check] Error:', err)
        // Don't disconnect on network errors - SSE will handle actual disconnects
      }
    }
    
    // Initial health check after 5 seconds (give time for initial setup)
    const timeoutId = window.setTimeout(() => {
      performHealthCheck()
    }, 5000)
    
    // Periodic health check every 30 seconds
    const intervalId = window.setInterval(() => {
      performHealthCheck()
    }, 30000)
    
    return () => {
      cancelled = true
      window.clearTimeout(timeoutId)
      window.clearInterval(intervalId)
    }
  }, [data?.connected_count, data?.status])

  const count = data?.connected_count ?? 0
  if (data?.status === 'none' || count === 0) {
    return (
      <div className={`px-3 pb-2 ${isCollapsed ? 'flex justify-center' : ''}`}>
        <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-gray-100/50 dark:bg-slate-800/50">
          <span className="w-2.5 h-2.5 rounded-full bg-gray-400 shrink-0" />
          <span className="text-xs font-medium text-gray-500 dark:text-gray-400 truncate">No recruiter connected</span>
        </div>
      </div>
    )
  }
  const label = count === 1 ? '1 recruiter connected' : `${count} recruiters connected`
  return (
    <div className={`px-3 pb-2 ${isCollapsed ? 'flex justify-center' : ''}`}>
      <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800/50">
        <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 shrink-0" />
        <span className="text-xs font-medium text-emerald-800 dark:text-emerald-200 truncate" title={label}>
          {label}
        </span>
      </div>
    </div>
  )
}

export default function ClientSidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { isCollapsed, isMobileOpen, closeMobile } = useSidebar()
  const { signOut } = useAuth()
  const [isKeyAccount, setIsKeyAccount] = useState(false)

  // Sub-Clients management is only meaningful for key accounts (e.g. SVA)
  // inside the tenant hierarchy; hidden for everyone else (fail-closed).
  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const info = await fetchTenantInfo()
        if (!cancelled) setIsKeyAccount(info?.is_key_account === true)
      } catch {
        if (!cancelled) setIsKeyAccount(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [])

  const navItems = [
    {
      title: 'Dashboard',
      path: '/client',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
        </svg>
      ),
    },
    {
      title: 'Jobs',
      path: '/client/jobs',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
      ),
    },
    {
      title: 'Match Results',
      path: '/client/matches',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      ),
    },
    {
      title: 'Review Candidates',
      path: '/client/candidates',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" />
        </svg>
      ),
    },
    {
      title: 'Live Recruiter Monitoring',
      path: '/client/live-monitoring',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
        </svg>
      ),
    },
    {
      title: 'Reports & Analytics',
      path: '/client/reports',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
        </svg>
      ),
    },
    ...(isKeyAccount
      ? [
          {
            title: 'Sub-Clients',
            path: '/client/sub-clients',
            icon: (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
              </svg>
            ),
          },
        ]
      : []),
    ...(import.meta.env.VITE_ENABLE_CONTROL_CENTER === 'true'
      ? [
          {
            title: 'Command Center',
            path: '/control',
            icon: (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            ),
          },
        ]
      : []),
  ]

  const isActive = (path: string) => {
    if (path === '/client') {
      return location.pathname === '/client'
    }
    return location.pathname.startsWith(path)
  }

  const handleLogout = async () => {
    try {
      await signOut()
      clearAuthStorage()
      navigate('/', { replace: true })
      window.location.href = '/'
    } catch (error) {
      console.error('Logout error:', error)
      clearAuthStorage()
      navigate('/', { replace: true })
      window.location.href = '/'
    }
  }

  const handleLinkClick = () => {
    // Close mobile menu when link is clicked
    if (window.innerWidth < 1024) {
      closeMobile()
    }
  }

  return (
    <aside 
      className={`fixed left-0 top-16 h-[calc(100vh-4rem)] bg-white dark:bg-slate-900 border-r border-gray-200 dark:border-slate-800 overflow-y-auto transition-all duration-300 z-50 ${
        // Mobile: show/hide based on isMobileOpen
        isMobileOpen ? 'translate-x-0' : '-translate-x-full'
      } lg:translate-x-0 ${
        // Desktop: width based on isCollapsed
        isCollapsed ? 'lg:w-20' : 'lg:w-64'
      } w-64`}
    >
      {/* Navigation */}
      <nav className="p-3 space-y-1 pb-32">
        {navItems.map((item) => (
          <Link
            key={item.path}
            to={item.path}
            onClick={handleLinkClick}
            title={isCollapsed ? item.title : undefined}
            className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-3'} px-3 py-3 rounded-xl transition-all duration-200 ${
              isActive(item.path)
                ? 'bg-gradient-to-r from-purple-500 to-pink-500 text-white shadow-lg shadow-purple-500/30'
                : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-slate-800'
            }`}
          >
            <span className="flex-shrink-0">{item.icon}</span>
            {!isCollapsed && <span className="font-medium truncate">{item.title}</span>}
          </Link>
        ))}
      </nav>

      {/* User Info & Logout */}
      <div className="absolute bottom-0 left-0 right-0 border-t border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        {/* API Status */}
        <div className={`px-3 pt-3 ${isCollapsed ? 'flex justify-center' : ''}`}>
          <ApiStatus />
        </div>
        {/* Connected Recruiter (mirrors recruiter sidebar's client company block) */}
        <ClientConnectedRecruiterStatusBlock />
        {/* User Details */}
        <div className={`p-3 ${isCollapsed ? 'flex justify-center' : ''}`}>
          <div className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-3'}`}>
            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-purple-500 to-pink-500 flex items-center justify-center text-white font-bold text-lg flex-shrink-0">
              {authStorage.getItem('user_name')?.charAt(0).toUpperCase() || 'C'}
            </div>
            {!isCollapsed && (
              <div className="overflow-hidden">
                <p className="font-semibold text-gray-900 dark:text-white truncate text-sm">
                  {authStorage.getItem('user_name') || 'Client'}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                  {authStorage.getItem('user_email') || 'client@sampada.hr'}
                </p>
              </div>
            )}
          </div>
        </div>
        {/* Logout Button */}
        <div className="p-3 pt-0">
          <button
            onClick={handleLogout}
            title={isCollapsed ? 'Logout' : undefined}
            className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-2'} w-full px-2.5 py-2 rounded-lg bg-red-500/10 hover:bg-red-500 text-red-600 hover:text-white border border-red-200 dark:border-red-800 hover:border-red-500 transition-all duration-200`}
          >
            <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
            </svg>
            {!isCollapsed && <span className="text-sm font-medium">Logout</span>}
          </button>
        </div>
      </div>
    </aside>
  )
}
