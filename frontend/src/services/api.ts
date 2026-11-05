import axios from 'axios'
import { authStorage } from '../utils/authStorage'

// API Base URL - Gateway service
// Standardized variable name: VITE_API_BASE_URL (see ENVIRONMENT_VARIABLES.md)
// Default to localhost for local development, use env var or Render URL for production
// All production URLs must be set via Vercel environment variables (dashboard → Settings → Environment Variables).
// Localhost fallbacks are only for local development.

const LOCAL_SERVICE_URLS = {
  gateway: 'http://localhost:8000',
  agent: 'http://localhost:9000',
  langgraph: 'http://localhost:9001',
} as const

const stripTrailingSlash = (url: string) => url.replace(/\/+$/, '')

function isLocalBrowserHost(): boolean {
  if (typeof window === 'undefined') return false
  const host = window.location.hostname
  return host === 'localhost' || host === '127.0.0.1'
}

/** In Vite dev on localhost, use local service ports even if .env still has Render URLs. */
function resolveServiceBaseUrl(configured: string | undefined, localDefault: string): string {
  const trimmed = configured?.trim()
  const candidate = stripTrailingSlash(trimmed || localDefault)
  if (!import.meta.env.DEV || !isLocalBrowserHost()) {
    return candidate
  }
  if (/onrender\.com/i.test(candidate)) {
    return stripTrailingSlash(localDefault)
  }
  return candidate
}

export const API_BASE_URL = resolveServiceBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  LOCAL_SERVICE_URLS.gateway,
)
export const AGENT_SERVICE_URL = resolveServiceBaseUrl(
  import.meta.env.VITE_AGENT_SERVICE_URL,
  LOCAL_SERVICE_URLS.agent,
)
export const LANGGRAPH_SERVICE_URL = resolveServiceBaseUrl(
  import.meta.env.VITE_LANGGRAPH_SERVICE_URL,
  LOCAL_SERVICE_URLS.langgraph,
)

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000, // Increased timeout for Render cold starts (30 seconds)
  headers: {
    'Content-Type': 'application/json',
  },
})

const NOTIFICATION_REQUEST_TIMEOUT_MS = 150000
const NOTIFICATION_TRANSIENT_RETRIES = 1

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const getAuthHeaders = () => {
  const token = authStorage.getItem('auth_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export interface GatewayMetricsDashboard {
  performance_summary: Record<string, unknown>
  business_metrics: Record<string, unknown>
  system_metrics: Record<string, unknown>
}

export interface ControlCenterApiMeta {
  correlationId?: string
}

export interface ControlCenterAuditEvent {
  action: string
  outcome: 'success' | 'failure' | 'denied'
  detail?: string
  correlation_id?: string
  context?: Record<string, unknown>
}

export interface GatewayCandidateStats {
  total_candidates: number
  active_jobs: number
  recent_matches: number
  pending_interviews: number
  new_candidates_this_week: number
  total_feedback_submissions: number
  statistics_generated_at?: string
  data_source?: string
  dashboard_ready?: boolean
}

const toNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'string' ? Number(value) : typeof value === 'number' ? value : Number.NaN
  return Number.isFinite(parsed) ? parsed : fallback
}

export interface ServiceHealthSnapshot {
  service: string
  status: string
  version?: string
  timestamp?: string
  environment?: string
  baseUrl: string
  healthy: boolean
  raw: Record<string, unknown>
}

export const fetchGatewayMetricsDashboard = async (): Promise<{
  data: GatewayMetricsDashboard
  meta: ControlCenterApiMeta
}> => {
  const response = await api.get('/metrics/dashboard')
  return {
    data: response.data as GatewayMetricsDashboard,
    meta: {
      correlationId:
        String(response.headers?.['x-correlation-id'] || response.headers?.['X-Correlation-ID'] || '').trim() ||
        undefined,
    },
  }
}

export const fetchGatewayCandidateStats = async (): Promise<{
  data: GatewayCandidateStats
  meta: ControlCenterApiMeta
}> => {
  const response = await api.get('/v1/candidates/stats')
  const data = (response.data || {}) as Record<string, unknown>

  return {
    data: {
      total_candidates: toNumber(data.total_candidates),
      active_jobs: toNumber(data.active_jobs),
      recent_matches: toNumber(data.recent_matches),
      pending_interviews: toNumber(data.pending_interviews),
      new_candidates_this_week: toNumber(data.new_candidates_this_week),
      total_feedback_submissions: toNumber(data.total_feedback_submissions),
      statistics_generated_at: typeof data.statistics_generated_at === 'string' ? data.statistics_generated_at : undefined,
      data_source: typeof data.data_source === 'string' ? data.data_source : undefined,
      dashboard_ready: typeof data.dashboard_ready === 'boolean' ? data.dashboard_ready : undefined,
    },
    meta: {
      correlationId:
        String(response.headers?.['x-correlation-id'] || response.headers?.['X-Correlation-ID'] || '').trim() ||
        undefined,
    },
  }
}

export const fetchServiceHealth = async (
  serviceBaseUrl: string,
  fallbackServiceName: string,
): Promise<ServiceHealthSnapshot> => {
  const normalizedBaseUrl = stripTrailingSlash(serviceBaseUrl)

  try {
    const response = await axios.get(`${normalizedBaseUrl}/health`, {
      timeout: 10000,
      headers: getAuthHeaders(),
    })

    const data = (response.data || {}) as Record<string, unknown>
    const status = String(data.status || 'unknown')

    return {
      service: String(data.service || fallbackServiceName),
      status,
      version: typeof data.version === 'string' ? data.version : undefined,
      timestamp: typeof data.timestamp === 'string' ? data.timestamp : undefined,
      environment: typeof data.environment === 'string' ? data.environment : undefined,
      baseUrl: normalizedBaseUrl,
      healthy: status.toLowerCase() === 'healthy' || status.toLowerCase() === 'ok',
      raw: {
        ...data,
        correlation_id:
          String(response.headers?.['x-correlation-id'] || response.headers?.['X-Correlation-ID'] || '').trim() ||
          undefined,
      },
    }
  } catch (error) {
    const errorMessage = axios.isAxiosError(error)
      ? error.message || 'Health endpoint unavailable'
      : error instanceof Error
        ? error.message
        : 'Health endpoint unavailable'

    return {
      service: fallbackServiceName,
      status: 'offline',
      baseUrl: normalizedBaseUrl,
      healthy: false,
      raw: { error: errorMessage },
    }
  }
}

export const postControlCenterAuditEvent = async (event: ControlCenterAuditEvent): Promise<void> => {
  await api.post('/v1/control-center/audit-events', event)
}

export interface ControlCenterTraceEvent {
  ts: string
  service: string
  op: string
  correlation_id: string
  status: 'success' | 'failure' | 'in_progress'
  source_system?: string
  derived_flag?: boolean
}

export interface ControlCenterDashboardAggregates {
  hiring_funnel: { label: string; count: number; color: string }[]
  department_load: { dept: string; load: number; color: string; applications?: number }[]
  policy_scope?: Record<string, unknown>
  generated_at?: string
  data_source?: string
}

export interface ControlCenterAuditReplay {
  events: ControlCenterTraceEvent[]
  count: number
  correlation_id?: string | null
  source: string
  replay_mode?: string
  policy_scope?: Record<string, unknown>
}

export const fetchControlCenterAuditReplay = async (
  correlationId?: string,
): Promise<{ data: ControlCenterAuditReplay; meta: ControlCenterApiMeta }> => {
  const response = await api.get('/v1/control-center/audit-replay', {
    params: correlationId ? { correlation_id: correlationId } : undefined,
  })
  const data = (response.data || {}) as Record<string, unknown>
  const events = Array.isArray(data.events) ? (data.events as ControlCenterTraceEvent[]) : []

  return {
    data: {
      events,
      count: toNumber(data.count, events.length),
      correlation_id: typeof data.correlation_id === 'string' ? data.correlation_id : null,
      source: typeof data.source === 'string' ? data.source : 'audit_logs',
      replay_mode: typeof data.replay_mode === 'string' ? data.replay_mode : undefined,
      policy_scope: typeof data.policy_scope === 'object' ? (data.policy_scope as Record<string, unknown>) : undefined,
    },
    meta: {
      correlationId:
        String(response.headers?.['x-correlation-id'] || response.headers?.['X-Correlation-ID'] || '').trim() ||
        undefined,
    },
  }
}

// ── Workforce governance APIs (control center visibility) ───────────────────

export interface WorkforceListResponse<T> {
  items: T[]
  policy_scope?: Record<string, unknown>
}

export interface WorkforceOrgHierarchy {
  organization: Record<string, unknown>
  divisions: Record<string, unknown>[]
  units: Record<string, unknown>[]
  departments: Record<string, unknown>[]
  employees: Record<string, unknown>[]
  policy_scope?: Record<string, unknown>
}

export interface WorkforceTraceReplay {
  correlation_id?: string | null
  event_count: number
  events: { action: string; outcome: string; correlation_id?: string; trace_id?: string }[]
}

export const fetchWorkforceOrganizations = async (): Promise<WorkforceListResponse<Record<string, unknown>>> => {
  const response = await api.get('/v1/workforce/organizations')
  return response.data as WorkforceListResponse<Record<string, unknown>>
}

export const fetchWorkforceOrgHierarchy = async (orgId: string): Promise<WorkforceOrgHierarchy> => {
  const response = await api.get(`/v1/workforce/organizations/${orgId}/hierarchy`)
  return response.data as WorkforceOrgHierarchy
}

export const fetchPolicyDefinitions = async (): Promise<WorkforceListResponse<Record<string, unknown>>> => {
  const response = await api.get('/v1/policies/definitions')
  return response.data as WorkforceListResponse<Record<string, unknown>>
}

export const fetchGovernanceChallenges = async (): Promise<WorkforceListResponse<Record<string, unknown>>> => {
  const response = await api.get('/v1/governance/challenges')
  return response.data as WorkforceListResponse<Record<string, unknown>>
}

export const fetchGovernanceDecisions = async (): Promise<WorkforceListResponse<Record<string, unknown>>> => {
  const response = await api.get('/v1/decisions')
  return response.data as WorkforceListResponse<Record<string, unknown>>
}

export const fetchSetuSignals = async (): Promise<WorkforceListResponse<Record<string, unknown>>> => {
  const response = await api.get('/v1/setu/signals')
  return response.data as WorkforceListResponse<Record<string, unknown>>
}

export const fetchWorkforceTraceReplay = async (
  correlationId?: string,
): Promise<WorkforceTraceReplay> => {
  const response = await api.get('/v1/workforce/trace-replay', {
    params: correlationId ? { correlation_id: correlationId } : undefined,
  })
  return response.data as WorkforceTraceReplay
}

export const fetchControlCenterDashboardAggregates = async (): Promise<{
  data: ControlCenterDashboardAggregates
  meta: ControlCenterApiMeta
}> => {
  const response = await api.get('/v1/control-center/dashboard-aggregates')
  const data = (response.data || {}) as Record<string, unknown>

  return {
    data: {
      hiring_funnel: Array.isArray(data.hiring_funnel)
        ? (data.hiring_funnel as ControlCenterDashboardAggregates['hiring_funnel'])
        : [],
      department_load: Array.isArray(data.department_load)
        ? (data.department_load as ControlCenterDashboardAggregates['department_load'])
        : [],
      policy_scope: typeof data.policy_scope === 'object' ? (data.policy_scope as Record<string, unknown>) : undefined,
      generated_at: typeof data.generated_at === 'string' ? data.generated_at : undefined,
      data_source: typeof data.data_source === 'string' ? data.data_source : undefined,
    },
    meta: {
      correlationId:
        String(response.headers?.['x-correlation-id'] || response.headers?.['X-Correlation-ID'] || '').trim() ||
        undefined,
    },
  }
}

const shouldRetryNotificationRequest = (error: unknown): boolean => {
  if (!axios.isAxiosError(error)) return false
  const status = error.response?.status
  // Retry only when service is temporarily unavailable upstream.
  return status === 503 || status === 504
}

const formatNotificationError = (error: unknown): string => {
  if (!axios.isAxiosError(error)) {
    return 'Notification service failed. Please try again.'
  }

  const status = error.response?.status

  if (status === 503 || status === 504) {
    return 'Notification service is warming up or temporarily overloaded. Please wait 20-40 seconds and try again.'
  }

  if (error.code === 'ECONNABORTED') {
    return 'Notification request timed out. WhatsApp may have been delivered already; wait 60 seconds before retrying to avoid duplicates.'
  }

  return error.message || 'Notification service failed. Please try again.'
}

const postNotificationWithRetry = async (url: string, payload: Record<string, unknown>) => {
  let lastError: unknown

  for (let attempt = 0; attempt <= NOTIFICATION_TRANSIENT_RETRIES; attempt++) {
    try {
      const response = await api.post(url, payload, {
        timeout: NOTIFICATION_REQUEST_TIMEOUT_MS,
      })
      return response.data
    } catch (error) {
      lastError = error

      if (attempt < NOTIFICATION_TRANSIENT_RETRIES && shouldRetryNotificationRequest(error)) {
        // Short backoff gives Render time to wake/recover.
        await wait(6000)
        continue
      }

      if (axios.isAxiosError(error)) {
        error.message = formatNotificationError(error)
      }
      throw error
    }
  }

  throw lastError
}

// Request interceptor - Use JWT token for authentication
api.interceptors.request.use(
  async (config) => {
    let token = authStorage.getItem('auth_token');
    
    if (!token) {
      const isAuthenticated = authStorage.getItem('isAuthenticated') === 'true';
      const isHealthCheck = config.url?.includes('/health');
      if (isHealthCheck) {
      } else if (isAuthenticated) {
        console.warn('⚠️ Token missing but user is authenticated for request:', config.url);
        console.warn('This might be a race condition. Available auth storage keys:', typeof sessionStorage !== 'undefined' ? Object.keys(sessionStorage) : []);
      } else {
        console.warn('⚠️ No auth_token found in auth storage for request:', config.url);
      }
    } else {
      // Only log for non-health check endpoints to reduce noise (optional: gate by import.meta.env.DEV)
      if (!config.url?.includes('/health') && import.meta.env.DEV) {
        console.log('✅ Adding Authorization header for request:', config.url);
      }
      config.headers.Authorization = `Bearer ${token}`;
    }
    
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    // Log errors for debugging
    if (error.response) {
      const status = error.response.status;
      const url = error.config?.url;
      
      if (status === 401) {
        // 401 Unauthorized - token might be invalid or expired
        console.error(`❌ 401 Unauthorized for: ${url}`);
        console.error('Response details:', error.response.data);
        
        const token = authStorage.getItem('auth_token');
        if (token) {
          console.error('Token exists but was rejected. Token (first 50 chars):', token.substring(0, 50));
          console.error('This suggests the token is invalid, expired, or signed with wrong secret.');
        } else {
          console.error('Token is missing from auth storage.');
        }
        
        // Don't clear token immediately - let the app handle it
        // The token might be valid but the endpoint might require different auth
      } else {
        // Expected when Complete-Infiverse is down; Tasks page shows a toast — avoid duplicate console noise.
        const u = String(url || '')
        const quiet502 =
          status === 502 && /\/v1\/candidate\/workflow/.test(u)
        if (!quiet502) {
          console.error(`API Error: ${status} - ${url}`)
        }
      }
    } else if (error.request) {
      // Network error - no response received
      console.error(`Network Error: Unable to reach API at ${error.config?.baseURL}${error.config?.url}`)
      // Add a more descriptive error message
      error.message = error.message || `Cannot connect to server. Please ensure the backend is running at ${error.config?.baseURL}`
      error.isNetworkError = true
    } else {
      console.error('API Error:', error.message)
    }
    return Promise.reject(error)
  }
)

// ==================== CANDIDATE AUTH API ====================

export interface CandidateLoginRequest {
  email: string
  password?: string
}

export interface CandidateRegisterRequest {
  name: string
  email: string
  password?: string
  phone?: string
  skills?: string[]
  experience_years?: number
  education?: string
  location?: string
}

export interface CandidateProfile {
  id: string
  name: string
  email: string
  phone?: string
  skills: string[]
  experience_years?: number
  totalExperience?: number
  education?: string
  educationLevel?: string
  education_level?: string  // Backend field name
  technical_skills?: string  // Backend field name
  seniority_level?: string  // Backend field name
  location?: string
  resume_url?: string
  resume_path?: string  // Backend field name
  expectedSalary?: number
  currentSalary?: number
  values_score?: {
    integrity: number
    honesty: number
    discipline: number
    hardWork: number
    gratitude: number
  }
}

export const candidateLogin = async (data: CandidateLoginRequest) => {
  try {
    const response = await api.post('/v1/candidate/login', data)
    if (response.data.success) {
      authStorage.setItem('auth_token', response.data.token || '')
      authStorage.setItem('candidate_id', response.data.candidate_id)
      if (response.data.candidate_id) {
        authStorage.setItem('backend_candidate_id', response.data.candidate_id.toString())
      }
      authStorage.setItem('user_name', response.data.name || '')
      authStorage.setItem('user_email', data.email)
    }
    return response.data
  } catch (error) {
    console.error('Candidate login error:', error)
    throw error
  }
}

export const candidateRegister = async (data: CandidateRegisterRequest) => {
  try {
    const response = await api.post('/v1/candidate/register', data)
    // Store the backend candidate_id if registration successful
    if (response.data.candidate_id) {
      authStorage.setItem('backend_candidate_id', response.data.candidate_id.toString())
    }
    return response.data
  } catch (error) {
    console.error('Candidate registration error:', error)
    throw error
  }
}

// Helper to get or create backend candidate ID
export const getOrCreateBackendCandidateId = async (): Promise<string | null> => {
  // Check if we already have a backend candidate_id stored
  const storedId = authStorage.getItem('backend_candidate_id')
  if (storedId) {
    return storedId
  }

  const userEmail = authStorage.getItem('user_email');
  const userName = authStorage.getItem('user_name') || 'User';
  
  if (!userEmail) {
    console.warn('No authenticated user email found');
    return null;
  }

  // First, try to get existing candidate by email (check if profile exists)
  try {
    // Try to get candidate profile - if it exists, we can extract the ID
    // We'll search through candidates endpoint or try to find by email
    const candidatesResponse = await api.get(`/v1/candidates?search=${encodeURIComponent(userEmail)}`)
    if (candidatesResponse.data?.candidates?.length > 0) {
      const candidate = candidatesResponse.data.candidates.find((c: any) => c.email === userEmail)
      if (candidate && candidate.id) {
        authStorage.setItem('backend_candidate_id', candidate.id.toString())
        return candidate.id.toString()
      }
    }
  } catch (searchError) {
    console.log('Could not find candidate by search, will try other methods')
  }

  // Try to create a candidate using the JWT authenticated user data
  // Note: Backend requires password, so we'll use a default password
  // In production, this should be handled differently
  try {
    const response = await api.post('/v1/candidate/register', {
      name: userName,
      email: userEmail,
      password: 'temp_password_123', // Temporary password - user should update profile
      phone: '',
      location: '',
      experience_years: 0,
      technical_skills: '',
      education_level: '',
      seniority_level: '',
    });
    
    // Check if registration was successful
    if (response.data.success !== false && response.data.candidate_id) {
      authStorage.setItem('backend_candidate_id', response.data.candidate_id.toString());
      return response.data.candidate_id.toString();
    }
    
    // If email already exists, try to get candidate ID from error or try login
    if (response.data.error && response.data.error.includes('already registered')) {
      // Email exists, try to get candidate by trying login with a dummy password
      // Or better: try to find candidate by email through profile endpoint
      console.log('Email already registered, trying to find existing candidate...')
      return await findCandidateByEmail(userEmail)
    }
    
    return null;
  } catch (error: any) {
    console.error('Error creating backend candidate:', error);
    
    // If registration failed due to email exists (422 or error message)
    if (error?.response?.status === 422 || 
        error?.response?.data?.error?.includes('already registered') ||
        error?.response?.data?.success === false) {
      console.log('Email already exists, trying to find candidate...')
      return await findCandidateByEmail(userEmail)
    }
    
    return null;
  }
}

// Helper to find candidate by email
async function findCandidateByEmail(email: string): Promise<string | null> {
  try {
    // Try to get candidates and find by email
    // The endpoint returns paginated results, so we might need to check multiple pages
    // For now, try first page (50 candidates)
    const response = await api.get('/v1/candidates?limit=100')
    if (response.data?.candidates) {
      const candidate = response.data.candidates.find((c: any) => c.email === email)
      if (candidate && candidate.id) {
        console.log('Found existing candidate by email:', candidate.id)
        authStorage.setItem('backend_candidate_id', candidate.id.toString())
        return candidate.id.toString()
      }
    }
    
    // If not found in first page, candidate might not exist yet
    // User needs to complete profile to create candidate record
    console.warn('Could not find candidate by email. User may need to complete profile setup.')
    return null
  } catch (error) {
    console.error('Error finding candidate by email:', error)
    return null
  }
}

// ==================== JOBS API ====================

export interface Job {
  id: string
  title: string
  department?: string
  location: string
  job_type: string
  employment_type?: string
  experience_required: string
  experience_level?: string
  salary_min?: number
  salary_max?: number
  skills_required: string[] | string
  requirements?: string
  description: string
  status: string
  created_at?: string
  company?: string
  client_id?: string | null
  recruiter_id?: string | null
  connection_id?: string | null
  /** Per-job counts from recruiter jobs endpoint (dashboard only). */
  applicants?: number
  shortlisted?: number
}

export interface JobFilters {
  skills?: string
  location?: string
  experience?: string
  job_type?: string
  search?: string
}

/** Normalize API job shape to Job (experience_level → experience_required, requirements → skills_required, etc.). */
function normalizeJob(raw: Record<string, unknown>): Job {
  const req = raw.requirements as string | undefined
  return {
    id: (raw.id as string) ?? '',
    title: (raw.title as string) ?? '',
    department: raw.department as string | undefined,
    location: (raw.location as string) ?? '',
    job_type: (raw.job_type as string) ?? (raw.employment_type as string) ?? '',
    employment_type: (raw.employment_type as string) ?? (raw.job_type as string) ?? '',
    experience_required: (raw.experience_required as string) ?? (raw.experience_level as string) ?? '',
    experience_level: (raw.experience_level as string) ?? (raw.experience_required as string) ?? '',
    salary_min: raw.salary_min != null ? Number(raw.salary_min) : undefined,
    salary_max: raw.salary_max != null ? Number(raw.salary_max) : undefined,
    skills_required: (raw.skills_required as string[] | string) ?? (req ? (req.includes(',') ? req.split(',').map(s => s.trim()) : req) : []),
    requirements: req,
    description: (raw.description as string) ?? '',
    status: (raw.status as string) ?? 'active',
    created_at: raw.created_at as string | undefined,
    company: raw.company as string | undefined,
    client_id: raw.client_id != null ? String(raw.client_id) : null,
    recruiter_id: raw.recruiter_id != null ? String(raw.recruiter_id) : null,
    connection_id: raw.connection_id != null ? String(raw.connection_id) : null,
    applicants: raw.applicants != null ? Number(raw.applicants) : undefined,
    shortlisted: raw.shortlisted != null ? Number(raw.shortlisted) : undefined,
  }
}

export const getJobs = async (filters?: JobFilters): Promise<Job[]> => {
  try {
    const params = new URLSearchParams()
    if (filters?.skills) params.append('skills', filters.skills)
    if (filters?.location) params.append('location', filters.location)
    if (filters?.experience) params.append('experience', filters.experience)
    if (filters?.job_type) params.append('job_type', filters.job_type)
    if (filters?.search) params.append('search', filters.search)
    
    const response = await api.get(`/v1/jobs?${params.toString()}`)
    const list = response.data.jobs || response.data || []
    return Array.isArray(list) ? list.map((j: Record<string, unknown>) => normalizeJob(j)) : []
  } catch (error) {
    console.error('Error fetching jobs:', error)
    throw error
  }
}

export const getJobById = async (jobId: string): Promise<Job> => {
  try {
    const response = await api.get(`/v1/jobs/${jobId}`)
    return normalizeJob(response.data || {})
  } catch (error) {
    console.error('Error fetching job:', error)
    throw error
  }
}

/** Search-as-you-type: job suggestions by title/department. Requires auth. */
export const getJobSuggestions = async (q: string, limit = 10): Promise<{ id: string; title: string; department: string; location?: string }[]> => {
  const normalizeAutocompleteQuery = (raw: string) =>
    (raw ?? '')
      .replace(/[\\/]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()

  const qNorm = normalizeAutocompleteQuery(q)
  if (!qNorm) return []
  try {
    const response = await api.get('/v1/jobs/autocomplete', { params: { q: qNorm, limit } })
    return response.data?.suggestions ?? []
  } catch (error) {
    console.error('Job suggestions error:', error)
    return []
  }
}

/** Search-as-you-type: candidate suggestions by name/email (recruiter: only their applicants). Requires auth. */
export const getCandidateSuggestions = async (q: string, limit = 10): Promise<{ id: string; name: string; email: string; technical_skills?: string; location?: string }[]> => {
  const res = await getCandidateSuggestionsResponse(q, limit)
  return res.suggestions
}

/** Same as getCandidateSuggestions but returns { suggestions, has_applicants } for recruiter empty-state messaging. */
export const getCandidateSuggestionsResponse = async (
  q: string,
  limit = 10
): Promise<{ suggestions: { id: string; name: string; email: string; technical_skills?: string; location?: string }[]; has_applicants: boolean | null }> => {
  const qNorm = (q ?? '')
    .replace(/[\\/]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!qNorm) return { suggestions: [], has_applicants: null }
  try {
    const response = await api.get('/v1/candidates/autocomplete', { params: { q: qNorm, limit } })
    const suggestions = response.data?.suggestions ?? []
    const hasApplicants = response.data?.has_applicants ?? null
    return { suggestions, has_applicants: hasApplicants }
  } catch (error) {
    console.error('Candidate suggestions error:', error)
    return { suggestions: [], has_applicants: null }
  }
}

/** Search-as-you-type: skill suggestions from active jobs' requirements (for candidate browse jobs skills field). */
export const getJobSkillSuggestions = async (q: string, limit = 15): Promise<{ id: string; label: string }[]> => {
  const qNorm = (q ?? '')
    .replace(/[\\/]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!qNorm) return []
  try {
    const response = await api.get('/v1/jobs/skills/autocomplete', { params: { q: qNorm, limit } })
    return response.data?.suggestions ?? []
  } catch (error) {
    console.error('Job skill suggestions error:', error)
    return []
  }
}

/** Search-as-you-type: location suggestions from active jobs (for candidate browse jobs location field). */
export const getJobLocationSuggestions = async (q: string, limit = 15): Promise<{ id: string; label: string }[]> => {
  const qNorm = (q ?? '')
    .replace(/[\\/]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!qNorm) return []
  try {
    const response = await api.get('/v1/jobs/locations/autocomplete', { params: { q: qNorm, limit } })
    return response.data?.suggestions ?? []
  } catch (error) {
    console.error('Job location suggestions error:', error)
    return []
  }
}

export const createJob = async (jobData: Partial<Job> | Record<string, any>) => {
  try {
    const response = await api.post('/v1/jobs', jobData)
    return response.data
  } catch (error) {
    console.error('Error creating job:', error)
    throw error
  }
}

export const updateJob = async (jobId: string, jobData: Partial<Job> | Record<string, any>) => {
  try {
    const response = await api.put(`/v1/jobs/${jobId}`, jobData)
    return response.data
  } catch (error) {
    console.error('Error updating job:', error)
    throw error
  }
}

export const deleteJob = async (jobId: string) => {
  try {
    const response = await api.delete(`/v1/jobs/${jobId}`)
    return response.data
  } catch (error) {
    console.error('Error deleting job:', error)
    throw error
  }
}

// ==================== CANDIDATE APPLICATIONS API ====================

export interface Application {
  id: string
  job_id: string
  candidate_id: string
  job_title: string
  company?: string
  status: 'applied' | 'screening' | 'shortlisted' | 'interview' | 'offer' | 'rejected' | 'hired'
  match_score?: number
  applied_date: string
  updated_at?: string
  required_documents?: ApplicationDocumentType[]
  required_documents_updated_at?: string
  documents_uploaded?: Partial<Record<ApplicationDocumentType, UploadedApplicationDocument>>
}

export type ApplicationDocumentType = 'resume' | 'nda'

export interface UploadedApplicationDocument {
  document_id: string
  filename: string
  content_type: string
  size_bytes: number
  uploaded_at: string
}

export interface ClientApplicantRecord {
  application_id: string
  job_id: string
  job_title?: string
  status: string
  applied_date?: string
  candidate_id: string
  candidate_name?: string
  candidate_email?: string
  candidate_phone?: string
  candidate_location?: string
  required_documents: ApplicationDocumentType[]
  required_documents_updated_at?: string | null
  documents_uploaded: Partial<Record<ApplicationDocumentType, UploadedApplicationDocument>>
}

export const applyForJob = async (jobId: string, candidateId: string, resumeUrl?: string) => {
  try {
    // Use backend candidate_id if available (integer), otherwise use the provided ID
    const backendCandidateId = authStorage.getItem('backend_candidate_id') || candidateId
    
    const response = await api.post('/v1/candidate/apply', {
      job_id: jobId,
      candidate_id: backendCandidateId,
      resume_url: resumeUrl
    })
    return response.data
  } catch (error: any) {
    // If 422 error (UUID vs integer issue), provide helpful message
    if (error?.response?.status === 422) {
      console.error('Apply failed: Backend requires registered candidate. Please complete profile setup.')
      throw new Error('Please complete your profile setup before applying for jobs.')
    }
    console.error('Error applying for job:', error)
    throw error
  }
}

export const getCandidateApplications = async (candidateId: string): Promise<Application[]> => {
  try {
    console.log('Fetching applications for candidate_id:', candidateId)
    const response = await api.get(`/v1/candidate/applications/${candidateId}`)
    console.log('Applications API response:', response.data)
    const applications = response.data.applications || response.data || []
    console.log('Parsed applications:', applications)
    return applications
  } catch (error: any) {
    console.error('Error fetching applications:', error)
    // Handle 401 (authentication error)
    if (error?.response?.status === 401) {
      console.warn('Authentication failed when fetching applications. Token may be expired.')
      authStorage.removeItem('auth_token')
      return []
    }
    // Handle 403 (forbidden)
    if (error?.response?.status === 403) {
      console.warn('Access denied: Cannot view applications')
      return []
    }
    // Handle 422 error (invalid candidate_id format)
    if (error?.response?.status === 422) {
      console.warn('Applications: Invalid candidate_id format.')
      return []
    }
    // Handle 404 (no applications found)
    if (error?.response?.status === 404) {
      console.log('No applications found (404)')
      return []
    }
    return []
  }
}

export const getClientApplicants = async (): Promise<ClientApplicantRecord[]> => {
  try {
    const response = await api.get('/v1/client/applicants')
    return response.data?.applicants || []
  } catch (error) {
    console.error('Error fetching client applicants:', error)
    return []
  }
}

export const setClientRequiredDocuments = async (
  applicationId: string,
  documentTypes: ApplicationDocumentType[]
): Promise<{ ok: boolean; application_id: string; required_documents: ApplicationDocumentType[] }> => {
  const response = await api.post(`/v1/client/applications/${encodeURIComponent(applicationId)}/required-documents`, {
    document_types: documentTypes,
  })
  return response.data
}

export const uploadCandidateApplicationDocument = async (
  applicationId: string,
  documentType: ApplicationDocumentType,
  file: File
): Promise<{ ok: boolean; document_type: ApplicationDocumentType; document: UploadedApplicationDocument }> => {
  const formData = new FormData()
  formData.append('file', file)
  const response = await api.post(
    `/v1/candidate/applications/${encodeURIComponent(applicationId)}/documents/${encodeURIComponent(documentType)}`,
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' } }
  )
  return response.data
}

export const getClientApplicantDocumentBlob = async (
  applicationId: string,
  documentType: ApplicationDocumentType,
  download: boolean
): Promise<{ blob: Blob; filename: string | null }> => {
  const response = await api.get(
    `/v1/client/applications/${encodeURIComponent(applicationId)}/documents/${encodeURIComponent(documentType)}`,
    {
      params: { download },
      responseType: 'blob',
    }
  )
  const contentDisposition = String(response.headers['content-disposition'] || '')
  const match = contentDisposition.match(/filename="?([^"]+)"?/)
  return { blob: response.data, filename: match?.[1] || null }
}

export const getRecruiterApplicants = async (): Promise<ClientApplicantRecord[]> => {
  try {
    const response = await api.get('/v1/recruiter/applicants')
    return response.data?.applicants || []
  } catch (error) {
    console.error('Error fetching recruiter applicants:', error)
    return []
  }
}

export const setRecruiterRequiredDocuments = async (
  applicationId: string,
  documentTypes: ApplicationDocumentType[]
): Promise<{ ok: boolean; application_id: string; required_documents: ApplicationDocumentType[] }> => {
  const response = await api.post(`/v1/recruiter/applications/${encodeURIComponent(applicationId)}/required-documents`, {
    document_types: documentTypes,
  })
  return response.data
}

export const getRecruiterApplicantDocumentBlob = async (
  applicationId: string,
  documentType: ApplicationDocumentType,
  download: boolean
): Promise<{ blob: Blob; filename: string | null }> => {
  const response = await api.get(
    `/v1/recruiter/applications/${encodeURIComponent(applicationId)}/documents/${encodeURIComponent(documentType)}`,
    {
      params: { download },
      responseType: 'blob',
    }
  )
  const contentDisposition = String(response.headers['content-disposition'] || '')
  const match = contentDisposition.match(/filename="?([^"]+)"?/)
  return { blob: response.data, filename: match?.[1] || null }
}

// ==================== CANDIDATE PROFILE API ====================

export const getCandidateProfile = async (candidateId: string): Promise<CandidateProfile | null> => {
  try {
    // Use the candidate portal endpoint which accepts JWT tokens
    const response = await api.get(`/v1/candidate/profile/${candidateId}`)
    // Backend returns candidate object directly or { error: "..." }
    if (response.data.error) {
      console.warn('Backend returned error:', response.data.error)
      return null
    }
    // The endpoint returns the candidate object directly (not wrapped in { candidate: {...} })
    return response.data
  } catch (error: any) {
    // Handle 401 (authentication error)
    if (error?.response?.status === 401) {
      console.warn('Authentication failed. Token may be expired or invalid.')
      authStorage.removeItem('auth_token')
      return null
    }
    // Handle 403 (forbidden - trying to access another candidate's profile)
    if (error?.response?.status === 403) {
      console.warn('Access denied: You can only view your own profile')
      return null
    }
    // Handle 422 (UUID vs integer mismatch) or 404 (not found)
    if (error?.response?.status === 422 || error?.response?.status === 404) {
      console.warn('Candidate profile not found or invalid ID format')
      return null
    }
    console.error('Error fetching candidate profile:', error)
    return null
  }
}

export const updateCandidateProfile = async (candidateId: string, data: Partial<CandidateProfile>) => {
  try {
    const response = await api.put(`/v1/candidate/profile/${candidateId}`, data)
    return response.data
  } catch (error: any) {
    // Handle 422 (UUID vs integer mismatch)
    if (error?.response?.status === 422) {
      console.warn('Cannot update profile: Backend expects integer candidate_id')
      return { error: 'Profile update not available. Please complete registration.' }
    }
    console.error('Error updating candidate profile:', error)
    throw error
  }
}

// Match endpoint: 60s timeout for AI shortlist (gateway uses AGENT_MATCH_TIMEOUT=90).
const MATCH_REQUEST_TIMEOUT_MS = 90_000

export const getCandidatesByJob = async (jobId: string) => {
  try {
    const response = await api.get(`/v1/match/${jobId}/top`, {
      timeout: MATCH_REQUEST_TIMEOUT_MS
    })
    return response.data.matches || response.data || []
  } catch (error) {
    // Timeout or agent unreachable: return [] so dashboard still loads; avoid flooding console
    if (import.meta.env.DEV) {
      console.warn('Match endpoint failed for job', jobId, '(showing 0 applicants):', (error as Error)?.message || error)
    }
    return []
  }
}

/** Review-candidates endpoint: returns candidates who actually applied to this job (client/recruiter auth isolation enforced by backend). */
export const getCandidatesForJobReview = async (jobId: string, limit: number = 200): Promise<MatchResult[]> => {
  try {
    const response = await api.get('/v1/candidates', {
      params: { job_id: jobId, limit },
      timeout: MATCH_REQUEST_TIMEOUT_MS,
    })
    const raw = response.data?.candidates ?? []
    if (!Array.isArray(raw)) return []
    return raw.map((candidate: any) => {
      const skillsText = String(candidate.technical_skills ?? '').trim()
      const skills = skillsText
        ? skillsText.split(',').map((s: string) => s.trim()).filter(Boolean)
        : []
      const score = Number(candidate.matching_score ?? candidate.match_score ?? 0)
      return {
        candidate_id: String(candidate.candidate_id ?? candidate.id ?? ''),
        candidate_name: String(candidate.name ?? candidate.candidate_name ?? ''),
        email: String(candidate.email ?? ''),
        match_score: Number.isFinite(score) ? score : 0,
        skills_match: skills.length ? Math.min(100, skills.length * 20) : 0,
        experience_match: 0,
        location_match: 0,
        values_score: undefined,
        matched_skills: skills,
        missing_skills: [],
        recommendation: candidate.status ? `Application status: ${candidate.status}` : '',
      } as MatchResult
    })
  } catch (error) {
    console.error('Error fetching candidates for job review:', error)
    return []
  }
}

// ==================== INTERVIEWS API ====================

export interface Interview {
  id: string
  candidate_id: string
  job_id: string
  job_title?: string
  company?: string
  scheduled_date: string
  /** Backend schedule endpoint expects interview_date (combined datetime string). */
  interview_date?: string
  scheduled_time?: string
  interview_type: string
  meeting_link?: string
  meeting_address?: string
  meeting_phone?: string
  status: 'scheduled' | 'completed' | 'cancelled' | 'rescheduled'
  notes?: string
  candidate_name?: string
  /** Backend schedule endpoint. */
  interviewer?: string
}

export const getInterviews = async (candidateId?: string): Promise<Interview[]> => {
  try {
    const params = candidateId ? `?candidate_id=${candidateId}` : ''
    const response = await api.get(`/v1/interviews${params}`)
    return response.data.interviews || response.data || []
  } catch (error: any) {
    // Handle errors gracefully - return empty array
    if (error?.response?.status === 404 || error?.response?.status === 422) {
      return []
    }
    console.error('Error fetching interviews:', error)
    return []
  }
}

export const getAllInterviews = async (): Promise<Interview[]> => {
  try {
    const response = await api.get('/v1/interviews')
    return response.data.interviews || response.data || []
  } catch (error: any) {
    if (error?.response?.status === 404) {
      return []
    }
    console.error('Error fetching all interviews:', error)
    return []
  }
}

export const scheduleInterview = async (data: Partial<Interview>) => {
  try {
    const response = await api.post('/v1/interviews', data)
    return response.data
  } catch (error) {
    console.error('Error scheduling interview:', error)
    throw error
  }
}

// ==================== FEEDBACK API ====================

export interface Feedback {
  id: string
  candidate_id: string
  job_id?: string
  job_title?: string
  interviewer_name?: string
  feedback_text: string
  rating?: number
  values_assessment?: {
    integrity: number
    honesty: number
    discipline: number
    hardWork: number
    gratitude: number
  }
  decision?: 'accept' | 'reject' | 'hold'
  created_at: string
}

export const getCandidateFeedback = async (candidateId: string): Promise<Feedback[]> => {
  try {
    const response = await api.get(`/v1/feedback?candidate_id=${candidateId}`)
    return response.data.feedback || response.data || []
  } catch (error: any) {
    // Handle errors gracefully - return empty array
    if (error?.response?.status === 404 || error?.response?.status === 422) {
      return []
    }
    console.error('Error fetching feedback:', error)
    return []
  }
}

export const submitFeedback = async (candidateId: string, feedbackData: Partial<Feedback>) => {
  try {
    const response = await api.post('/v1/feedback', {
      candidate_id: candidateId,
      ...feedbackData
    })
    return response.data
  } catch (error) {
    console.error('Error submitting feedback:', error)
    throw error
  }
}

// ==================== TASKS API ====================
// Planned alignment with workflow backend (see Complete-Infiverse client lib/api.js):
//   GET/POST/PUT /tasks, GET /users/:id/tasks, submissions routes, etc.

export interface Task {
  id: string
  candidate_id: string
  job_id: string
  job_title?: string
  title: string
  description: string
  deadline: string
  status: 'pending' | 'in_progress' | 'submitted' | 'reviewed'
  submission_url?: string
}

export const getTasks = async (candidateId: string): Promise<Task[]> => {
  try {
    const response = await api.get(`/v1/tasks?candidate_id=${candidateId}`)
    return response.data.tasks || response.data || []
  } catch (error: any) {
    // Tasks endpoint doesn't exist on backend - return empty array
    // Only log in development to reduce console noise
    if (error?.response?.status === 404) {
      if (import.meta.env.DEV) {
        console.warn('Tasks endpoint not available on backend (expected - feature not implemented yet)')
      }
      return []
    }
    console.error('Error fetching tasks:', error)
    return []
  }
}

export const submitTask = async (taskId: string, submissionUrl: string) => {
  try {
    const response = await api.put(`/v1/tasks/${taskId}/submit`, { submission_url: submissionUrl })
    return response.data
  } catch (error: any) {
    // Tasks endpoint doesn't exist on backend
    if (error?.response?.status === 404) {
      const message = 'Task submission feature is not available yet. Please contact support.'
      if (import.meta.env.DEV) {
        console.warn('Tasks submit endpoint not available on backend:', message)
      }
      throw new Error(message)
    }
    console.error('Error submitting task:', error)
    throw error
  }
}

/** Response shape from gateway workflow bridge (Complete-Infiverse). */
export interface WorkflowBridgeTask {
  id: string
  title: string
  description: string
  workflowStatus: string
  priority: string
  progress: number
  dueDate: string | null
  department?: string
  jobTitle?: string
  candidate_id: string
  submission?: {
    id: string
    status?: string
    githubLink?: string
    documentLink?: string
    feedback?: string
  } | null
}

export const fetchCandidateWorkflowTasks = async (): Promise<WorkflowBridgeTask[]> => {
  const response = await api.get<{ tasks?: WorkflowBridgeTask[] }>('/v1/candidate/workflow-tasks')
  return response.data.tasks ?? []
}

export const fetchCandidateWorkflowTaskDetail = async (taskId: string): Promise<WorkflowBridgeTask> => {
  const response = await api.get<WorkflowBridgeTask>(`/v1/candidate/workflow-tasks/${encodeURIComponent(taskId)}`)
  return response.data
}

export const submitCandidateWorkflowTask = async (
  taskId: string,
  submissionUrl: string
): Promise<WorkflowBridgeTask> => {
  const response = await api.post<{ task: WorkflowBridgeTask }>(
    `/v1/candidate/workflow-tasks/${encodeURIComponent(taskId)}/submit`,
    { submission_url: submissionUrl }
  )
  return response.data.task
}

export interface WorkflowLinkStatus {
  linked: boolean
  shared_password_configured: boolean
  workflow_employee_email: string | null
}

export const getWorkflowLinkStatus = async (): Promise<WorkflowLinkStatus> => {
  const response = await api.get<WorkflowLinkStatus>('/v1/candidate/workflow-link-status')
  return response.data
}

export const postWorkflowLink = async (body: {
  password: string
  workflow_employee_email?: string | null
}): Promise<{ ok: boolean; workflow_employee_email: string }> => {
  const response = await api.post<{ ok: boolean; workflow_employee_email: string }>(
    '/v1/candidate/workflow-link',
    body
  )
  return response.data
}

export const deleteWorkflowLink = async (): Promise<{ ok: boolean }> => {
  const response = await api.delete<{ ok: boolean }>('/v1/candidate/workflow-link')
  return response.data
}

// ==================== OFFERS API ====================

export interface Offer {
  id: string
  candidate_id: string
  job_id: string
  job_title?: string
  company?: string
  salary_offered: number
  joining_date?: string
  status: 'pending' | 'accepted' | 'rejected' | 'negotiating'
  created_at: string
}

export const getCandidateOffers = async (candidateId: string): Promise<Offer[]> => {
  try {
    const response = await api.get(`/v1/offers?candidate_id=${candidateId}`)
    return response.data.offers || response.data || []
  } catch (error) {
    console.error('Error fetching offers:', error)
    return []
  }
}

export const getAllOffers = async (): Promise<Offer[]> => {
  try {
    const response = await api.get('/v1/offers')
    return response.data.offers || response.data || []
  } catch (error) {
    console.error('Error fetching all offers:', error)
    return []
  }
}

// ==================== CLIENT PROFILE & CONNECTION ID API ====================
export interface ClientProfile {
  client_id: string
  company_name: string
  email: string
  connection_id: string
}

export const getClientProfile = async (): Promise<ClientProfile | null> => {
  try {
    const response = await api.get('/v1/client/profile')
    return response.data
  } catch (error) {
    console.error('Error fetching client profile:', error)
    return null
  }
}

/** Validate connection_id and get client info (for recruiter job posting). Returns null if invalid. */
export const getClientByConnectionId = async (connectionId: string): Promise<{ client_id: string; company_name: string } | null> => {
  if (!connectionId?.trim()) return null
  const id = connectionId.trim()
  if (id.length !== 24 || !/^[0-9a-fA-F]+$/.test(id)) return null
  try {
    const response = await api.get(`/v1/client/by-connection/${encodeURIComponent(id)}`)
    return response.data
  } catch {
    return null
  }
}

/** Storage key for recruiter's last validated connection (persists across sessions). */
export const RECRUITER_LAST_CONNECTION_KEY = 'recruiter_last_connection'

// ==================== CLIENT DASHBOARD STATS API ====================
/** Lightweight stats for client dashboard (no match/top calls). Match results stay on Match Results page. */
export interface ClientStats {
  active_jobs: number
  total_applications: number
  shortlisted: number
  interviews_scheduled: number
  offers_made: number
  hired: number
}

export const getClientStats = async (): Promise<ClientStats> => {
  try {
    const response = await api.get('/v1/client/stats')
    const d = response.data
    return {
      active_jobs: d.active_jobs ?? 0,
      total_applications: d.total_applications ?? 0,
      shortlisted: d.shortlisted ?? 0,
      interviews_scheduled: d.interviews_scheduled ?? 0,
      offers_made: d.offers_made ?? 0,
      hired: d.hired ?? 0,
    }
  } catch (error) {
    console.error('Error fetching client stats:', error)
    return {
      active_jobs: 0,
      total_applications: 0,
      shortlisted: 0,
      interviews_scheduled: 0,
      offers_made: 0,
      hired: 0,
    }
  }
}

export type ClientConnectedRecruiter = {
  connected_count: number
  status: 'none' | 'connected'
}

export const getClientConnectedRecruiter = async (): Promise<ClientConnectedRecruiter> => {
  try {
    const response = await api.get<{ connected_count?: number; status?: string }>('/v1/client/connected-recruiter')
    const d = response.data
    const count = typeof d?.connected_count === 'number' ? d.connected_count : 0
    const status = d?.status === 'connected' ? 'connected' : 'none'
    return { connected_count: count, status }
  } catch {
    return { connected_count: 0, status: 'none' }
  }
}

/** Establish and lock recruiter–client connection. Call only after validation (getClientByConnectionId). Notifies client so dashboard shows activated only after confirm. */
export const confirmRecruiterConnection = async (connectionId: string): Promise<{ client_id: string; company_name: string }> => {
  const res = await api.post<{ client_id: string; company_name: string }>('/v1/recruiter/confirm-connection', {
    connection_id: connectionId.trim(),
  })
  return res.data
}

/** Call when recruiter explicitly disconnects; notifies client and recruiter via SSE so both see disconnect immediately. */
export const disconnectRecruiterConnection = async (): Promise<void> => {
  await api.post('/v1/recruiter/disconnect')
}

/** Fetch recruiter's current active connection from database. Returns connection_id and company_name if connected, null otherwise. Used on login to restore connection state across devices/browsers. */
export const getRecruiterCurrentConnection = async (): Promise<{ connection_id: string | null; company_name: string | null }> => {
  const res = await api.get<{ connection_id: string | null; company_name: string | null }>('/v1/recruiter/current-connection')
  return res.data
}

/** Bidirectional connection health check. Validates connection status and triggers disconnect events if either party is unavailable. Called every 30 seconds by both client and recruiter. */
export const checkConnectionHealth = async (): Promise<{ healthy: boolean; connected?: boolean; connected_count?: number; reason?: string; disconnected?: boolean }> => {
  const res = await api.post<{ healthy: boolean; connected?: boolean; connected_count?: number; reason?: string; disconnected?: boolean }>('/v1/connection/health-check')
  return res.data
}

export type ConnectionEvent = {
  event: 'connected' | 'disconnected'
  recruiter_name?: string
  company_name?: string
  connected_count?: number
}

/** Subscribe to client connection SSE; use AbortSignal to stop. No polling - both parties get same events. */
export function subscribeClientConnectionEvents(
  onEvent: (data: ConnectionEvent) => void,
  signal?: AbortSignal
): () => void {
  const token = authStorage.getItem('auth_token')
  if (!token) return () => {}
  const url = `${API_BASE_URL}/v1/client/connection-events`
  const controller = new AbortController()
  if (signal) signal.addEventListener('abort', () => controller.abort())
  let buffer = ''
  const run = async () => {
    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
      if (!res.ok || !res.body) return
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += dec.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          const line = part.split('\n').find(l => l.startsWith('data:'))
          if (line) {
            try {
              const data = JSON.parse(line.slice(5).trim()) as ConnectionEvent
              onEvent(data)
            } catch {
              // ignore heartbeat or invalid
            }
          }
        }
      }
    } catch (e) {
      if ((e as { name?: string })?.name !== 'AbortError') {
        console.warn('Client connection-events stream ended:', e)
      }
    }
  }
  run()
  return () => controller.abort()
}

/** Subscribe to recruiter connection SSE; use AbortSignal to stop. No polling - both parties get same events. */
export function subscribeRecruiterConnectionEvents(
  onEvent: (data: ConnectionEvent) => void,
  signal?: AbortSignal
): () => void {
  const token = authStorage.getItem('auth_token')
  if (!token) return () => {}
  const url = `${API_BASE_URL}/v1/recruiter/connection-events`
  const controller = new AbortController()
  if (signal) signal.addEventListener('abort', () => controller.abort())
  let buffer = ''
  const run = async () => {
    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
      if (!res.ok || !res.body) return
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += dec.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          const line = part.split('\n').find(l => l.startsWith('data:'))
          if (line) {
            try {
              const data = JSON.parse(line.slice(5).trim()) as ConnectionEvent
              onEvent(data)
            } catch {
              // ignore
            }
          }
        }
      }
    } catch (e) {
      if ((e as { name?: string })?.name !== 'AbortError') {
        console.warn('Recruiter connection-events stream ended:', e)
      }
    }
  }
  run()
  return () => controller.abort()
}

export const respondToOffer = async (offerId: string, response: 'accepted' | 'rejected') => {
  try {
    const res = await api.put(`/v1/offers/${offerId}`, { status: response })
    return res.data
  } catch (error) {
    console.error('Error responding to offer:', error)
    throw error
  }
}

// ==================== AUTOMATION API ====================

export const triggerAutomation = async (type: string, data?: Record<string, unknown>) => {
  try {
    const response = await api.post('/v1/automation/trigger', {
      type,
      payload: data || {},
    })
    return response.data
  } catch (error) {
    console.error('Error triggering automation:', error)
    throw error
  }
}

export const getNotificationServiceHealth = async () => {
  try {
    const response = await api.get('/v1/notifications/health')
    return response.data
  } catch (error) {
    console.error('Error checking notification service health:', error)
    throw error
  }
}

export const sendNotification = async (payload: Record<string, unknown>) => {
  try {
    return await postNotificationWithRetry('/v1/notifications/send', payload)
  } catch (error) {
    console.error('Error sending notification:', error)
    throw error
  }
}

export const testNotificationSequence = async (payload: Record<string, unknown>) => {
  try {
    return await postNotificationWithRetry('/v1/notifications/test-sequence', payload)
  } catch (error) {
    console.error('Error testing notification sequence:', error)
    throw error
  }
}

export const sendGroupedNotifications = async (payload: Record<string, unknown>) => {
  try {
    return await postNotificationWithRetry('/v1/notifications/send-grouped-by-candidate', payload)
  } catch (error) {
    console.error('Error sending grouped notifications:', error)
    throw error
  }
}

export const sendBulkNotifications = async (payload: Record<string, unknown>) => {
  try {
    return await postNotificationWithRetry('/v1/notifications/bulk', payload)
  } catch (error) {
    console.error('Error sending bulk notifications:', error)
    throw error
  }
}

// ==================== CANDIDATE ACTIONS API ====================

export const shortlistCandidate = async (jobId: string, candidateId: string) => {
  try {
    const response = await api.post(`/v1/jobs/${jobId}/shortlist`, { candidate_id: candidateId })
    return response.data
  } catch (error) {
    console.error('Error shortlisting candidate:', error)
    throw error
  }
}

export const rejectCandidate = async (jobId: string, candidateId: string) => {
  try {
    const response = await api.post(`/v1/jobs/${jobId}/reject`, { candidate_id: candidateId })
    return response.data
  } catch (error) {
    console.error('Error rejecting candidate:', error)
    throw error
  }
}

export const assignTask = async (jobId: string, candidateId: string, taskData: Partial<Task>) => {
  try {
    const response = await api.post('/v1/tasks', {
      job_id: jobId,
      candidate_id: candidateId,
      ...taskData
    })
    return response.data
  } catch (error) {
    console.error('Error assigning task:', error)
    throw error
  }
}

// ==================== DASHBOARD STATS API ====================

export interface DashboardStats {
  total_applications: number
  interviews_scheduled: number
  profile_views: number
  shortlisted: number
  offers_received: number
}

export const getCandidateDashboardStats = async (candidateId: string): Promise<DashboardStats> => {
  try {
    // Try to get stats from dedicated endpoint, fallback to computing from other endpoints
    const response = await api.get(`/v1/candidate/stats/${candidateId}`)
    return response.data
  } catch {
    // Compute stats from other endpoints if dedicated endpoint doesn't exist
    try {
      const [applications, interviews, offers] = await Promise.all([
        getCandidateApplications(candidateId),
        getInterviews(candidateId),
        getCandidateOffers(candidateId)
      ])
      
      return {
        total_applications: applications.length,
        interviews_scheduled: interviews.filter(i => i.status === 'scheduled').length,
        profile_views: 0,
        shortlisted: applications.filter(a => a.status === 'shortlisted').length,
        offers_received: offers.length
      }
    } catch {
      return {
        total_applications: 0,
        interviews_scheduled: 0,
        profile_views: 0,
        shortlisted: 0,
        offers_received: 0
      }
    }
  }
}

// ==================== AI MATCHING ENGINE API ====================

export interface MatchResult {
  candidate_id: string
  candidate_name: string
  email: string
  match_score: number
  skills_match: number
  experience_match: number
  location_match: number
  values_score?: number
  matched_skills: string[]
  missing_skills: string[]
  recommendation: string
}

export interface MatchingStats {
  total_candidates: number
  avg_match_score: number
  high_matches: number
  medium_matches: number
  low_matches: number
}

/** Normalize gateway match response to MatchResult (gateway uses name/score, UI expects candidate_name/match_score/matched_skills). */
function normalizeMatchToResult(m: any): MatchResult {
  const skillsStr = m.skills_match ?? ''
  const matchedSkills = Array.isArray(m.matched_skills)
    ? m.matched_skills
    : (typeof skillsStr === 'string' ? skillsStr.split(',').map((s: string) => s.trim()).filter(Boolean) : [])
  const score = typeof m.score === 'number' ? m.score : (typeof m.match_score === 'number' ? m.match_score : 0)
  const loc = m.location_match
  const locationPct = typeof loc === 'number' ? loc : (loc === true ? 100 : 0)
  const exp = m.experience_match
  const experiencePct = typeof exp === 'number' ? exp : (typeof exp === 'string' && /^\d+/.test(exp) ? parseInt(exp, 10) : 0)
  const skillsPct = typeof m.skills_match === 'number' ? m.skills_match : (matchedSkills.length ? Math.min(100, matchedSkills.length * 25) : 0)
  return {
    candidate_id: m.candidate_id ?? m.id ?? '',
    candidate_name: m.candidate_name ?? m.name ?? '',
    email: m.email ?? '',
    match_score: score,
    skills_match: skillsPct,
    experience_match: experiencePct,
    location_match: locationPct,
    matched_skills: matchedSkills,
    missing_skills: Array.isArray(m.missing_skills) ? m.missing_skills : [],
    recommendation: m.recommendation ?? m.recommendation_strength ?? m.reasoning ?? ''
  }
}

export const getTopMatches = async (jobId: string, limit: number = 10): Promise<MatchResult[]> => {
  try {
    const response = await api.get(`/v1/match/${jobId}/top?limit=${limit}`, {
      timeout: MATCH_REQUEST_TIMEOUT_MS
    })
    const raw = response.data.matches || response.data || []
    return Array.isArray(raw) ? raw.map(normalizeMatchToResult) : []
  } catch (error) {
    console.error('Error fetching top matches:', error)
    throw error
  }
}

export const runBatchMatching = async (jobId: string, candidateIds?: string[]) => {
  try {
    const response = await api.post('/v1/match/batch', {
      job_id: jobId,
      candidate_ids: candidateIds
    })
    return response.data
  } catch (error) {
    console.error('Error running batch matching:', error)
    throw error
  }
}

// ==================== ANALYTICS API ====================

export interface SystemStats {
  total_candidates: number
  total_jobs: number
  total_applications: number
  total_interviews: number
  hiring_rate: number
  avg_time_to_hire: number
}

export interface SkillsAnalytics {
  skill: string
  count: number
  demand: number
}

export interface HiringFunnel {
  stage: string
  count: number
  percentage: number
}

export const getSystemStats = async (): Promise<SystemStats> => {
  try {
    const response = await api.get('/v1/candidates/stats')
    return response.data
  } catch (error) {
    console.error('Error fetching system stats:', error)
    // Return mock data for development
    return {
      total_candidates: 0,
      total_jobs: 0,
      total_applications: 0,
      total_interviews: 0,
      hiring_rate: 0,
      avg_time_to_hire: 0
    }
  }
}

export const getSkillsAnalytics = async (): Promise<SkillsAnalytics[]> => {
  try {
    const response = await api.get('/v1/analytics/skills')
    return response.data.skills || response.data || []
  } catch (error) {
    console.error('Error fetching skills analytics:', error)
    return []
  }
}

export const getHiringFunnel = async (): Promise<HiringFunnel[]> => {
  try {
    const response = await api.get('/v1/analytics/funnel')
    return response.data.funnel || response.data || []
  } catch (error) {
    console.error('Error fetching hiring funnel:', error)
    return []
  }
}

// ==================== RECRUITER API ====================

export interface RecruiterStats {
  total_jobs: number
  total_applicants: number
  shortlisted: number
  interviewed: number
  offers_sent: number
  hired: number
  assessments_completed?: number
}

/** Jobs posted by the logged-in recruiter only (for recruiter dashboard). */
export const getRecruiterJobs = async (): Promise<Job[]> => {
  try {
    const response = await api.get('/v1/recruiter/jobs')
    const list = response.data?.jobs ?? response.data ?? []
    return Array.isArray(list) ? list.map((j: Record<string, unknown>) => normalizeJob(j)) : []
  } catch (error) {
    console.error('Error fetching recruiter jobs:', error)
    return []
  }
}

/** Jobs for the logged-in client only (data isolation). Use in client portal instead of getJobs(). */
export const getClientJobs = async (): Promise<Job[]> => {
  try {
    const response = await api.get('/v1/client/jobs')
    const list = response.data?.jobs ?? response.data ?? []
    return Array.isArray(list) ? list.map((j: Record<string, unknown>) => normalizeJob(j)) : []
  } catch (error) {
    console.error('Error fetching client jobs:', error)
    return []
  }
}

export const getRecruiterStats = async (): Promise<RecruiterStats> => {
  try {
    const response = await api.get('/v1/recruiter/stats')
    return response.data
  } catch {
    try {
      const jobs = await getRecruiterJobs()
      return {
        total_jobs: jobs.length,
        total_applicants: 0,
        shortlisted: 0,
        interviewed: 0,
        offers_sent: 0,
        hired: 0,
        assessments_completed: 0
      }
    } catch {
      return {
        total_jobs: 0,
        total_applicants: 0,
        shortlisted: 0,
        interviewed: 0,
        offers_sent: 0,
        hired: 0,
        assessments_completed: 0
      }
    }
  }
}

export interface CandidateFilters {
  skills?: string
  experience?: string
  location?: string
  search?: string
  status?: string | string[]
  has_interview?: boolean
  interview_date_gte?: string
  interview_date_lt?: string
  matching_score_gte?: number
  created_at_gte?: string
  feedback_submitted?: boolean
  exclude_statuses?: string[]
  limit?: number
  recruiter_id?: string
  client_id?: string  // Added for client data isolation
  include_never_applied?: boolean  // Include candidates who never applied to any job
  job_id?: string  // Filter candidates by specific job
}

export const getAllCandidates = async (filters?: CandidateFilters) => {
  try {
    const params = new URLSearchParams()
    
    if (filters) {
      // Legacy filters
      if (filters.skills) params.append('skills', filters.skills)
      if (filters.experience) params.append('experience', filters.experience)
      if (filters.location) params.append('location', filters.location)
      if (filters.search) params.append('search', filters.search)
      
      // New notification filtering
      if (filters.status) {
        const statusParam = Array.isArray(filters.status) 
          ? filters.status.join(',') 
          : filters.status
        params.append('status', statusParam)
      }
      
      if (filters.has_interview !== undefined) {
        params.append('has_interview', filters.has_interview.toString())
      }
      
      if (filters.interview_date_gte) {
        params.append('interview_date_gte', filters.interview_date_gte)
      }
      
      if (filters.interview_date_lt) {
        params.append('interview_date_lt', filters.interview_date_lt)
      }
      
      if (filters.matching_score_gte) {
        params.append('matching_score_gte', filters.matching_score_gte.toString())
      }
      
      if (filters.created_at_gte) {
        params.append('created_at_gte', filters.created_at_gte)
      }
      
      if (filters.feedback_submitted !== undefined) {
        params.append('feedback_submitted', filters.feedback_submitted.toString())
      }
      
      if (filters.exclude_statuses && filters.exclude_statuses.length > 0) {
        params.append('exclude_statuses', filters.exclude_statuses.join(','))
      }
      
      if (filters.limit) {
        params.append('limit', filters.limit.toString())
      }
      
      if (filters.recruiter_id) {
        params.append('recruiter_id', filters.recruiter_id)
      }
      
      if (filters.client_id) {
        params.append('client_id', filters.client_id)
      }
      
      if (filters.include_never_applied !== undefined) {
        params.append('include_never_applied', filters.include_never_applied.toString())
      }
      
      if (filters.job_id) {
        params.append('job_id', filters.job_id)
      }
    }
    
    const queryString = params.toString()
    const url = queryString ? `/v1/candidates?${queryString}` : '/v1/candidates'
    
    const response = await api.get(url)
    return response.data.candidates || response.data || []
  } catch (error) {
    console.error('Error fetching candidates:', error)
    throw error
  }
}

export const getNotificationHistory = async (candidateId: string) => {
  try {
    const response = await api.get(`/v1/notifications/history/${candidateId}`)
    return response.data.history || []
  } catch (error) {
    console.error('Error fetching notification history:', error)
    return []
  }
}

export interface PortalNotification {
  id: string
  title: string
  message: string
  kind: string
  is_read: boolean
  created_at: string
  payload?: Record<string, unknown>
}

export const getPortalNotifications = async (limit = 30): Promise<{ notifications: PortalNotification[]; unread_count: number }> => {
  const response = await api.get('/v1/portal/notifications', { params: { limit } })
  return {
    notifications: response.data?.notifications || [],
    unread_count: Number(response.data?.unread_count || 0),
  }
}

export const markPortalNotificationRead = async (notificationId: string): Promise<void> => {
  await api.post(`/v1/portal/notifications/${encodeURIComponent(notificationId)}/read`)
}

export const markAllPortalNotificationsRead = async (): Promise<void> => {
  await api.post('/v1/portal/notifications/read-all')
}

export const previewNotification = async (notificationType: string, sampleData?: any) => {
  try {
    const payload = {
      sequence_type: notificationType,
      candidate_name: sampleData?.candidate_name || 'John Doe',
      job_title: sampleData?.job_title || 'Software Engineer',
      job_id: sampleData?.job_id || 'job_123',
      matching_score: sampleData?.matching_score || '85',
      interview_date: sampleData?.interview_date || '2024-02-15',
      interview_time: sampleData?.interview_time || '2:00 PM',
      interviewer: sampleData?.interviewer || 'HR Team',
      application_id: sampleData?.application_id || 'APP_001'
    }

    const response = await api.post('/v1/notifications/preview', payload)
    return response.data
  } catch (error) {
    console.error('Error fetching notification preview:', error)
    throw error
  }
}

export interface SearchCandidatesResult {
  candidates: any[]
  total: number
}

export const searchCandidates = async (
  query: string,
  filters?: {
    job_id?: string
    skills?: string
    location?: string
    experience_min?: number
    experience_max?: number
    education_level?: string
    seniority_level?: string
    status?: string
    search?: string
    limit?: number
    offset?: number
  }
): Promise<SearchCandidatesResult> => {
  try {
    const searchTerm = (query ?? filters?.search ?? '').toString().trim()
    const params: Record<string, string | number | undefined> = { query: searchTerm }
    if (filters?.job_id) params.job_id = filters.job_id
    if (filters?.skills) params.skills = filters.skills
    if (filters?.location) params.location = filters.location
    if (filters?.experience_min !== undefined) params.experience_min = filters.experience_min
    if (filters?.experience_max !== undefined) params.experience_max = filters.experience_max
    if (filters?.education_level) params.education_level = filters.education_level
    if (filters?.seniority_level) params.seniority_level = filters.seniority_level
    if (filters?.status) params.status = filters.status
    if (filters?.limit !== undefined) params.limit = filters.limit
    if (filters?.offset !== undefined) params.offset = filters.offset
    const response = await api.get('/v1/candidates/search', { params })
    const candidates = response.data.candidates || response.data || []
    const total = response.data.total ?? candidates.length
    return { candidates: Array.isArray(candidates) ? candidates : [], total }
  } catch (error) {
    console.error('Error searching candidates:', error)
    throw error
  }
}

// ==================== BULK UPLOAD API ====================

export interface BulkCandidate {
  name: string
  email: string
  cv_url?: string
  phone?: string
  experience_years?: number
  status?: string
  location?: string
  technical_skills?: string
  designation?: string
  education_level?: string
}

/** Parse a PDF file on the server and return candidate-like rows for editable preview. */
export const parsePdfCandidates = async (file: File): Promise<{ rows: Record<string, string>[] }> => {
  const formData = new FormData()
  formData.append('file', file)
  const response = await api.post('/v1/candidates/parse-pdf', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000
  })
  return { rows: response.data?.rows ?? [] }
}

/** Check which candidate emails already exist in the database */
export const checkDuplicateCandidates = async (
  emails: string[], 
  signal?: AbortSignal
): Promise<{ duplicates: string[]; count: number }> => {
  try {
    const response = await api.post('/v1/candidates/check-duplicates', emails, {
      signal // Pass AbortSignal to axios for request cancellation
    })
    return response.data
  } catch (error) {
    console.error('Error checking duplicate candidates:', error)
    throw error
  }
}

/** Bulk upload candidates for a job. job_id links applicants to the job so dashboard stats stay in sync. */
export const bulkUploadCandidates = async (candidates: BulkCandidate[], jobId: string) => {
  try {
    const response = await api.post('/v1/candidates/bulk', { candidates, job_id: jobId })
    return response.data
  } catch (error) {
    console.error('Error uploading candidates:', error)
    throw error
  }
}

// ==================== CLIENT PORTAL API ====================

export interface ShortlistedCandidate {
  id: string
  name: string
  email: string
  job_title: string
  match_score: number
  skills: string[]
  experience_years: number
  status: 'pending_review' | 'approved' | 'rejected'
  recruiter_notes?: string
}

export const getShortlistedCandidates = async (clientId?: string): Promise<ShortlistedCandidate[]> => {
  try {
    const params = clientId ? `?client_id=${clientId}` : ''
    const response = await api.get(`/v1/client/shortlist${params}`)
    return response.data.candidates || response.data || []
  } catch (error) {
    console.error('Error fetching shortlisted candidates:', error)
    return []
  }
}

export const reviewCandidate = async (candidateId: string, decision: 'approved' | 'rejected', notes?: string) => {
  try {
    const response = await api.post(`/v1/client/review/${candidateId}`, {
      decision,
      notes
    })
    return response.data
  } catch (error) {
    console.error('Error reviewing candidate:', error)
    throw error
  }
}

// ==================== HEALTH CHECK API ====================

export const checkApiHealth = async () => {
  try {
    const response = await api.get('/health')
    return { healthy: true, data: response.data }
  } catch (error) {
    return { healthy: false, error }
  }
}

export const getDetailedHealth = async () => {
  try {
    const response = await api.get('/health/detailed')
    return response.data
  } catch (error) {
    console.error('Error fetching health status:', error)
    throw error
  }
}

// ==================== TENANCY HIERARCHY API ====================
// platform -> key account -> account (account isolation layer)

export interface TenantClientAccount {
  client_id: string
  company_name?: string
  email?: string
  status?: string
}

export interface TenantNode {
  tenant_id: string
  name: string
  level: 'key_account' | 'account'
  parent_tenant_id: string
  path: string
  client_ids: string[]
  status: 'active' | 'suspended'
  created_at?: string | null
  client_accounts?: TenantClientAccount[]
  children?: TenantNode[]
}

export interface TenantInfo {
  success: boolean
  scope: 'platform' | 'client'
  linked: boolean
  client_id?: string
  tenant_id?: string
  name?: string
  level?: string | null
  parent_tenant_id?: string
  path?: string
  status?: string
  is_key_account: boolean
  client_ids?: string[]
  visible_client_ids?: string[]
  sub_client_count?: number
}

export interface SubClient {
  tenant_id: string
  name: string
  level: string
  status: string
  path: string
  client_ids: string[]
  client_accounts?: TenantClientAccount[]
  created_at?: string | null
}

export const fetchTenantInfo = async (): Promise<TenantInfo> => {
  const response = await api.get('/v1/tenant/info', { headers: getAuthHeaders() })
  return response.data
}

export const fetchTenantSubClients = async (): Promise<{
  tenant: { tenant_id: string; name: string; path: string }
  sub_clients: SubClient[]
  count: number
  visible_client_ids: string[]
}> => {
  const response = await api.get('/v1/tenant/sub-clients', { headers: getAuthHeaders() })
  return response.data
}

export interface CreateSubClientPayload {
  name: string
  email: string
  password?: string
  company_name?: string
}

export const createTenantSubClient = async (payload: CreateSubClientPayload) => {
  const response = await api.post('/v1/tenant/sub-clients', payload, { headers: getAuthHeaders() })
  return response.data
}

export interface AdminTenantsResponse {
  success: boolean
  tree: TenantNode[]
  tenants: TenantNode[]
  counts: { key_accounts: number; accounts: number; linked_clients: number }
}

export const fetchAdminTenants = async (): Promise<AdminTenantsResponse> => {
  const response = await api.get('/v1/admin/tenants', { headers: getAuthHeaders() })
  return response.data
}

export interface CreateTenantPayload {
  name: string
  level: 'key_account' | 'account'
  tenant_id?: string
  parent_tenant_id?: string
  client_id?: string
  provision_client?: { email: string; password?: string; company_name?: string }
}

export const createAdminTenant = async (payload: CreateTenantPayload) => {
  const response = await api.post('/v1/admin/tenants', payload, { headers: getAuthHeaders() })
  return response.data
}

export const updateAdminTenant = async (
  tenantId: string,
  payload: { name?: string; status?: string }
) => {
  const response = await api.patch(`/v1/admin/tenants/${encodeURIComponent(tenantId)}`, payload, {
    headers: getAuthHeaders(),
  })
  return response.data
}

export const linkTenantClient = async (tenantId: string, clientId: string) => {
  const response = await api.post(
    `/v1/admin/tenants/${encodeURIComponent(tenantId)}/link-client`,
    { client_id: clientId },
    { headers: getAuthHeaders() }
  )
  return response.data
}

export interface AdminClientAccount {
  client_id: string
  company_name: string
  email: string
  status: string
  client_code: string
  created_at: string | null
  linked_tenant_id: string | null
}

export const fetchAdminClients = async (
  search = ''
): Promise<{ success: boolean; count: number; clients: AdminClientAccount[] }> => {
  const response = await api.get('/v1/admin/clients', {
    params: search ? { search } : {},
    headers: getAuthHeaders(),
  })
  return response.data
}

// ---------------------------------------------------------------------------
// Candidate NDA gate (Phase 5D)
// ---------------------------------------------------------------------------

export type NdaStatusValue = 'NOT_SUBMITTED' | 'NEEDS_REVIEW' | 'VERIFIED' | 'REJECTED'

export interface NdaVerification {
  result: NdaStatusValue
  reasons: string[]
  method: string
  checked_at: string
  admin_override?: NdaStatusValue
}

export interface NdaStatus {
  candidate_id: string
  status: NdaStatusValue
  gate_passed: boolean
  verification: NdaVerification | null
  document: {
    filename: string
    content_type: string
    size_bytes: number
    uploaded_at: string
  } | null
  review: {
    reviewed_by: string
    reviewed_at: string
    decision: string
    note?: string
  } | null
  updated_at: string | null
}

export interface NdaUploadResult {
  ok: boolean
  status: NdaStatusValue
  gate_passed: boolean
  verification: NdaVerification
  document: NdaStatus['document']
  message: string
}

export const fetchNdaStatus = async (): Promise<NdaStatus> => {
  const response = await api.get('/v1/nda/status', { headers: getAuthHeaders() })
  return response.data
}

export const uploadNda = async (file: File): Promise<NdaUploadResult> => {
  const form = new FormData()
  form.append('file', file)
  const response = await api.post('/v1/nda/upload', form, {
    headers: { ...getAuthHeaders(), 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  })
  return response.data
}

/**
 * Open the official NDA template in a new tab (fetched WITH the auth header,
 * then handed to the tab as a blob URL so "Print / Save as PDF" works).
 */
export const openNdaTemplate = async (): Promise<void> => {
  const response = await api.get('/v1/nda/template', {
    headers: getAuthHeaders(),
    responseType: 'text',
    transformResponse: [(data) => data],
  })
  const blob = new Blob([response.data as unknown as string], { type: 'text/html' })
  const url = URL.createObjectURL(blob)
  const win = window.open(url, '_blank', 'noopener')
  if (!win) {
    // Popup blocked: trigger a download instead so the candidate still gets it.
    const a = document.createElement('a')
    a.href = url
    a.target = '_blank'
    a.rel = 'noopener'
    a.click()
  }
  // Give the new tab time to load before the object URL is reclaimed.
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}

export default api
