import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useSidebar } from '../../context/SidebarContext'
import { useAuth } from '../../context/AuthContext'
import { authStorage, clearAuthStorage } from '../../utils/authStorage'
import ApiStatus from '../ApiStatus'

export default function CandidateSidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { isCollapsed, isMobileOpen, closeMobile } = useSidebar()
  const { signOut, isGuest } = useAuth()
  
  const navItems = [
    {
      title: 'Dashboard',
      path: '/candidate/dashboard',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
        </svg>
      ),
    },
    {
      title: 'My Profile',
      path: '/candidate/profile',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
        </svg>
      ),
    },
    {
      title: 'Browse Jobs',
      path: '/candidate/jobs',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
      ),
    },
    {
      title: 'Applied Jobs',
      path: '/candidate/applied-jobs',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
        </svg>
      ),
    },
    {
      title: 'Interviews',
      path: '/candidate/interviews',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
      ),
    },
    {
      title: 'My Tasks',
      path: '/candidate/tasks',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
      ),
    },
    {
      title: 'Feedback',
      path: '/candidate/feedback',
      icon: (
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
        </svg>
      ),
    },
  ]

  const isActive = (path: string) =>
    path === '/candidate/tasks'
      ? location.pathname === path || location.pathname.startsWith(`${path}/`)
      : location.pathname === path

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
                ? 'bg-gradient-to-r from-blue-500 to-cyan-500 text-white shadow-lg shadow-blue-500/30'
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
        {/* User Details */}
        <div className={`p-3 ${isCollapsed ? 'flex justify-center' : ''}`}>
          <div className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-3'}`}>
            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-blue-500 to-cyan-500 flex items-center justify-center text-white font-bold text-lg flex-shrink-0">
              {authStorage.getItem('user_name')?.charAt(0).toUpperCase() || 'C'}
            </div>
            {!isCollapsed && (
              <div className="overflow-hidden">
                <p className="font-semibold text-gray-900 dark:text-white truncate text-sm">
                  {authStorage.getItem('user_name') || 'Candidate'}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                  {authStorage.getItem('user_email') || 'candidate@sampada.hr'}
                </p>
              </div>
            )}
          </div>
        </div>
        {/* Sign in (guest) / Logout (signed-in) */}
        <div className="p-3 pt-0">
          {isGuest ? (
            <button
              onClick={() => navigate('/auth')}
              title={isCollapsed ? 'Sign in' : undefined}
              className={`flex items-center ${isCollapsed ? 'justify-center' : 'gap-2'} w-full px-2.5 py-2 rounded-lg bg-blue-500/10 hover:bg-blue-500 text-blue-600 hover:text-white border border-blue-200 dark:border-blue-800 hover:border-blue-500 transition-all duration-200`}
            >
              <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7" />
              </svg>
              {!isCollapsed && <span className="text-sm font-medium">Sign in</span>}
            </button>
          ) : (
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
          )}
        </div>
      </div>
    </aside>
  )
}
