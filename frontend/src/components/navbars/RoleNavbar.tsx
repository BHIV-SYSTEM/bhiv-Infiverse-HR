import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTheme } from '../../context/ThemeContext'
import { useSidebar } from '../../context/SidebarContext'
import { useAuth } from '../../context/AuthContext'
import { authStorage, clearAuthStorage } from '../../utils/authStorage'
import {
  getPortalNotifications,
  markAllPortalNotificationsRead,
  markPortalNotificationRead,
  type PortalNotification,
} from '../../services/api'

interface RoleNavbarProps {
  role: 'candidate' | 'recruiter' | 'client'
}

const roleConfig = {
  candidate: {
    gradient: 'from-blue-500 to-cyan-500',
    title: 'Candidate Portal',
    homePath: '/candidate/dashboard',
    profilePath: '/candidate/profile',
    icon: (
      <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
      </svg>
    ),
  },
  recruiter: {
    gradient: 'from-emerald-500 to-teal-500',
    title: 'Recruiter Console',
    homePath: '/recruiter',
    profilePath: '/recruiter/profile',
    icon: (
      <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
      </svg>
    ),
  },
  client: {
    gradient: 'from-purple-500 to-pink-500',
    title: 'Client Dashboard',
    homePath: '/client',
    profilePath: '/client/profile',
    icon: (
      <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
      </svg>
    ),
  },
}

export default function RoleNavbar({ role }: RoleNavbarProps) {
  const { theme, toggleTheme } = useTheme()
  const { isCollapsed, toggleSidebar, toggleMobile } = useSidebar()
  const { signOut, userName, isGuest } = useAuth()
  const navigate = useNavigate()
  const config = roleConfig[role]
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [notifications, setNotifications] = useState<PortalNotification[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [loadingNotifications, setLoadingNotifications] = useState(false)
  const notificationsRef = useRef<HTMLDivElement | null>(null)
  const notificationsSignatureRef = useRef<string>('')

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

  const loadNotifications = async (silent = true) => {
    try {
      if (!silent) setLoadingNotifications(true)
      const data = await getPortalNotifications(20)
      const nextNotifications = Array.isArray(data.notifications) ? data.notifications : []
      const nextUnread = Number(data.unread_count || 0)
      setNotifications(nextNotifications)
      setUnreadCount(nextUnread)

      const latestId = nextNotifications[0]?.id || ''
      const signature = `${role}:${latestId}:${nextUnread}:${nextNotifications.length}`
      if (notificationsSignatureRef.current !== signature) {
        notificationsSignatureRef.current = signature
        window.dispatchEvent(
          new CustomEvent('portal-notifications-updated', {
            detail: {
              role,
              unreadCount: nextUnread,
              latestNotificationId: latestId,
            },
          })
        )
      }
    } catch {
      // Silent fail: bell is auxiliary UI
    } finally {
      if (!silent) setLoadingNotifications(false)
    }
  }

  useEffect(() => {
    loadNotifications(true)
    const interval = setInterval(() => {
      loadNotifications(true)
    }, 20000)
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        loadNotifications(true)
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  useEffect(() => {
    const onClickOutside = (event: MouseEvent) => {
      if (!notificationsRef.current) return
      if (!notificationsRef.current.contains(event.target as Node)) {
        setNotificationsOpen(false)
      }
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [])

  const handleOpenNotifications = async () => {
    setNotificationsOpen(prev => !prev)
    await loadNotifications(false)
  }

  const handleMarkAllNotificationsRead = async () => {
    try {
      await markAllPortalNotificationsRead()
      setNotifications(prev => prev.map(n => ({ ...n, is_read: true })))
      setUnreadCount(0)
    } catch {
      // no-op
    }
  }

  const handleMarkNotificationRead = async (notificationId: string) => {
    setNotifications(prev => prev.map(n => (n.id === notificationId ? { ...n, is_read: true } : n)))
    setUnreadCount(prev => Math.max(0, prev - 1))
    try {
      await markPortalNotificationRead(notificationId)
    } catch {
      // no-op
    }
  }

  return (
    <nav className="fixed top-0 left-0 right-0 h-16 backdrop-blur-xl bg-white/95 dark:bg-slate-900/95 border-b border-gray-200/50 dark:border-slate-700/50 z-50 shadow-sm transition-all duration-300">
      <div className="flex items-center justify-between h-full px-3 sm:px-4">
        {/* Logo & Mobile Menu Button */}
        <div className="flex items-center gap-2">
          {/* Mobile Menu Button */}
          <button
            onClick={toggleMobile}
            className="lg:hidden p-2 hover:bg-gray-100 dark:hover:bg-slate-800 rounded-lg transition-colors mr-1"
            title="Toggle Menu"
          >
            <svg className="w-6 h-6 text-gray-700 dark:text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          {/* Desktop Sidebar Toggle */}
          <button
            onClick={toggleSidebar}
            className="hidden lg:flex items-center space-x-3 group"
            title={isCollapsed ? 'Expand Sidebar' : 'Collapse Sidebar'}
          >
            <div className={`w-10 h-10 bg-gradient-to-br ${config.gradient} rounded-xl flex items-center justify-center shadow-lg group-hover:scale-110 transition-all duration-300 ${isCollapsed ? 'rotate-180' : ''}`}>
              {config.icon}
            </div>
            <div className="hidden sm:block text-left">
              <span className={`text-xl font-bold bg-gradient-to-r ${config.gradient} bg-clip-text text-transparent`}>
                Sampada
              </span>
              <p className="text-xs text-gray-500 dark:text-gray-400 font-medium">
                HR Recruitment System
              </p>
            </div>
          </button>
          {/* Mobile Logo Only */}
          <div className="lg:hidden flex items-center gap-2">
            <div className={`w-8 h-8 bg-gradient-to-br ${config.gradient} rounded-lg flex items-center justify-center shadow-md`}>
              {config.icon}
            </div>
            <span className={`text-lg font-bold bg-gradient-to-r ${config.gradient} bg-clip-text text-transparent`}>
              Sampada
            </span>
          </div>
        </div>
        
        {/* Right Side Actions */}
        <div className="flex items-center space-x-1 sm:space-x-2 lg:space-x-3">
          {/* Theme Toggle */}
          <button 
            onClick={toggleTheme}
            className="p-2 sm:p-2.5 hover:bg-gray-100 dark:hover:bg-slate-800 rounded-xl transition-all duration-300 text-gray-700 dark:text-gray-300 hover:scale-110"
            title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
          >
            {theme === 'light' ? (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
              </svg>
            ) : (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
            )}
          </button>
          
          {/* Notifications */}
          <div className="relative hidden sm:block" ref={notificationsRef}>
            <button
              onClick={() => void handleOpenNotifications()}
              className="p-2 sm:p-2.5 hover:bg-gray-100 dark:hover:bg-slate-800 rounded-xl transition-all duration-300 text-gray-700 dark:text-gray-300 hover:scale-110 relative"
              title="Notifications"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
              </svg>
              {unreadCount > 0 && (
                <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 bg-red-500 text-white text-[10px] font-bold rounded-full flex items-center justify-center">
                  {unreadCount > 99 ? '99+' : unreadCount}
                </span>
              )}
            </button>

            {notificationsOpen && (
              <div className="absolute right-0 mt-2 w-96 max-w-[85vw] rounded-xl border border-gray-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-2xl z-50">
                <div className="px-4 py-3 border-b border-gray-100 dark:border-slate-700 flex items-center justify-between">
                  <p className="text-sm font-semibold text-gray-900 dark:text-white">Notifications</p>
                  <button
                    onClick={() => void handleMarkAllNotificationsRead()}
                    className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50"
                    disabled={unreadCount === 0}
                  >
                    Mark all read
                  </button>
                </div>
                <div className="max-h-80 overflow-y-auto">
                  {loadingNotifications ? (
                    <div className="p-4 text-sm text-gray-500 dark:text-gray-400">Loading…</div>
                  ) : notifications.length === 0 ? (
                    <div className="p-4 text-sm text-gray-500 dark:text-gray-400">No notifications yet.</div>
                  ) : (
                    notifications.map((item) => (
                      <button
                        key={item.id}
                        onClick={() => void handleMarkNotificationRead(item.id)}
                        className={`w-full text-left px-4 py-3 border-b border-gray-100 dark:border-slate-800 hover:bg-gray-50 dark:hover:bg-slate-800/60 transition-colors ${
                          item.is_read ? '' : 'bg-blue-50/60 dark:bg-blue-900/10'
                        }`}
                      >
                        <p className="text-sm font-semibold text-gray-900 dark:text-white">{item.title}</p>
                        <p className="text-xs text-gray-600 dark:text-gray-300 mt-0.5">{item.message}</p>
                        <p className="text-[11px] text-gray-400 mt-1">{new Date(item.created_at).toLocaleString()}</p>
                      </button>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>

          {/* User Profile */}
          <div className="flex items-center gap-1 sm:gap-2 pl-1 sm:pl-2 lg:pl-3 border-l border-gray-200 dark:border-slate-700">
            <button
              onClick={() => navigate(config.profilePath)}
              className="flex items-center gap-1 sm:gap-2 hover:bg-gray-100 dark:hover:bg-slate-800 rounded-xl p-1 sm:p-1.5 transition-all duration-200"
              title="View Profile"
            >
              <div className={`w-7 h-7 sm:w-8 sm:h-8 bg-gradient-to-br ${config.gradient} rounded-full flex items-center justify-center shadow-md`}>
                <span className="text-white font-semibold text-xs sm:text-sm">
                  {userName?.charAt(0).toUpperCase() || authStorage.getItem('user_name')?.charAt(0).toUpperCase() || role.charAt(0).toUpperCase()}
                </span>
              </div>
              <div className="hidden lg:block text-left">
                <p className="text-sm font-medium text-gray-900 dark:text-white">
                  {userName || authStorage.getItem('user_name') || role.charAt(0).toUpperCase() + role.slice(1)}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {config.title}
                </p>
              </div>
            </button>
            {/* Sign in (guest) / Logout (signed-in) - Hidden on mobile, shown on tablet+ */}
            {isGuest ? (
              <button
                onClick={() => navigate('/auth')}
                className="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-500 hover:bg-blue-600 text-white text-sm font-medium transition-colors"
                title="Sign in"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7" />
                </svg>
                Sign in
              </button>
            ) : (
              <button
                onClick={handleLogout}
                className="hidden sm:block p-2 hover:bg-gray-100 dark:hover:bg-slate-800 rounded-lg transition-colors"
                title="Logout"
              >
                <svg className="w-5 h-5 text-gray-500 dark:text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </nav>
  )
}
