import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import toast from 'react-hot-toast'
import { getCandidateApplications, uploadCandidateApplicationDocument, type Application, type ApplicationDocumentType } from '../../services/api'
import { useAuth } from '../../context/AuthContext'
import { authStorage } from '../../utils/authStorage'

type StatusFilter = 'all' | 'applied' | 'screening' | 'shortlisted' | 'interview' | 'offer' | 'rejected' | 'hired'

export default function AppliedJobs() {
  const { user } = useAuth()
  const [applications, setApplications] = useState<Application[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [selectedApp, setSelectedApp] = useState<Application | null>(null)
  const [uploadModalOpen, setUploadModalOpen] = useState(false)
  const [uploadDocType, setUploadDocType] = useState<ApplicationDocumentType | null>(null)
  const [pendingUploadFile, setPendingUploadFile] = useState<File | null>(null)
  const [uploadingDocKey, setUploadingDocKey] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  // Deep link from a "Documents requested by client" notification:
  // /candidate/applied-jobs?application=<id>
  const deepLinkApplicationId = searchParams.get('application')

  const loadApplications = useCallback(async () => {
    // Check authentication first
    const isAuthenticated = authStorage.getItem('isAuthenticated') === 'true' || !!user
    
    if (!isAuthenticated) {
      toast.error('Please login to view your applications')
      setLoading(false)
      return
    }

    const currentBackendId = authStorage.getItem('backend_candidate_id')
    
    // If authenticated but no candidate ID, show empty state
    if (!currentBackendId) {
      setApplications([])
      setLoading(false)
      return
    }

    try {
      setLoading(true)
      console.log('Loading applications for candidate:', currentBackendId)
      // Use backend_candidate_id (ObjectId string) for applications
      const data = await getCandidateApplications(currentBackendId)
      console.log('Loaded applications:', data)
      setApplications(data || [])
    } catch (error) {
      console.error('Failed to load applications:', error)
      setApplications([])
    } finally {
      setLoading(false)
    }
  }, [user])

  useEffect(() => {
    loadApplications()
  }, [loadApplications])

  useEffect(() => {
    if (!selectedApp) return
    const refreshed = applications.find((app) => app.id === selectedApp.id)
    if (refreshed) {
      setSelectedApp(refreshed)
    }
  }, [applications, selectedApp])

  // Reload applications when component becomes visible or tab is focused
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        loadApplications()
      }
    }

    const handleFocus = () => {
      loadApplications()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('focus', handleFocus)
    
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      window.removeEventListener('focus', handleFocus)
    }
  }, [loadApplications])

  useEffect(() => {
    const onNotificationsUpdated = (event: Event) => {
      const customEvent = event as CustomEvent<{ role?: string }>
      if (customEvent.detail?.role !== 'candidate') return
      void loadApplications()
    }
    window.addEventListener('portal-notifications-updated', onNotificationsUpdated)
    return () => window.removeEventListener('portal-notifications-updated', onNotificationsUpdated)
  }, [loadApplications])

  // Deep link: notification "Documents requested by client" opens the
  // application straight into its upload flow (?application=<id>).
  useEffect(() => {
    if (!deepLinkApplicationId || loading) return
    const target = applications.find((app) => app.id === deepLinkApplicationId)
    if (target) {
      setSelectedApp(target)
      const hasPendingRequest = (target.required_documents || []).some(
        (doc) => !isDocSubmissionLocked(target, doc)
      )
      if (hasPendingRequest) {
        setUploadDocType(null)
        setPendingUploadFile(null)
        setUploadModalOpen(true)
      }
    }
    // Consume the param so a refresh does not reopen the modal.
    setSearchParams({}, { replace: true })
  }, [deepLinkApplicationId, loading, applications, setSearchParams])

  useEffect(() => {
    const interval = setInterval(() => {
      void loadApplications()
    }, 30000)
    return () => clearInterval(interval)
  }, [loadApplications])

  const getStatusConfig = (status: string) => {
    const configs: Record<string, { color: string; label: string }> = {
      applied: { 
        color: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400', 
        label: 'Applied' 
      },
      screening: { 
        color: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400', 
        label: 'Screening' 
      },
      shortlisted: { 
        color: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400', 
        label: 'Shortlisted' 
      },
      interview: { 
        color: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400', 
        label: 'Interview' 
      },
      offer: { 
        color: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400', 
        label: 'Offer' 
      },
      rejected: { 
        color: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400', 
        label: 'Rejected' 
      },
      hired: { 
        color: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400', 
        label: 'Hired' 
      },
    }
    return configs[status.toLowerCase()] || configs.applied
  }

  const filteredApplications = statusFilter === 'all' 
    ? applications 
    : applications.filter(app => app.status.toLowerCase() === statusFilter)

  const stats = {
    total: applications.length,
    applied: applications.filter(a => a.status === 'applied').length,
    screening: applications.filter(a => a.status === 'screening').length,
    shortlisted: applications.filter(a => a.status === 'shortlisted').length,
    interview: applications.filter(a => a.status === 'interview').length,
    offer: applications.filter(a => a.status === 'offer').length,
    rejected: applications.filter(a => a.status === 'rejected').length,
    hired: applications.filter(a => a.status === 'hired').length,
  }

  const statusFilters: { value: StatusFilter; label: string; count: number }[] = [
    { value: 'all', label: 'All', count: stats.total },
    { value: 'applied', label: 'Applied', count: stats.applied },
    { value: 'screening', label: 'Screening', count: stats.screening },
    { value: 'shortlisted', label: 'Shortlisted', count: stats.shortlisted },
    { value: 'interview', label: 'Interview', count: stats.interview },
    { value: 'offer', label: 'Offers', count: stats.offer },
    { value: 'hired', label: 'Hired', count: stats.hired },
  ]

  const documentLabel = (docType: ApplicationDocumentType) => (
    docType === 'resume' ? 'CV / Resume' : 'NDA'
  )

  const handlePickDocument = (docType: ApplicationDocumentType) => {
    if (!selectedApp) return
    const input = document.getElementById(`upload-modal-${selectedApp.id}-${docType}`) as HTMLInputElement | null
    input?.click()
  }

  const parseIsoMillis = (value?: string | null): number => {
    if (!value) return 0
    const t = new Date(value).getTime()
    return Number.isFinite(t) ? t : 0
  }

  const isDocSubmissionLocked = (app: Application, docType: ApplicationDocumentType): boolean => {
    const requested = Array.isArray(app.required_documents) && app.required_documents.includes(docType)
    if (!requested) return true
    const requestedAt = parseIsoMillis(app.required_documents_updated_at || null)
    const uploadedAt = parseIsoMillis(app.documents_uploaded?.[docType]?.uploaded_at || null)
    // Locked once submitted for latest request.
    if (requestedAt <= 0) {
      return uploadedAt > 0
    }
    return uploadedAt >= requestedAt && uploadedAt !== 0
  }

  const documentBadge = (submitted: boolean) => (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold ${
        submitted
          ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
          : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
      }`}
    >
      {submitted ? 'Submitted' : 'Pending'}
    </span>
  )

  const openUploadModal = () => {
    if (!selectedApp) return
    setUploadDocType(null)
    setPendingUploadFile(null)
    setUploadModalOpen(true)
  }

  // Open an application's upload flow directly from the list (pending request).
  const openRequestUpload = (app: Application) => {
    setSelectedApp(app)
    setUploadDocType(null)
    setPendingUploadFile(null)
    setUploadModalOpen(true)
  }

  const closeUploadModal = () => {
    if (uploadingDocKey) return
    setUploadModalOpen(false)
    setUploadDocType(null)
    setPendingUploadFile(null)
  }

  const handleUploadDocument = async () => {
    if (!selectedApp || !uploadDocType || !pendingUploadFile) {
      toast.error('Select a document type and choose a file first.')
      return
    }
    try {
      setUploadingDocKey(`${selectedApp.id}:${uploadDocType}`)
      await uploadCandidateApplicationDocument(selectedApp.id, uploadDocType, pendingUploadFile)
      toast.success(`${documentLabel(uploadDocType)} uploaded successfully`)
      await loadApplications()
      setPendingUploadFile(null)
      setUploadDocType(null)
    } catch (error: any) {
      const msg = error?.response?.data?.detail || error?.message || 'Failed to upload document'
      toast.error(msg)
    } finally {
      setUploadingDocKey(null)
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="p-6 rounded-2xl bg-gradient-to-r from-blue-500/5 to-cyan-500/5 dark:from-blue-500/10 dark:to-cyan-500/10 backdrop-blur-xl border border-blue-300/20 dark:border-blue-500/20">
        <h1 className="page-title">My Applications</h1>
        <p className="page-subtitle">Track and manage all your job applications</p>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-white dark:bg-slate-800 rounded-xl p-4 shadow-sm border border-gray-100 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
              <svg className="w-5 h-5 text-blue-600 dark:text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">{stats.total}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">Total Applied</p>
            </div>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 rounded-xl p-4 shadow-sm border border-gray-100 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-emerald-100 dark:bg-emerald-900/30 flex items-center justify-center">
              <svg className="w-5 h-5 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" /></svg>
            </div>
            <div>
              <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400">{stats.shortlisted}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">Shortlisted</p>
            </div>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 rounded-xl p-4 shadow-sm border border-gray-100 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
              <svg className="w-5 h-5 text-purple-600 dark:text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
            </div>
            <div>
              <p className="text-2xl font-bold text-purple-600 dark:text-purple-400">{stats.interview}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">Interviews</p>
            </div>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 rounded-xl p-4 shadow-sm border border-gray-100 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-100 dark:bg-cyan-900/30 flex items-center justify-center">
              <svg className="w-5 h-5 text-cyan-600 dark:text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z" /></svg>
            </div>
            <div>
              <p className="text-2xl font-bold text-cyan-600 dark:text-cyan-400">{stats.offer}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">Offers</p>
            </div>
          </div>
        </div>
      </div>

      {/* Filter Tabs */}
      <div className="bg-white dark:bg-slate-800 rounded-xl p-2 shadow-sm border border-gray-100 dark:border-slate-700">
        <div className="flex flex-wrap gap-2">
          {statusFilters.map(filter => (
            <button
              key={filter.value}
              onClick={() => setStatusFilter(filter.value)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                statusFilter === filter.value
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-slate-700'
              }`}
            >
              {filter.label}
              <span className={`ml-2 px-1.5 py-0.5 rounded text-xs ${
                statusFilter === filter.value 
                  ? 'bg-blue-400 text-white' 
                  : 'bg-gray-200 dark:bg-slate-600 text-gray-600 dark:text-gray-300'
              }`}>
                {filter.count}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Applications List */}
      {loading ? (
        <div className="space-y-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="bg-white dark:bg-slate-800 rounded-xl p-6 animate-pulse">
              <div className="flex justify-between">
                <div className="space-y-2 flex-1">
                  <div className="h-5 bg-gray-200 dark:bg-slate-700 rounded w-1/3"></div>
                  <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-1/4"></div>
                </div>
                <div className="h-6 w-20 bg-gray-200 dark:bg-slate-700 rounded"></div>
              </div>
            </div>
          ))}
        </div>
      ) : filteredApplications.length === 0 ? (
        <div className="bg-white dark:bg-slate-800 rounded-2xl p-12 text-center shadow-sm border border-gray-100 dark:border-slate-700">
          <svg className="w-16 h-16 mx-auto text-gray-400 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          <h3 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">No applications found</h3>
          <p className="text-gray-500 dark:text-gray-400">
            {statusFilter === 'all' 
              ? "You haven't applied to any jobs yet. Start exploring!" 
              : `No applications with "${statusFilter}" status`}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {filteredApplications.map(app => {
            const statusConfig = getStatusConfig(app.status)
            const pendingDocs = (app.required_documents || []).filter((doc) => !isDocSubmissionLocked(app, doc))
            return (
              <div
                key={app.id}
                className="bg-white dark:bg-slate-800 rounded-xl p-6 shadow-sm border border-gray-100 dark:border-slate-700 hover:shadow-md transition-all"
              >
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                  <div className="flex-1">
                    <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{app.job_title}</h3>
                    <p className="text-gray-600 dark:text-gray-400">{app.company || 'Company'}</p>
                    <div className="flex items-center gap-4 mt-2 text-sm text-gray-500 dark:text-gray-400">
                      <span className="flex items-center gap-1">
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                        </svg>
                        Applied: {new Date(app.applied_date).toLocaleDateString()}
                      </span>
                      {app.match_score && (
                        <span className="flex items-center gap-1">
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                          </svg>
                          Match: {app.match_score}%
                        </span>
                      )}
                    </div>
                  </div>
                  
                  <div className="flex flex-wrap items-center gap-3">
                    <span className={`px-3 py-1 rounded-full text-sm font-medium ${statusConfig.color}`}>
                      {statusConfig.label}
                    </span>
                    {pendingDocs.length > 0 && (
                      <span
                        className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
                        title={pendingDocs.map((doc) => documentLabel(doc)).join(', ')}
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                        </svg>
                        {pendingDocs.length === 1
                          ? `${documentLabel(pendingDocs[0])} requested`
                          : `${pendingDocs.length} documents requested`}
                      </span>
                    )}
                    <button
                      onClick={() => setSelectedApp(app)}
                      className="px-4 py-2 bg-gray-100 dark:bg-slate-700 hover:bg-gray-200 dark:hover:bg-slate-600 text-gray-700 dark:text-gray-300 rounded-lg text-sm font-medium transition-colors"
                    >
                      View Details
                    </button>
                    {pendingDocs.length > 0 && (
                      <button
                        onClick={() => openRequestUpload(app)}
                        className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
                      >
                        Upload {pendingDocs.length === 1 ? documentLabel(pendingDocs[0]) : 'Documents'}
                      </button>
                    )}
                  </div>
                </div>

                {/* Match Score Progress Bar */}
                {app.match_score && (
                  <div className="mt-4 pt-4 border-t border-gray-100 dark:border-slate-700">
                    <div className="flex items-center justify-between text-sm mb-1">
                      <span className="text-gray-600 dark:text-gray-400">Match Score</span>
                      <span className={`font-semibold ${
                        app.match_score >= 80 ? 'text-emerald-600' :
                        app.match_score >= 60 ? 'text-amber-600' : 'text-red-600'
                      }`}>{app.match_score}%</span>
                    </div>
                    <div className="w-full bg-gray-200 dark:bg-slate-700 rounded-full h-2">
                      <div
                        className={`h-2 rounded-full transition-all ${
                          app.match_score >= 80 ? 'bg-emerald-500' :
                          app.match_score >= 60 ? 'bg-amber-500' : 'bg-red-500'
                        }`}
                        style={{ width: `${app.match_score}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Application Details Modal */}
      {selectedApp && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-800 rounded-2xl max-w-lg w-full">
            <div className="p-6 border-b border-gray-100 dark:border-slate-700">
              <div className="flex items-start justify-between">
                <div>
                  <h2 className="text-xl font-bold text-gray-900 dark:text-white">{selectedApp.job_title}</h2>
                  <p className="text-gray-600 dark:text-gray-400">{selectedApp.company || 'Company'}</p>
                </div>
                <button
                  onClick={() => setSelectedApp(null)}
                  className="p-2 hover:bg-gray-100 dark:hover:bg-slate-700 rounded-lg transition-colors"
                >
                  <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>
            
            <div className="p-6 space-y-4">
              <div className="flex items-center justify-between p-4 bg-gray-50 dark:bg-slate-700/50 rounded-lg">
                <span className="text-gray-600 dark:text-gray-400">Status</span>
                <span className={`px-3 py-1 rounded-full text-sm font-medium ${getStatusConfig(selectedApp.status).color}`}>
                  {getStatusConfig(selectedApp.status).label}
                </span>
              </div>
              
              <div className="flex items-center justify-between p-4 bg-gray-50 dark:bg-slate-700/50 rounded-lg">
                <span className="text-gray-600 dark:text-gray-400">Applied Date</span>
                <span className="font-medium text-gray-900 dark:text-white">
                  {new Date(selectedApp.applied_date).toLocaleDateString()}
                </span>
              </div>
              
              {selectedApp.match_score && (
                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-gray-600 dark:text-gray-400">Match Score</span>
                    <span className={`font-bold ${
                      selectedApp.match_score >= 80 ? 'text-emerald-600' :
                      selectedApp.match_score >= 60 ? 'text-amber-600' : 'text-red-600'
                    }`}>{selectedApp.match_score}%</span>
                  </div>
                  <div className="w-full bg-gray-200 dark:bg-slate-600 rounded-full h-3">
                    <div
                      className={`h-3 rounded-full ${
                        selectedApp.match_score >= 80 ? 'bg-emerald-500' :
                        selectedApp.match_score >= 60 ? 'bg-amber-500' : 'bg-red-500'
                      }`}
                      style={{ width: `${selectedApp.match_score}%` }}
                    />
                  </div>
                </div>
              )}

              {selectedApp.updated_at && (
                <div className="flex items-center justify-between p-4 bg-gray-50 dark:bg-slate-700/50 rounded-lg">
                  <span className="text-gray-600 dark:text-gray-400">Last Updated</span>
                  <span className="font-medium text-gray-900 dark:text-white">
                    {new Date(selectedApp.updated_at).toLocaleDateString()}
                  </span>
                </div>
              )}

              {/* Upload Documents Section */}
              <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-lg space-y-3">
                {(() => {
                  const requiredDocTypes = (selectedApp.required_documents || []) as ApplicationDocumentType[]
                  const submittedDocTypes = (Object.keys(selectedApp.documents_uploaded || {}) as ApplicationDocumentType[])
                  const displayDocTypes = Array.from(new Set([...requiredDocTypes, ...submittedDocTypes]))
                  const hasAnyDocContext = displayDocTypes.length > 0
                  if (!hasAnyDocContext) {
                    return (
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        No document requests have been made by the client yet.
                      </p>
                    )
                  }
                  return (
                    <>
                <div className="flex items-center justify-between">
                  <span className="text-gray-700 dark:text-gray-300 font-medium">Upload Documents</span>
                  <button
                    onClick={openUploadModal}
                    disabled={
                      requiredDocTypes.length === 0 || !(selectedApp.required_documents || []).some((doc) => !isDocSubmissionLocked(selectedApp, doc))
                    }
                    className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white rounded-lg text-sm font-medium transition-colors"
                  >
                    Upload Documents
                  </button>
                </div>

                <div className="text-sm text-gray-600 dark:text-gray-400">
                  <p className="mb-1">Required by client:</p>
                  <div className="flex flex-wrap gap-2">
                    {(selectedApp.required_documents && selectedApp.required_documents.length > 0) ? (
                      selectedApp.required_documents.map((docType) => (
                        <span
                          key={`req-badge-${selectedApp.id}-${docType}`}
                          className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 dark:border-slate-700 px-2 py-1 bg-white dark:bg-slate-800"
                        >
                          <span>{documentLabel(docType)}</span>
                          {documentBadge(isDocSubmissionLocked(selectedApp, docType))}
                        </span>
                      ))
                    ) : (
                      <span>None</span>
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {displayDocTypes.map((docType) => {
                    const uploaded = selectedApp.documents_uploaded?.[docType]
                    const requested = requiredDocTypes.includes(docType)
                    const locked = requested ? isDocSubmissionLocked(selectedApp, docType) : true
                    const statusSubmitted = requested ? locked : Boolean(uploaded)
                    return (
                      <div key={docType} className="rounded-lg border border-gray-200 dark:border-slate-700 px-3 py-2">
                        <p className="text-xs font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-2">
                          <span>{documentLabel(docType)}</span>
                          {documentBadge(statusSubmitted)}
                        </p>
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                          {uploaded ? `Uploaded: ${new Date(uploaded.uploaded_at).toLocaleString()}` : 'Not uploaded'}
                        </p>
                        <p className="text-[11px] text-gray-400 mt-1">
                          {!requested
                            ? 'Previously submitted'
                            : locked
                              ? 'Upload locked until client requests again'
                              : 'Pending upload'}
                        </p>
                      </div>
                    )
                  })}
                </div>
                    </>
                  )
                })()}
              </div>
            </div>

            <div className="p-6 border-t border-gray-100 dark:border-slate-700">
              <button
                onClick={() => setSelectedApp(null)}
                className="w-full py-3 bg-gray-100 dark:bg-slate-700 hover:bg-gray-200 dark:hover:bg-slate-600 text-gray-700 dark:text-gray-300 font-medium rounded-lg transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Upload Documents Modal */}
      {selectedApp && uploadModalOpen && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60] p-4">
          <div className="bg-white dark:bg-slate-800 rounded-2xl max-w-lg w-full shadow-xl border border-gray-200 dark:border-slate-700">
            <div className="p-5 border-b border-gray-100 dark:border-slate-700">
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="text-lg font-bold text-gray-900 dark:text-white">Upload Requested Document</h3>
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{selectedApp.job_title}</p>
                </div>
                <button
                  onClick={closeUploadModal}
                  className="p-2 hover:bg-gray-100 dark:hover:bg-slate-700 rounded-lg"
                >
                  <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            <div className="p-5 space-y-4">
              <div>
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Select document type</p>
                <div className="space-y-2">
                  {(selectedApp.required_documents || []).map((docType) => {
                    const locked = isDocSubmissionLocked(selectedApp, docType)
                    return (
                      <label key={docType} className="flex items-center justify-between rounded-lg border border-gray-200 dark:border-slate-700 px-3 py-2">
                        <span className="text-sm text-gray-700 dark:text-gray-300 inline-flex items-center gap-2">
                          <span>{documentLabel(docType)}</span>
                          {documentBadge(locked)}
                        </span>
                        <input
                          type="radio"
                          name="docType"
                          checked={uploadDocType === docType}
                          onChange={() => {
                            setUploadDocType(docType)
                            setPendingUploadFile(null)
                          }}
                          disabled={locked}
                        />
                      </label>
                    )
                  })}
                  {(selectedApp.required_documents || []).length === 0 && (
                    <p className="text-sm text-gray-500 dark:text-gray-400">No documents requested by client.</p>
                  )}
                </div>
              </div>

              <div>
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">Choose file</p>
                <button
                  onClick={() => uploadDocType && handlePickDocument(uploadDocType)}
                  disabled={!uploadDocType}
                  className="px-3 py-2 rounded-lg bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 disabled:opacity-50 text-sm font-medium text-gray-700 dark:text-gray-200"
                >
                  {uploadDocType ? `Select ${documentLabel(uploadDocType)} file` : 'Select document type first'}
                </button>
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  {pendingUploadFile ? `Selected: ${pendingUploadFile.name}` : 'No file selected'}
                </p>
              </div>

              {(selectedApp.required_documents || []).map((docType) => (
                <input
                  key={`upload-modal-${selectedApp.id}-${docType}`}
                  id={`upload-modal-${selectedApp.id}-${docType}`}
                  type="file"
                  className="hidden"
                  accept={docType === 'resume' ? '.pdf,.doc,.docx' : '.pdf'}
                  onChange={(e) => {
                    const f = e.target.files?.[0] || null
                    setUploadDocType(docType)
                    setPendingUploadFile(f)
                    e.currentTarget.value = ''
                  }}
                />
              ))}
            </div>

            <div className="p-5 border-t border-gray-100 dark:border-slate-700 flex items-center gap-3">
              <button
                onClick={closeUploadModal}
                className="flex-1 py-2.5 rounded-lg bg-gray-100 dark:bg-slate-700 hover:bg-gray-200 dark:hover:bg-slate-600 text-gray-700 dark:text-gray-200 font-medium"
              >
                Cancel
              </button>
              <button
                onClick={() => void handleUploadDocument()}
                disabled={!uploadDocType || !pendingUploadFile || !!uploadingDocKey}
                className="flex-1 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white font-medium"
              >
                {uploadingDocKey ? 'Submitting...' : 'Submit'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
