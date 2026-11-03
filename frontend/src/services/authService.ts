import axios from 'axios';
import { authStorage } from '../utils/authStorage';

// Define types for user and auth response
interface User {
  id: string;
  email: string;
  name?: string;
  role?: string;
}

interface RegisterRequest {
  email: string;
  password: string;
  name: string;
  role?: string; // 'candidate' | 'recruiter' | 'client'
  company?: string; // For recruiter/client
  phone?: string;
}

interface AuthResponse {
  success: boolean;
  token?: string;
  user?: User;
  error?: string;
}

class AuthService {
  private API_BASE_URL: string;
  private TOKEN_KEY = 'auth_token';
  private USER_KEY = 'user_data';

  constructor() {
    // Standardized variable name: VITE_API_BASE_URL (see ENVIRONMENT_VARIABLES.md)
    this.API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
  }

  // Login user and store JWT token - supports candidate, recruiter, and client
  async login(email: string, password: string, role?: string): Promise<AuthResponse> {
    try {
      // Get role from parameter, or from localStorage (stored during registration)
      // This ensures we use the role the user registered with
      const storedRole = authStorage.getItem('user_role');
      // `role` is set only when the caller chose it deliberately (admin login
      // page, signup auto-login). A stored role is just a routing hint — guest
      // sessions always write 'candidate' and admin sessions write 'admin', so
      // it must never lock the other login endpoints out.
      const explicitRole = role;
      const userRole = role || storedRole;
      
      let response;
      
      if (!userRole) {
        // If no role is stored and none provided, try to detect by attempting both logins
        // This handles edge cases where localStorage was cleared
        console.log('🔐 No role found, attempting auto-detection...');
        
        // Try client login first
        const autoDetect = await this.performClientLogin(email, password);
        if (autoDetect.success) {
          return autoDetect;
        }
        console.log('🔐 Client login failed, trying candidate/recruiter login');
      }
      
      // Platform admin (multi-tenant hierarchy) uses /v1/admin/login
      if (userRole === 'admin') {
        try {
          response = await axios.post(`${this.API_BASE_URL}/v1/admin/login`, {
            email: email,
            password: password
          });

          const data = response.data;
          if (data.success && data.access_token) {
            const token = data.access_token;
            if (!token || token.trim() === '') {
              return { success: false, error: 'Invalid token received from server' };
            }
            const userData: User = {
              id: data.email || email,
              email: data.email || email,
              name: 'Platform Admin',
              role: 'admin'
            };

            this.setAuthToken(token);
            authStorage.setItem(this.TOKEN_KEY, token);
            authStorage.setItem('auth_token', token);
            authStorage.setItem(this.USER_KEY, JSON.stringify(userData));
            authStorage.setItem('user_role', 'admin');
            authStorage.setItem('user_email', userData.email);
            authStorage.setItem('user_name', userData.name || 'Platform Admin');
            authStorage.setItem('isAuthenticated', 'true');

            return { success: true, token: token, user: userData };
          }
          const detail = typeof data.detail === 'string' ? data.detail : '';
          if (explicitRole) {
            return {
              success: false,
              error: data.error || detail || 'Admin login failed. Please check your credentials.'
            };
          }
          // Stored 'admin' is only a stale hint — fall through to the
          // candidate/client chain below instead of failing the login.
        } catch (adminError: any) {
          console.error('❌ Admin login error:', adminError.response?.data?.error || adminError.message);
          const payload = adminError.response?.data;
          let message = payload?.error;
          if (!message && typeof payload?.detail === 'string') message = payload.detail;
          if (!message && Array.isArray(payload?.detail)) {
            message = payload.detail.map((d: any) => d.msg || d.message || '').filter(Boolean).join('; ');
          }
          if (!message && adminError.response?.status) {
            message = `Admin login failed (HTTP ${adminError.response.status}).`;
          }
          if (!message) {
            message = `Cannot reach the gateway at ${this.API_BASE_URL}. Is the backend running?`;
          }
          if (explicitRole) {
            return { success: false, error: message };
          }
          // Stored 'admin' is only a stale hint — fall through to the
          // candidate/client chain below instead of failing the login.
        }
      }

      // Use stored role to determine which endpoint to call
      // Client uses /v1/client/login, candidate/recruiter use /v1/candidate/login
      if (userRole === 'client') {
        const clientResult = await this.performClientLogin(email, password);
        if (clientResult.success || explicitRole) {
          // Explicit client intent (signup auto-login) surfaces the client
          // endpoint's own error (locked / password-setup / invalid) directly.
          return clientResult;
        }
        // Stored 'client' is only a stale hint — fall through to the
        // candidate chain below.
      }
      
      // For recruiter and candidate (or client fallback), use candidate login
      // Backend candidate login works for both candidates and recruiters (stored as candidates)
      try {
        response = await axios.post(`${this.API_BASE_URL}/v1/candidate/login`, {
          email,
          password
        });
      } catch (candidateError: any) {
        // If candidate login fails with "Invalid credentials", try client login as fallback
        // This handles cases where user_role was incorrectly set to 'candidate' for a client
        if (candidateError.response?.status === 401 || 
            candidateError.response?.data?.error?.includes('Invalid credentials') ||
            candidateError.response?.data?.error?.includes('Invalid')) {
          console.log('🔐 Candidate login failed, trying client login as fallback...');
          const thrownFallback = await this.performClientLogin(email, password);
          if (thrownFallback.success) {
            return thrownFallback;
          }
          console.error('❌ Both candidate and client login failed');
          return {
            success: false,
            error: candidateError.response?.data?.error || 'Invalid credentials'
          };
        }
        
        // Re-throw if it's not an "Invalid credentials" error
        throw candidateError;
      }

      // The candidate endpoint answers HTTP 200 with success:false for bad
      // credentials (it never throws), so the catch-based fallback above never
      // fires for it. Retry the client portal before giving up — without this,
      // a stale candidate role in localStorage (guest sessions always write
      // one) blocks tenant-provisioned client accounts from signing in.
      if (!response?.data?.token || !response?.data?.success) {
        if (!explicitRole) {
          const clientResult = await this.performClientLogin(email, password);
          if (clientResult.success) {
            return clientResult;
          }
          return {
            success: false,
            error: clientResult.error || response?.data?.error || 'Invalid credentials'
          };
        }
        return {
          success: false,
          error: response?.data?.error || 'Invalid credentials'
        };
      }

      // Backend returns 'candidate' but we need to set correct role
      if (response.data.token && response.data.success) {
        const userData = response.data.candidate || response.data.user;
        const token = response.data.token;
        
        // Validate token is not empty
        if (!token || token.trim() === '') {
          console.error('❌ Login response has empty token!');
          return { success: false, error: 'Invalid token received from server' };
        }
        
        // Override role from localStorage if available (for recruiter)
        const actualRole = userRole === 'recruiter' ? 'recruiter' : (userData.role || 'candidate');
        const userWithRole = { ...userData, role: actualRole };
        
        // Store token FIRST - this is critical
        console.log('🔐 Storing auth token after login');
        console.log('🔐 Token length:', token.length);
        console.log('🔐 Token first 50 chars:', token.substring(0, 50));
        
        this.setAuthToken(token);
        authStorage.setItem(this.TOKEN_KEY, token);
        authStorage.setItem('auth_token', token);
        
        const storedToken = authStorage.getItem(this.TOKEN_KEY);
        const storedTokenDirect = authStorage.getItem('auth_token');
        
        if (!storedToken && !storedTokenDirect) {
          console.error('❌ CRITICAL: Failed to store token! Auth storage may be disabled or full.');
          console.error('❌ Available storage keys:', typeof sessionStorage !== 'undefined' ? Object.keys(sessionStorage) : []);
        } else if (storedToken !== token && storedTokenDirect !== token) {
          console.error('❌ Token stored but value mismatch!');
          console.error('❌ Expected length:', token.length, 'Stored length:', storedToken?.length || storedTokenDirect?.length);
        } else {
          console.log('✅ Token stored successfully');
          console.log('✅ Token verification: Stored token matches');
        }
        
        authStorage.setItem(this.USER_KEY, JSON.stringify(userWithRole));
        authStorage.setItem('user_role', actualRole);
        
        if (response.data.candidate_id) {
          authStorage.setItem('backend_candidate_id', response.data.candidate_id.toString());
          authStorage.setItem('candidate_id', response.data.candidate_id.toString());
        } else if (userData.id) {
          authStorage.setItem('backend_candidate_id', userData.id.toString());
          authStorage.setItem('candidate_id', userData.id.toString());
        }
        
        return {
          success: true,
          token: token,
          user: userWithRole
        };
      }

      // Handle error response
      if (response.data.error) {
        return { success: false, error: response.data.error };
      }

      return { success: false, error: 'Invalid response from server' };
    } catch (error: any) {
      console.error('Login error:', error);
      return { 
        success: false, 
        error: error.response?.data?.error || error.response?.data?.message || error.message || 'Login failed' 
      };
    }
  }

  // POST /v1/client/login and persist the session on success. Never throws —
  // returns the normalized AuthResponse so callers can chain fallbacks.
  private async performClientLogin(email: string, password: string): Promise<AuthResponse> {
    try {
      const response = await axios.post(`${this.API_BASE_URL}/v1/client/login`, {
        email,
        password
      });
      const data = response.data;
      if (data?.success && data.access_token) {
        const token = String(data.access_token);
        if (!token.trim()) {
          return { success: false, error: 'Invalid token received from server' };
        }
        const userData = {
          id: data.client_id,
          email: email,
          name: data.company_name || '',
          role: 'client',
          company: data.company_name
        };
        this.setAuthToken(token);
        authStorage.setItem(this.TOKEN_KEY, token);
        authStorage.setItem('auth_token', token);
        authStorage.setItem(this.USER_KEY, JSON.stringify(userData));
        authStorage.setItem('client_id', data.client_id);
        authStorage.setItem('user_role', 'client');
        return { success: true, token: token, user: userData };
      }
      return {
        success: false,
        error: data?.error || 'Client login failed. Please check your credentials.'
      };
    } catch (error: any) {
      return {
        success: false,
        error: error?.response?.data?.error || 'Client login failed. Please check your credentials.'
      };
    }
  }

  // Register new user - supports candidate, recruiter, and client
  async register(userData: RegisterRequest): Promise<AuthResponse> {
    try {
      const role = userData.role || 'candidate';
      
      let response;
      
      if (role === 'client') {
        // Client registration requires client_id and company_name
        const client_id = userData.email.split('@')[0] + '_' + Date.now(); // Generate client_id from email
        
        response = await axios.post(`${this.API_BASE_URL}/v1/client/register`, {
          client_id: client_id,
          company_name: userData.company || userData.name + "'s Company",
          contact_email: userData.email,
          password: userData.password
        });
        
        // Client registration successful - don't auto-login, just return success
        if (response.data.success) {
          const final_client_id = response.data.client_id || client_id;
          
          authStorage.setItem('client_id', final_client_id);
          
          // Store role for later login
          const userObj = {
            id: final_client_id,
            email: userData.email,
            name: userData.name,
            role: 'client',
            company: userData.company
          };
          
          return {
            success: true,
            user: userObj,
            // Note: Client login uses client_id, not email - stored in localStorage
          };
        }
      } else if (role === 'recruiter') {
        // Recruiter registration - use candidate endpoint with role field
        response = await axios.post(`${this.API_BASE_URL}/v1/candidate/register`, {
          email: userData.email,
          password: userData.password,
          name: userData.name,
          phone: userData.phone || '',
          location: '',
          experience_years: 0,
          technical_skills: '',
          education_level: '',
          seniority_level: '',
          role: 'recruiter'  // Send role field to backend
        });
        
        // Registration successful - store role but don't auto-login
        if (response.data.success) {
          const userObj = {
            id: response.data.candidate_id || '',
            email: userData.email,
            name: userData.name,
            role: 'recruiter' // Override role from candidate registration
          };
          
          return {
            success: true,
            user: userObj
          };
        }
      } else {
        // Candidate registration (default)
        response = await axios.post(`${this.API_BASE_URL}/v1/candidate/register`, {
          email: userData.email,
          password: userData.password,
          name: userData.name,
          phone: userData.phone || '',
          location: '',
          experience_years: 0,
          technical_skills: '',
          education_level: '',
          seniority_level: ''
        });
        
        // Registration successful - store role but don't auto-login
        if (response.data.success) {
          const userObj = {
            id: response.data.candidate_id || '',
            email: userData.email,
            name: userData.name,
            role: 'candidate'
          };
          
          return {
            success: true,
            user: userObj
          };
        }
      }

      // Handle error response
      if (response.data.error || !response.data.success) {
        const errorMsg = response.data.error || 'Registration failed';
        return { success: false, error: errorMsg };
      }

      return { success: false, error: 'Invalid response from server' };
    } catch (error: any) {
      console.error('Registration error:', error);
      // Handle duplicate email error
      const errorMsg = error.response?.data?.error || error.response?.data?.message || error.message || 'Registration failed';
      
      // Check for email already registered error
      if (errorMsg.includes('already registered') || 
          errorMsg.includes('Email already registered') ||
          errorMsg.includes('already exists')) {
        return { success: false, error: 'This email is already registered. Please use a different email or login instead.' };
      }
      
      return { 
        success: false, 
        error: errorMsg
      };
    }
  }

  logout(): void {
    this.removeAuthToken();
    authStorage.removeItem(this.USER_KEY);
  }

  getAuthToken(): string | null {
    return authStorage.getItem(this.TOKEN_KEY);
  }

  setAuthToken(token: string): void {
    authStorage.setItem(this.TOKEN_KEY, token);
    axios.defaults.headers.common['Authorization'] = `Bearer ${token}`;
  }

  removeAuthToken(): void {
    authStorage.removeItem(this.TOKEN_KEY);
    delete axios.defaults.headers.common['Authorization'];
  }

  // Check if user is authenticated
  isAuthenticated(): boolean {
    const token = this.getAuthToken();
    if (!token) return false;

    // Check if token is expired
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      const currentTime = Math.floor(Date.now() / 1000);
      return payload.exp > currentTime;
    } catch (error) {
      console.error('Error decoding token:', error);
      return false;
    }
  }

  getUserData(): User | null {
    const userData = authStorage.getItem(this.USER_KEY);
    return userData ? JSON.parse(userData) : null;
  }


}

export default new AuthService();