import { createContext, useContext, useEffect, useState, ReactNode } from 'react'
import { authStorage, clearAuthStorage } from '../utils/authStorage'

interface User {
  id: string;
  email: string;
  name?: string;
  role?: string;
  isGuest?: boolean;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<{ error: any }>;
  signUp: (email: string, password: string, userData: { name: string; role: string; company?: string; phone?: string }) => Promise<{ error: any; user?: User }>;
  signOut: () => Promise<void>;
  isGuest: boolean;
  userRole: string | null;
  userName: string | null;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Initialize auth from stored JWT token; derive role from token so role is correct on reload.
    // When there is no valid local session, try Pravesh (blackhole_auth) SSO
    // auto-login: the blackhole_token cookie is sent with credentials:'include'
    // and a valid session means the user never sees the sign-in page.
    let cancelled = false

    const tryPraveshSso = async (): Promise<boolean> => {
      try {
        const { API_BASE_URL } = await import('../services/api')
        const response = await fetch(`${API_BASE_URL}/api/v1/pravesh/exchange`, {
          method: 'POST',
          credentials: 'include',
        })
        if (!response.ok) return false
        const data = await response.json()
        if (!data?.success || !data.token || !data.user) return false
        if (cancelled) return false

        const role = ['candidate', 'recruiter', 'client'].includes(data.user.role)
          ? data.user.role
          : 'candidate'
        const candidateId = data.user.candidate_id || data.user.id
        const ssoUser: User = {
          id: candidateId,
          email: data.user.email || '',
          name: data.user.name || '',
          role,
        }

        authStorage.setItem('auth_token', data.token)
        authStorage.setItem('user_data', JSON.stringify(ssoUser))
        authStorage.setItem('user_role', role)
        authStorage.setItem('user_email', ssoUser.email)
        authStorage.setItem('user_name', ssoUser.name || '')
        authStorage.setItem('isAuthenticated', 'true')
        if (candidateId) {
          authStorage.setItem('candidate_id', candidateId)
          authStorage.setItem('backend_candidate_id', candidateId)
        }
        setUser(ssoUser)
        console.log('✅ AuthContext: Pravesh SSO auto-login as', role)
        return true
      } catch (error) {
        // No Pravesh session (or bridge offline) — fall back to the sign-in page
        console.log('AuthContext: no Pravesh SSO session, opening as guest')
        return false
      }
    }

    const createGuestSession = async (): Promise<void> => {
      // No local session and no Pravesh cookie — open the UI as a demo guest.
      try {
        const { API_BASE_URL } = await import('../services/api')
        const response = await fetch(`${API_BASE_URL}/v1/auth/guest-session`, { method: 'POST' })
        if (response.ok) {
          const data = await response.json()
          if (cancelled) return
          if (data?.success && data.token && data.user) {
            const guest: User = { ...data.user, isGuest: true }
            authStorage.setItem('auth_token', data.token)
            authStorage.setItem('user_data', JSON.stringify(guest))
            authStorage.setItem('user_role', guest.role || 'candidate')
            authStorage.setItem('user_email', '')
            authStorage.setItem('user_name', guest.name || 'Guest User')
            authStorage.setItem('isAuthenticated', 'true')
            authStorage.setItem('candidate_id', guest.id)
            authStorage.setItem('backend_candidate_id', guest.id)
            setUser(guest)
            console.log('✅ AuthContext: guest demo session started')
            return
          }
        }
      } catch (error) {
        console.log('AuthContext: guest session endpoint unavailable, using local guest')
      }
      if (cancelled) return
      const guest: User = { id: 'guest:demo', email: '', name: 'Guest User', role: 'candidate', isGuest: true }
      authStorage.setItem('user_data', JSON.stringify(guest))
      authStorage.setItem('user_role', 'candidate')
      authStorage.setItem('user_email', '')
      authStorage.setItem('user_name', 'Guest User')
      authStorage.setItem('isAuthenticated', 'true')
      setUser(guest)
    }

    const initAuth = async () => {
      const token = authStorage.getItem('auth_token')
      const userData = authStorage.getItem('user_data')
      let restored = false

      if (token && userData) {
        try {
          const payload = JSON.parse(atob(token.split('.')[1]))
          const currentTime = Math.floor(Date.now() / 1000)

          if (payload.exp > currentTime) {
            const parsed = JSON.parse(userData) as User
            const roleFromToken = payload.role
            const role = typeof roleFromToken === 'string' && ['candidate', 'recruiter', 'client'].includes(roleFromToken)
              ? roleFromToken
              : (parsed.role || authStorage.getItem('user_role') || 'candidate')
            const userWithRole = { ...parsed, role }
            setUser(userWithRole)
            authStorage.setItem('user_role', role)
            restored = true
          } else {
            clearAuthStorage()
          }
        } catch (error) {
          console.error('Error parsing token or user data:', error)
          clearAuthStorage()
        }
      }

      if (!restored && !cancelled) {
        const ssoOk = await tryPraveshSso()
        if (!ssoOk && !cancelled) {
          await createGuestSession()
        }
      }
      if (!cancelled) {
        setLoading(false)
      }
    }

    initAuth()
    return () => {
      cancelled = true
    }
  }, [])

  const handleSignIn = async (email: string, password: string) => {
    try {
      const storedRole = authStorage.getItem('user_role');
      
      console.log('🔐 AuthContext: Attempting login with stored role:', storedRole || 'auto-detect');
      
      const authService = (await import('../services/authService')).default;
      // Pass the stored role to login function
      // If no role is stored, authService will auto-detect by trying both endpoints
      const result = await authService.login(email, password, storedRole || undefined);
      
      console.log('🔐 AuthContext: Login result:', { 
        success: result.success, 
        hasToken: !!result.token, 
        hasUser: !!result.user,
        userRole: result.user?.role 
      });
      
      if (result.success && result.token && result.user) {
        const token = result.token;
        
        // Validate token is not empty
        if (!token || token.trim() === '') {
          console.error('❌ AuthContext: Empty token received!');
          return { error: 'Invalid token received from server' };
        }
        
        // Extract role from JWT token (token contains role in payload)
        let role = 'candidate';
        try {
          const payload = JSON.parse(atob(token.split('.')[1]));
          role = payload.role || result.user.role || storedRole || 'candidate';
          console.log('🔐 AuthContext: Extracted role from token:', role);
        } catch (tokenError) {
          console.error('❌ AuthContext: Error parsing token:', tokenError);
          // If token parsing fails, use role from user object, stored role, or default
          role = result.user.role || storedRole || 'candidate';
          console.log('🔐 AuthContext: Using role from user object or stored role:', role);
        }
        
        // Store the JWT token FIRST - this is critical
        console.log('🔐 AuthContext: Storing auth token for role:', role);
        console.log('🔐 AuthContext: Token length:', token.length);
        console.log('🔐 AuthContext: Token first 50 chars:', token.substring(0, 50));
        
        // Store token using multiple methods to ensure it's saved
        try {
          authStorage.setItem('auth_token', token);
          const authServiceInstance = (await import('../services/authService')).default;
          if (authServiceInstance.setAuthToken) {
            authServiceInstance.setAuthToken(token);
          }
          const storedToken = authStorage.getItem('auth_token');
          if (!storedToken) {
            console.error('❌ AuthContext: CRITICAL - Failed to store token!');
            try {
              authStorage.setItem('auth_token', token);
              const retryToken = authStorage.getItem('auth_token');
              if (!retryToken) {
                return { error: 'Failed to store authentication token. Please check browser settings.' };
              }
            } catch (e) {
              return { error: 'Failed to store authentication token: ' + (e as Error).message };
            }
          }
        } catch (storageError) {
          console.error('❌ AuthContext: Error storing token:', storageError);
          return { error: 'Failed to store authentication token: ' + (storageError as Error).message };
        }
        
        authStorage.setItem('user_data', JSON.stringify(result.user));
        authStorage.setItem('user_role', role);
        authStorage.setItem('user_email', result.user.email || email);
        authStorage.setItem('user_name', result.user.name || '');
        authStorage.setItem('isAuthenticated', 'true');
        if (role === 'client' && result.user.id) {
          authStorage.setItem('client_id', result.user.id);
        } else if ((role === 'candidate' || role === 'recruiter') && result.user.id) {
          authStorage.setItem('candidate_id', result.user.id);
          authStorage.setItem('backend_candidate_id', result.user.id);
        }
        
        // Update user object with role
        const userWithRole = { ...result.user, role };
        setUser(userWithRole);

        console.log('✅ AuthContext: Login successful for role:', role);
        return { error: null };
      } else {
        console.error('❌ AuthContext: Login failed - no token or user in result:', result);
        const errorMsg = result.error || 'Login failed - no token or user data received';
        return { error: errorMsg };
      }
    } catch (error) {
      console.error('❌ AuthContext: Login error:', error);
      return { error: error instanceof Error ? error.message : 'Login failed' };
    }
  };

  const handleSignUp = async (email: string, password: string, userData: { name: string; role: string; company?: string; phone?: string }) => {
    try {
      const authService = (await import('../services/authService')).default;
      const result = await authService.register({
        email,
        password,
        name: userData.name,
        role: userData.role,
        company: userData.company,
        phone: userData.phone
      });

      if (result.success && result.user) {
        // Store user data and role temporarily
        const role = userData.role || result.user.role || 'candidate';
        authStorage.setItem('user_role', role);
        authStorage.setItem('user_email', email);
        authStorage.setItem('user_name', userData.name);
        authStorage.setItem('user_data', JSON.stringify(result.user));
        if (role === 'client' && result.user.id) {
          authStorage.setItem('client_id', result.user.id);
        }
        if ((role === 'candidate' || role === 'recruiter') && result.user.id) {
          authStorage.setItem('backend_candidate_id', String(result.user.id));
          authStorage.setItem('candidate_id', String(result.user.id));
        }
        
        // Do NOT setUser(result.user) here. Any component observing user could redirect or set
        // authReady before token is stored and verified. setUser is called only after token
        // verification (below) so Dashboard and others never see user without a valid token.
        
        // IMPORTANT: Auto-login after successful registration to get auth token
        // Registration doesn't return a token, so we need to log in to get one
        console.log('🔐 Auto-logging in after successful registration...');
        const loginResult = await authService.login(email, password, role);
        
        if (loginResult.success && loginResult.token && loginResult.user) {
          const token = loginResult.token;
          
          // Validate token is not empty
          if (!token || token.trim() === '') {
            console.error('❌ AuthContext: Empty token received after auto-login!');
            return { error: 'Registration successful but auto-login failed. Please log in manually.' };
          }
          
          // Extract role from JWT token
          let extractedRole = role;
          try {
            const payload = JSON.parse(atob(token.split('.')[1]));
            extractedRole = payload.role || role;
            console.log('🔐 AuthContext: Extracted role from auto-login token:', extractedRole);
          } catch (tokenError) {
            console.error('❌ AuthContext: Error parsing auto-login token:', tokenError);
          }
          
          // Store the JWT token
          authStorage.setItem('auth_token', token);
          const storedToken = authStorage.getItem('auth_token');
          if (!storedToken) {
            return { error: 'Registration successful but failed to store authentication token.' };
          }
          authStorage.setItem('user_data', JSON.stringify(loginResult.user));
          authStorage.setItem('user_role', extractedRole);
          authStorage.setItem('isAuthenticated', 'true');
          if (extractedRole === 'client' && loginResult.user.id) {
            authStorage.setItem('client_id', loginResult.user.id);
          } else if ((extractedRole === 'candidate' || extractedRole === 'recruiter') && loginResult.user.id) {
            authStorage.setItem('candidate_id', loginResult.user.id);
            authStorage.setItem('backend_candidate_id', loginResult.user.id);
          }
          const userWithRole = { ...loginResult.user, role: extractedRole };
          authService.setAuthToken(token);
          const verifyToken = () => authStorage.getItem('auth_token') === token;
          if (!verifyToken()) {
            clearAuthStorage();
            return { error: 'Failed to persist authentication. Please log in manually.' };
          }
          await new Promise((r) => setTimeout(r, 0));
          if (!verifyToken()) {
            clearAuthStorage();
            return { error: 'Authentication state lost. Please log in manually.' };
          }
          
          // Only set user after token is verified so consumers never see user without a valid token
          setUser(userWithRole);
          
          console.log('✅ AuthContext: Registration and auto-login successful for role:', extractedRole);
          return { error: null, user: userWithRole };
        } else {
          console.error('❌ AuthContext: Auto-login failed after registration:', loginResult.error);
          return { error: loginResult.error || 'Registration successful but auto-login failed. Please log in manually.' };
        }
      } else {
        return { error: result.error || 'Registration failed' };
      }
    } catch (error) {
      console.error('❌ AuthContext: Registration error:', error);
      return { error: error instanceof Error ? error.message : 'Registration failed' };
    }
  };

  const handleSignOut = async () => {
    // End the Pravesh SSO session too: the httpOnly blackhole_token cookie
    // outlives local storage, and initAuth() would auto-login again on reload.
    try {
      const { API_BASE_URL } = await import('../services/api')
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 3000)
      try {
        await fetch(`${API_BASE_URL}/api/v1/pravesh/logout`, {
          method: 'POST',
          credentials: 'include',
          signal: controller.signal,
        })
      } finally {
        clearTimeout(timer)
      }
    } catch {
      // Bridge offline — local sign-out still proceeds
    }
    clearAuthStorage();
    const authService = (await import('../services/authService')).default;
    authService.removeAuthToken();
    setUser(null);
  };

  const userRole = user?.role || authStorage.getItem('user_role');
  const userName = user?.name || authStorage.getItem('user_name');

  return (
    <AuthContext.Provider value={{
      user,
      loading,
      signIn: handleSignIn,
      signUp: handleSignUp,
        signOut: handleSignOut,
        isGuest: user?.isGuest === true,
        userRole,
      userName,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
