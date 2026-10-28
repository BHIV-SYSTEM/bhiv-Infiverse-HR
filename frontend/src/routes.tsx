import { Routes, Route, Navigate } from 'react-router-dom'

// Recruiter Pages
import RecruiterDashboard from './pages/recruiter/Dashboard'
import JobCreation from './pages/recruiter/JobCreation'
import ApplicantsMatching from './pages/recruiter/ApplicantsMatching'
import FeedbackForm from './pages/recruiter/FeedbackForm'
import AutomationPanel from './pages/recruiter/AutomationPanel'

// Candidate Pages
import CandidateProfile from './pages/candidate/Profile'
import AppliedJobs from './pages/candidate/AppliedJobs'
import CandidateInterviews from './pages/candidate/Interviews'
import CandidateTasks from './pages/candidate/Tasks'
import CandidateFeedback from './pages/candidate/Feedback'
import CvBuilder from './pages/candidate/CvBuilder'

// Client Pages
import ClientDashboard from './pages/client/ClientDashboard'
import ClientJobPosting from './pages/client/ClientJobPosting'
import ClientCandidates from './pages/client/ClientCandidates'
import MatchResults from './pages/client/MatchResults'
import ClientReports from './pages/client/ClientReports'
import ShortlistReview from './pages/client/ShortlistReview'

export default function AppRoutes() {
  return (
    <Routes>
      {/* Recruiter Routes */}
      <Route path="/recruiter" element={<RecruiterDashboard />} />
      <Route path="/recruiter/create-job" element={<JobCreation />} />
      <Route path="/recruiter/applicants/:jobId" element={<ApplicantsMatching />} />
      <Route path="/recruiter/feedback/:candidateId" element={<FeedbackForm />} />
      <Route path="/recruiter/automation" element={<AutomationPanel />} />
      <Route path="/recruiter/cv-builder" element={<CvBuilder />} />

      {/* Candidate Routes */}
      <Route path="/candidate" element={<Navigate to="/candidate/profile" replace />} />
      <Route path="/candidate/profile" element={<CandidateProfile />} />
      <Route path="/candidate/applied-jobs" element={<AppliedJobs />} />
      <Route path="/candidate/interviews" element={<CandidateInterviews />} />
      <Route path="/candidate/tasks" element={<CandidateTasks />} />
      <Route path="/candidate/feedback" element={<CandidateFeedback />} />
      <Route path="/candidate/cv-builder" element={<CvBuilder />} />

      {/* Client Routes */}
      <Route path="/client" element={<ClientDashboard />} />
      <Route path="/client/dashboard" element={<ClientDashboard />} />
      <Route path="/client/jobs" element={<ClientJobPosting />} />
      <Route path="/client/candidates" element={<ClientCandidates />} />
      <Route path="/client/matches" element={<MatchResults />} />
      <Route path="/client/reports" element={<ClientReports />} />
      <Route path="/client/shortlist/:jobId" element={<ShortlistReview />} />

      {/* 404 - Redirect to role selection */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
