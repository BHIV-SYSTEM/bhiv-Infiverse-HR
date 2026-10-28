import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { authStorage } from '../utils/authStorage'

interface ProtectedRouteProps {
  children: React.ReactNode
  allowedRoles?: string[]
  requireAuth?: boolean
}

/**
 * ProtectedRoute - Role-based route protection component
 * 
 * Usage:
 * - Wrap routes that require authentication
 * - Specify allowedRoles to restrict access by role
 * 
 * Examples:
 * <ProtectedRoute allowedRoles={['candidate']}>
 *   <CandidateLayout />
 * </ProtectedRoute>
 */
export default function ProtectedRoute({ 
  children, 
  allowedRoles = [],
  requireAuth = true 
}: ProtectedRouteProps) {
  const { user, userRole, loading } = useAuth()
  const location = useLocation()

  // Show loading state while checking auth
  if (loading) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
          <p className="text-slate-400">Checking authentication...</p>
        </div>
      </div>
    )
  }

  // Check if user is authenticated
  const isAuthenticated = !!user || authStorage.getItem('isAuthenticated') === 'true'
  
  if (requireAuth && !isAuthenticated) {
    // Redirect to auth page with return URL
    return <Navigate to="/auth" state={{ from: location }} replace />
  }

  // Check role-based access
  if (allowedRoles.length > 0 && userRole) {
    const hasRequiredRole = allowedRoles.includes(userRole)
    
    if (!hasRequiredRole) {
      // Redirect to their correct dashboard based on their actual role
      const roleRedirects: Record<string, string> = {
        candidate: '/candidate/dashboard',
        recruiter: '/recruiter',
        client: '/client',
      }
      
      const redirectPath = roleRedirects[userRole] || '/'
      
      // Prevent redirect loop
      if (location.pathname !== redirectPath) {
        return <Navigate to={redirectPath} replace />
      }
    }
  }

  return <>{children}</>
}

/**
 * PublicRoute - For routes that should redirect authenticated users
 * 
 * Usage: Wrap login/signup pages to redirect already logged-in users
 */
export function PublicRoute({ children }: { children: React.ReactNode }) {
  const { user, userRole, loading } = useAuth()

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  // If user is already authenticated, redirect to their dashboard.
  // Guests keep access to /auth so they can sign in for real.
  const isAuthenticated = !!user || authStorage.getItem('isAuthenticated') === 'true'
  const isGuest = user?.isGuest === true

  if (isAuthenticated && userRole && !isGuest) {
    const roleRedirects: Record<string, string> = {
      candidate: '/candidate/dashboard',
      recruiter: '/recruiter',
      client: '/client',
    }
    return <Navigate to={roleRedirects[userRole] || '/'} replace />
  }

  return <>{children}</>
}
