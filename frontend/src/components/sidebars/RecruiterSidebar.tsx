import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useSidebar } from '../../context/SidebarContext'
import { useAuth } from '../../context/AuthContext'
import { useRecruiterConnectionOptional } from '../../context/RecruiterConnectionContext'
import { authStorage, clearAuthStorage } from '../../utils/authStorage'
import ApiStatus from '../ApiStatus'

function RecruiterConnectionStatusBlock() {
  const connection = useRecruiterConnectionOptional()
  const { isCollapsed } = useSidebar()
  if (!connection) return null
  const { connectionId, companyName, status } = connection
  if (!connectionId) {
    return (
      <div className={`px-3 pb-2 ${isCollapsed ? 'flex justify-center' : ''}`}>
        <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-gray-100/50 dark:bg-slate-800/50">
          <span className="w-2.5 h-2.5 rounded-full bg-gray-400 shrink-0" />
          <span className="text-xs font-medium text-gray-500 dark:text-gray-400 truncate">No client connected</span>
        </div>
      </div>
    )
  }
  if (status === 'invalid') {
    return (
      <div className={`px-3 pb-2 ${isCollapsed ? 'flex justify-center' : ''}`}>
        <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50">
          <span className="w-2.5 h-2.5 rounded-full bg-red-500 shrink-0" />
          <span className="text-xs font-medium text-red-700 dark:text-red-300 truncate">Connection no longer valid</span>
        </div>
      </div>
    )
  }
  return (
    <div className={`px-3 pb-2 ${isCollapsed ? 'flex justify-center' : ''}`}>
      <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800/50">
        <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 shrink-0" />
        <span className="text-xs font-medium text-emerald-800 dark:text-emerald-200 truncate" title={companyName || undefined}>
          {companyName ? `→ ${companyName}` : 'Connected'}
        </span>
      </div>
    </div>
  )
}

export default function RecruiterSidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { isCollapsed, isMobileOpen, closeMobile } = useSidebar()
  const { signOut } = useAuth()
  
  const navItems = [
    {
      title: 'Dashboard Overview',
      path: '/recruiter',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
        </svg>
      ),
    },
    {
      title: 'CV Builder',
      path: '/recruiter/cv-builder',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m-6-8h6M9 4h6a2 2 0 012 2v12a2 2 0 01-2 2H9a2 2 0 01-2-2V6a2 2 0 012-2z" />
        </svg>
      ),
    },
    {
      title: 'Create Job Posting',
      path: '/recruiter/create-job',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
        </svg>
      ),
    },
    {
      title: 'Upload Candidates',
      path: '/recruiter/upload-candidates',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
        </svg>
      ),
    },
    {
      title: 'AI Shortlist',
      path: '/recruiter/screening',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      ),
    },
    {
      title: 'Search & Filter',
      path: '/recruiter/candidate-search',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
      ),
    },
    {
      title: 'Submit Values',
      path: '/recruiter/values-assessment',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
        </svg>
      ),
    },
    {
      title: 'Schedule Interview',
      path: '/recruiter/schedule-interview',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
      ),
    },
    {
      title: 'Export Assessment',
      path: '/recruiter/export-reports',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z" />
        </svg>
      ),
    },

    {
      title: 'Live Client Monitoring',
      path: '/recruiter/client-jobs',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
        </svg>
      ),
    },
    {
      title: 'Batch Operations',
      path: '/recruiter/batch-operations',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
      ),
    },
    {
      title: 'Auto-Messaging System',
      path: '/recruiter/automation',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
        </svg>
      ),
    },
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
    if (path === '/recruiter') {
      return location.pathname === '/recruiter'
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
                ? 'bg-gradient-to-r from-emerald-500 to-teal-500 text-white shadow-lg shadow-emerald-500/30'
                : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-slate-800'
            }`}
          >
            <span className="flex-shrink-0">{item.icon}</span>
            {!isCollapsed && <span className="font-medium truncate text-sm">{item.title}</span>}
          </Link>
        ))}
      </nav>

      {/* User Info & Logout */}
      <div className="absolute bottom-0 left-0 right-0 border-t border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <div className={`px-3 pt-3 ${isCollapsed ? 'flex justify-center' : ''}`}>
          <ApiStatus />
        </div>
        <RecruiterConnectionStatusBlock />
        <div className={`p-3 ${isCollapsed ? 'flex justify-center' : ''}`}>
          <div className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-3'}`}>
            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-emerald-500 to-teal-500 flex items-center justify-center text-white font-bold text-lg flex-shrink-0">
              {authStorage.getItem('user_name')?.charAt(0).toUpperCase() || 'R'}
            </div>
            {!isCollapsed && (
              <div className="overflow-hidden">
                <p className="font-semibold text-gray-900 dark:text-white truncate text-sm">
                  {authStorage.getItem('user_name') || 'Recruiter'}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                  {authStorage.getItem('user_email') || 'recruiter@sampada.hr'}
                </p>
              </div>
            )}
          </div>
        </div>
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
