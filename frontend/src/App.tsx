import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import SplashScreen from './components/SplashScreen'
import AuthPage from './pages/auth/AuthPage'
import { ThemeProvider } from './context/ThemeContext'
import { AuthProvider, useAuth } from './context/AuthContext'
import ProtectedRoute, { PublicRoute } from './components/ProtectedRoute'

// Layouts
import CandidateLayout from './components/layouts/CandidateLayout'
import RecruiterLayout from './components/layouts/RecruiterLayout'
import ClientLayout from './components/layouts/ClientLayout'

// Candidate Pages
import CandidateDashboard from './pages/candidate/Dashboard'
import CandidateProfile from './pages/candidate/Profile'
import JobSearch from './pages/candidate/JobSearch'
import AppliedJobs from './pages/candidate/AppliedJobs'
import CandidateInterviews from './pages/candidate/Interviews'
import CandidateTasks from './pages/candidate/Tasks'
import CandidateTaskDetail from './pages/candidate/TaskDetail'
import CandidateFeedback from './pages/candidate/Feedback'
import CvBuilder from './pages/candidate/CvBuilder'

// Recruiter Pages
import RecruiterDashboard from './pages/recruiter/Dashboard'
import JobCreation from './pages/recruiter/JobCreation'
import ApplicantsMatching from './pages/recruiter/ApplicantsMatching'
import FeedbackForm from './pages/recruiter/FeedbackForm'
import AutomationPanel from './pages/recruiter/AutomationPanel'
import CandidateSearch from './pages/recruiter/CandidateSearch'
import BatchUpload from './pages/recruiter/BatchUpload'
import BatchOperations from './pages/recruiter/BatchOperations'
import InterviewScheduling from './pages/recruiter/InterviewScheduling'
import ExportReports from './pages/recruiter/ExportReports'
import ClientJobsMonitor from './pages/recruiter/ClientJobsMonitor'

import ValuesAssessment from './pages/recruiter/ValuesAssessment'

// Client Pages
import ClientDashboard from './pages/client/ClientDashboard'
import ClientJobPosting from './pages/client/ClientJobPosting'
import ClientCandidates from './pages/client/ClientCandidates'
import MatchResults from './pages/client/MatchResults'
import ClientReports from './pages/client/ClientReports'
import LiveRecruiterMonitoring from './pages/client/LiveRecruiterMonitoring'
import ControlCenter from './pages/control/ControlCenter'

function HomeRedirect() {
  const { user, userRole, loading } = useAuth()

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  const roleRedirects: Record<string, string> = {
    candidate: '/candidate/dashboard',
    recruiter: '/recruiter',
    client: '/client',
    admin: '/control',
  }

  if (user) {
    const role = userRole || user.role || 'candidate'
    return <Navigate to={roleRedirects[role] || '/candidate/dashboard'} replace />
  }
  return <Navigate to="/auth" replace />
}

function App() {
  const [showSplash, setShowSplash] = useState(true)

  useEffect(() => {
    // Check if splash has been shown in this session
    const splashShown = sessionStorage.getItem('splashShown')
    if (splashShown) {
      setShowSplash(false)
    }
  }, [])

  const handleSplashComplete = () => {
    sessionStorage.setItem('splashShown', 'true')
    setShowSplash(false)
  }

  return (
    <AuthProvider>
      <ThemeProvider>
        {showSplash ? (
          <SplashScreen onComplete={handleSplashComplete} />
        ) : (
          <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Routes>
              {/* Public Routes */}
              <Route path="/" element={<HomeRedirect />} />
              <Route path="/auth" element={<PublicRoute><AuthPage /></PublicRoute>} />
              <Route
                path="/control"
                element={
                  <ProtectedRoute allowedRoles={['client', 'recruiter', 'admin']}>
                    <ControlCenter />
                  </ProtectedRoute>
                }
              />

              {/* Candidate Routes - Protected for 'candidate' role only */}
              <Route path="/candidate" element={
                <ProtectedRoute allowedRoles={['candidate']}>
                  <CandidateLayout />
                </ProtectedRoute>
              }>
                <Route index element={<Navigate to="/candidate/dashboard" replace />} />
                <Route path="dashboard" element={<CandidateDashboard />} />
                <Route path="profile" element={<CandidateProfile />} />
                <Route path="jobs" element={<JobSearch />} />
                <Route path="applied-jobs" element={<AppliedJobs />} />
                <Route path="interviews" element={<CandidateInterviews />} />
                <Route path="tasks" element={<CandidateTasks />} />
                <Route path="tasks/:taskId" element={<CandidateTaskDetail />} />
                <Route path="feedback" element={<CandidateFeedback />} />
                <Route path="cv-builder" element={<CvBuilder />} />
              </Route>

              {/* Recruiter Routes - Protected for 'recruiter' role only */}
              <Route path="/recruiter" element={
                <ProtectedRoute allowedRoles={['recruiter']}>
                  <RecruiterLayout />
                </ProtectedRoute>
              }>
                <Route index element={<RecruiterDashboard />} />
                <Route path="create-job" element={<JobCreation />} />
                <Route path="upload-candidates" element={<BatchUpload />} />
                <Route path="jobs" element={<RecruiterDashboard />} />
                <Route path="candidates" element={<Navigate to="/recruiter/candidate-search" replace />} />
                <Route path="candidate-search" element={<CandidateSearch />} />
                <Route path="screening" element={<ApplicantsMatching />} />
                <Route path="applicants/:jobId" element={<ApplicantsMatching />} />
                <Route path="schedule-interview" element={<InterviewScheduling />} />
                <Route path="values-assessment" element={<ValuesAssessment />} />
                <Route path="feedback/:candidateId" element={<FeedbackForm />} />
                <Route path="export-reports" element={<ExportReports />} />
                <Route path="client-jobs" element={<ClientJobsMonitor />} />

                <Route path="batch-operations" element={<BatchOperations />} />
                <Route path="automation" element={<AutomationPanel />} />
                <Route path="reports" element={<ExportReports />} />
                <Route path="cv-builder" element={<CvBuilder />} />
              </Route>

              {/* Client Routes - Protected for 'client' role only */}
              <Route path="/client" element={
                <ProtectedRoute allowedRoles={['client']}>
                  <ClientLayout />
                </ProtectedRoute>
              }>
                <Route index element={<ClientDashboard />} />
                <Route path="dashboard" element={<ClientDashboard />} />
                <Route path="jobs" element={<ClientJobPosting />} />
                <Route path="candidates" element={<ClientCandidates />} />
                <Route path="matches" element={<MatchResults />} />
                <Route path="live-monitoring" element={<LiveRecruiterMonitoring />} />
                <Route path="reports" element={<ClientReports />} />
              </Route>

              {/* Fallback - re-evaluate via HomeRedirect */}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
            <Toaster
              position="top-right"
              toastOptions={{
                duration: 3000,
                className: 'dark:bg-gray-900 dark:text-gray-100',
                style: {
                  background: 'var(--toast-bg, #ffffff)',
                  color: 'var(--toast-color, #1f2937)',
                  border: '2px solid var(--toast-border, #e5e7eb)',
                  borderRadius: '12px',
                  padding: '16px',
                  fontWeight: '600',
                },
                success: {
                  iconTheme: {
                    primary: '#10b981',
                    secondary: '#ffffff',
                  },
                },
                error: {
                  iconTheme: {
                    primary: '#ef4444',
                    secondary: '#ffffff',
                  },
                },
              }}
            />
          </BrowserRouter>
        )}
      </ThemeProvider>
    </AuthProvider>
  )
}

export default App
