import { useState, useEffect, useCallback } from 'react'
import toast from 'react-hot-toast'
import { useLocation, Link } from 'react-router-dom'
import api, { updateCandidateProfile, getCandidateProfile } from '../../services/api'
import FormInput from '../../components/FormInput'
import SalaryRangeInput from '../../components/SalaryRangeInput'
import { useAuth } from '../../context/AuthContext'
import { authStorage } from '../../utils/authStorage'

export default function CandidateProfile() {
  const { user } = useAuth()
  const location = useLocation()
  const [loading, setLoading] = useState(false)
  const [profileLoading, setProfileLoading] = useState(true)
  const [isEditing, setIsEditing] = useState(false)
  const [savedProfile, setSavedProfile] = useState<any>(null)
  const [identityRecord, setIdentityRecord] = useState<any>(null)
  const [resumeFileName, setResumeFileName] = useState<string | null>(null)
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    phone: '',
    location: '',
    totalExperience: '',
    skills: '',
    educationLevel: '',
    expectedSalary: '',
    resume: null as File | null,
  })

  const loadProfile = useCallback(async (candidateId: string) => {
    try {
      setProfileLoading(true)
      console.log('Profile: Loading profile for candidate:', candidateId)
      
      // First, try to load from per-tab auth storage as backup
      const cachedProfile = authStorage.getItem('candidate_profile_data')
      if (cachedProfile) {
        try {
          const parsed = JSON.parse(cachedProfile)
          if (parsed && parsed.id === candidateId) {
            console.log('Profile: Using cached profile data')
            setSavedProfile(parsed)
            setFormData({
              name: parsed.name || '',
              email: parsed.email || '',
              phone: parsed.phone || '',
              location: parsed.location || '',
              totalExperience: (parsed.experience_years || parsed.totalExperience)?.toString() || '',
              skills: parsed.technical_skills || (Array.isArray(parsed.skills) ? parsed.skills.join(', ') : (parsed.skills || '')),
              educationLevel: parsed.education_level || parsed.educationLevel || '',
              expectedSalary: parsed.expectedSalary?.toString() || '',
              resume: null,
            })
          }
        } catch (e) {
          console.warn('Failed to parse cached profile:', e)
        }
      }
      
      // Then try to load from backend
      const data = await getCandidateProfile(candidateId)
      console.log('Profile: Received data from backend:', data)
      if (data) {
        // Store in per-tab auth storage as backup
        authStorage.setItem('candidate_profile_data', JSON.stringify({ ...data, id: candidateId }))
        
        setSavedProfile(data)
        // Map backend field names to frontend state
        setFormData({
          name: data.name || '',
          email: data.email || '',
          phone: data.phone || '',
          location: data.location || '',
          totalExperience: (data.experience_years || data.totalExperience)?.toString() || '',
          skills: data.technical_skills || (Array.isArray(data.skills) ? data.skills.join(', ') : (data.skills || '')),
          educationLevel: data.education_level || data.educationLevel || '',
          expectedSalary: data.expectedSalary?.toString() || '',
          resume: null,
        })
      } else {
        // If no data from backend but we have cached data, keep using cached
        if (!cachedProfile) {
          setSavedProfile(null)
        }
      }
    } catch (error) {
      console.error('Failed to load profile:', error)
      // On error, try to use cached profile if available
      const cachedProfile = authStorage.getItem('candidate_profile_data')
      if (cachedProfile) {
        try {
          const parsed = JSON.parse(cachedProfile)
          if (parsed) {
            console.log('Profile: Using cached profile due to error')
            setSavedProfile(parsed)
            setFormData({
              name: parsed.name || '',
              email: parsed.email || '',
              phone: parsed.phone || '',
              location: parsed.location || '',
              totalExperience: (parsed.experience_years || parsed.totalExperience)?.toString() || '',
              skills: parsed.technical_skills || (Array.isArray(parsed.skills) ? parsed.skills.join(', ') : (parsed.skills || '')),
              educationLevel: parsed.education_level || parsed.educationLevel || '',
              expectedSalary: parsed.expectedSalary?.toString() || '',
              resume: null,
            })
          }
        } catch (e) {
          console.warn('Failed to use cached profile:', e)
        }
      }
      // Don't clear savedProfile on error, keep existing data
    } finally {
      setProfileLoading(false)
    }
  }, [])

  useEffect(() => {
    // Get backend candidate ID (integer) for API calls
    const backendCandidateId = authStorage.getItem('backend_candidate_id')
    const candidateId = backendCandidateId || user?.id || authStorage.getItem('candidate_id')
    console.log('Profile: Using candidate ID:', candidateId, '(backend_id:', backendCandidateId, ')')
    if (candidateId) {
      loadProfile(candidateId)
    } else {
      setProfileLoading(false)
    }
  }, [user, location.pathname, loadProfile]) // Reload when route changes or user changes

  // Reload profile when tab becomes visible or window gets focus (user switches back)
  useEffect(() => {
    const reloadProfile = () => {
      const backendCandidateId = authStorage.getItem('backend_candidate_id')
      const candidateId = backendCandidateId || user?.id || authStorage.getItem('candidate_id')
      if (candidateId && !isEditing) {
        console.log('Profile: Reloading profile...')
        loadProfile(candidateId)
      }
    }

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        reloadProfile()
      }
    }

    const handleFocus = () => {
      reloadProfile()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('focus', handleFocus)
    
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      window.removeEventListener('focus', handleFocus)
    }
  }, [user, isEditing, loadProfile])


  // Fetch canonical identity record from Sampada Gateway
  useEffect(() => {
    const fetchIdentity = async () => {
      const storedCandId = authStorage.getItem('candidate_id') || authStorage.getItem('backend_candidate_id') || user?.id
      const email = authStorage.getItem('user_email') || user?.email
      const queryId = (storedCandId && !storedCandId.startsWith('pravesh:')) ? storedCandId : email
      if (queryId) {
        try {
          const res = await api.get(`/api/v1/identity/candidate/${encodeURIComponent(queryId)}`)
          if (res.data?.record) {
            setIdentityRecord(res.data.record)
            if (res.data.record.canonical_candidate_id) {
              authStorage.setItem('candidate_id', res.data.record.canonical_candidate_id)
              authStorage.setItem('backend_candidate_id', res.data.record.canonical_candidate_id)
            }
          }
        } catch (e) {
          console.warn('Identity fetch fallback:', e)
        }
      }
    }
    fetchIdentity()
  }, [user])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
    setFormData({ ...formData, [e.target.name]: e.target.value })
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      if (file.type !== 'application/pdf') {
        toast.error('Please upload a PDF file')
        return
      }
      if (file.size > 5 * 1024 * 1024) {
        toast.error('File size should be less than 5MB')
        return
      }
      setFormData({ ...formData, resume: file })
      setResumeFileName(file.name)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)

    try {
      // Get backend candidate ID (integer) for API calls
      const backendCandidateId = authStorage.getItem('backend_candidate_id')
      const candidateId = backendCandidateId || user?.id || authStorage.getItem('candidate_id') || 'demo-candidate'
      
      console.log('Profile Update: Using candidate ID:', candidateId)
      
      // Map frontend fields to backend API field names
      const profileData = {
        name: formData.name || undefined,
        phone: formData.phone || undefined,
        location: formData.location || undefined,
        technical_skills: formData.skills || undefined,  // Backend expects technical_skills (string)
        experience_years: formData.totalExperience ? parseInt(formData.totalExperience) : undefined,  // Backend expects experience_years (int)
        education_level: formData.educationLevel || undefined,  // Backend expects education_level
        seniority_level: undefined,  // Backend supports this field
      }
      
      // Remove undefined fields
      const cleanedData = Object.fromEntries(
        Object.entries(profileData).filter(([_, v]) => v !== undefined)
      )
      
      console.log('Profile Update: Sending data:', cleanedData)
      
      const response = await updateCandidateProfile(candidateId, cleanedData)
      console.log('Profile Update: Response:', response)
      
      // Check if update was successful
      if (response?.success !== false && !response?.error) {
        toast.success('Profile updated successfully!')
        
        // Update local state immediately
        const updatedProfile = {
          ...savedProfile,
          name: formData.name,
          phone: formData.phone,
          location: formData.location,
          technical_skills: formData.skills,
          experience_years: formData.totalExperience ? parseInt(formData.totalExperience) : 0,
          education_level: formData.educationLevel,
          skills: formData.skills,
          totalExperience: formData.totalExperience ? parseInt(formData.totalExperience) : 0,
          educationLevel: formData.educationLevel,
          resumeFileName: resumeFileName || savedProfile?.resumeFileName,
          id: candidateId,
        }
        
        authStorage.setItem('candidate_profile_data', JSON.stringify(updatedProfile))
        setSavedProfile(updatedProfile)
        
        // Reload profile from backend to ensure we have the latest data
        console.log('Profile: Reloading profile from backend after update...')
        await loadProfile(candidateId)
        
        setIsEditing(false)
      } else {
        // Update failed on backend
        throw new Error(response?.error || 'Profile update failed')
      }
    } catch (error: any) {
      console.error('Error updating candidate profile:', error)
      
      // Provide more specific error messages
      if (error.isNetworkError || error.code === 'ERR_NETWORK' || error.message?.includes('ERR_INTERNET_DISCONNECTED')) {
        toast.error('Cannot connect to server. Please ensure the backend is running at http://localhost:8000')
      } else if (error.response?.status === 422) {
        toast.error('Invalid candidate ID format. Please complete registration first.')
      } else if (error.response?.status === 404) {
        toast.error('Profile not found. Please complete registration first.')
      } else if (error.response?.status === 401) {
        toast.error('Authentication failed. Please log in again.')
      } else if (error.response?.data?.error) {
        toast.error(error.response.data.error)
      } else {
        toast.error(error.message || 'Failed to update profile. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  const formatSalary = (salary: number) => {
    if (!salary) return 'Not specified'
    return `₹${(salary / 100000).toFixed(1)}L per annum`
  }

  if (profileLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500"></div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="p-6 rounded-2xl bg-gradient-to-r from-blue-500/5 to-cyan-500/5 dark:from-blue-500/10 dark:to-cyan-500/10 backdrop-blur-xl border border-blue-300/20 dark:border-blue-500/20">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div>
            <h1 className="page-title">My Profile</h1>
            <p className="page-subtitle">Your professional information</p>
            {/* Canonical Candidate ID Pill */}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <div className="inline-flex items-center gap-2 px-3 py-1.5 bg-blue-500/10 dark:bg-blue-500/20 border border-blue-500/30 rounded-xl">
                <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                <span className="text-xs uppercase tracking-wider text-blue-600 dark:text-blue-300 font-semibold">Candidate ID</span>
                <span className="font-mono text-sm font-bold text-gray-900 dark:text-white">
                  {identityRecord?.canonical_candidate_id || (authStorage.getItem('candidate_id') && !authStorage.getItem('candidate_id')?.startsWith('pravesh:') ? authStorage.getItem('candidate_id') : 'BHIV-2026-MSSU-0001')}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    const cid = identityRecord?.canonical_candidate_id || (authStorage.getItem('candidate_id') && !authStorage.getItem('candidate_id')?.startsWith('pravesh:') ? authStorage.getItem('candidate_id') : 'BHIV-2026-MSSU-0001');
                    navigator.clipboard.writeText(cid);
                    toast.success('Candidate ID copied: ' + cid);
                  }}
                  title="Copy Candidate ID"
                  className="text-gray-400 hover:text-blue-500 transition-colors ml-1 p-0.5"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                  </svg>
                </button>
              </div>
              <span className="px-2.5 py-1 text-xs font-semibold bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 rounded-lg border border-emerald-500/30 flex items-center gap-1.5">
                🏛️ {identityRecord?.institute_code || 'MSSU'}
              </span>
              <span className="px-2.5 py-1 text-xs font-semibold bg-purple-50 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 rounded-lg border border-purple-500/30 flex items-center gap-1.5">
                🎓 {identityRecord?.cohort_id || 'COHORT-2026-OCT-A'}
              </span>
              <span className="px-2.5 py-1 text-xs font-semibold bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300 rounded-lg border border-green-500/30 flex items-center gap-1.5">
                ✓ Verified
              </span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Link
              to="/candidate/cv-builder"
              className="border border-blue-500/40 text-blue-600 dark:text-blue-300 px-5 py-2.5 rounded-xl font-semibold hover:bg-blue-50 dark:hover:bg-blue-500/10 transition-all duration-300 flex items-center gap-2"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m-6-8h6M9 4h6a2 2 0 012 2v12a2 2 0 01-2 2H9a2 2 0 01-2-2V6a2 2 0 012-2z" />
              </svg>
              Build My CV
            </Link>
          {!isEditing && (
            <button
              onClick={() => setIsEditing(true)}
              className="bg-gradient-to-r from-blue-500 to-cyan-600 hover:from-blue-600 hover:to-cyan-700 text-white px-6 py-2.5 rounded-xl font-semibold transition-all duration-300 flex items-center gap-2 shadow-lg shadow-blue-500/20"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
              Edit Profile
            </button>
          )}
          </div>
        </div>
      </div>

      {/* Profile Info Section */}
      {!isEditing && (
        <div className="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow-sm border border-gray-100 dark:border-slate-700">
          <h2 className="section-title flex items-center gap-2">
            <svg className="w-6 h-6 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
            Profile Information
          </h2>
          
          {(savedProfile || formData.name) ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Personal Info */}
              <div className="space-y-4">
                <div className="flex items-center gap-4 p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="w-16 h-16 bg-gradient-to-br from-blue-500 to-cyan-500 rounded-full flex items-center justify-center text-white text-2xl font-bold shadow-lg">
                    {(savedProfile?.name || formData.name)?.charAt(0)?.toUpperCase() || 'C'}
                  </div>
                  <div>
                    <p className="text-lg font-semibold text-gray-900 dark:text-white">
                      {savedProfile?.name || formData.name || 'Not specified'}
                    </p>
                    <p className="text-gray-500 dark:text-gray-400">
                      {savedProfile?.email || formData.email || 'No email'}
                    </p>
                  </div>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Phone</p>
                  </div>
                  <p className="text-gray-900 dark:text-white font-medium text-lg">
                    {savedProfile?.phone || formData.phone || 'Not specified'}
                  </p>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Location</p>
                  </div>
                  <p className="text-gray-900 dark:text-white font-medium text-lg">
                    {savedProfile?.location || formData.location || 'Not specified'}
                  </p>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 14l9-5-9-5-9 5 9 5z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 14l6.16-3.422a12.083 12.083 0 01.665 6.479A11.952 11.952 0 0012 20.055a11.952 11.952 0 00-6.824-2.998 12.078 12.078 0 01.665-6.479L12 14z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Education Level</p>
                  </div>
                  <p className="text-gray-900 dark:text-white font-medium text-lg">
                    {savedProfile?.educationLevel || savedProfile?.education_level || formData.educationLevel || 'Not specified'}
                  </p>
                </div>
              </div>

              {/* Professional Info */}
              <div className="space-y-4">
                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Total Experience</p>
                  </div>
                  <p className="text-gray-900 dark:text-white font-medium text-lg">
                    {(savedProfile?.experience_years || savedProfile?.totalExperience || savedProfile?.total_experience || formData.totalExperience) 
                      ? `${savedProfile?.experience_years || savedProfile?.totalExperience || savedProfile?.total_experience || formData.totalExperience} years` 
                      : 'Not specified'}
                  </p>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Expected Salary</p>
                  </div>
                  <p className="text-gray-900 dark:text-white font-medium text-lg">
                    {formatSalary(savedProfile?.expectedSalary || savedProfile?.expected_salary || Number(formData.expectedSalary))}
                  </p>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-2">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Skills</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {(() => {
                      const skills = savedProfile?.technical_skills || savedProfile?.skills || formData.skills
                      const skillArray = Array.isArray(skills) ? skills : (skills?.split(',') || [])
                      return skillArray.length > 0 ? skillArray.map((skill: string, index: number) => (
                        <span
                          key={index}
                          className="px-3 py-1.5 bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded-full text-sm font-medium"
                        >
                          {skill.trim()}
                        </span>
                      )) : <span className="text-gray-500">No skills specified</span>
                    })()}
                  </div>
                </div>

                <div className="p-4 bg-gray-50 dark:bg-slate-700/50 rounded-xl">
                  <div className="flex items-center gap-2 mb-1">
                    <svg className="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Resume</p>
                  </div>
                  <div className="text-gray-900 dark:text-white font-medium">
                    {(savedProfile?.resume_url || savedProfile?.resume) ? (
                      <a href={savedProfile.resume_url || savedProfile.resume} target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:underline flex items-center gap-2">
                        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                        View Resume
                      </a>
                    ) : (savedProfile?.resumeFileName || resumeFileName) ? (
                      <div className="flex items-center gap-3 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800">
                        <div className="w-10 h-10 bg-red-500 rounded-lg flex items-center justify-center">
                          <svg className="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 24 24">
                            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20M10.92,12.31C10.68,11.54 10.15,9.08 11.55,9.04C12.95,9 12.03,12.16 12.03,12.16C12.42,13.65 14.05,14.72 14.05,14.72C14.55,14.57 17.4,14.24 17,15.72C16.57,17.2 13.5,15.81 13.5,15.81C11.55,15.95 10.09,16.47 10.09,16.47C8.96,18.58 7.64,19.5 7.1,18.61C6.43,17.5 9.23,16.07 9.23,16.07C10.68,13.72 10.9,12.35 10.92,12.31Z" />
                          </svg>
                        </div>
                        <div className="flex-1">
                          <p className="font-medium text-gray-900 dark:text-white text-sm">
                            {savedProfile?.resumeFileName || resumeFileName}
                          </p>
                          <p className="text-xs text-green-600 dark:text-green-400 flex items-center gap-1">
                            <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                            Uploaded successfully
                          </p>
                        </div>
                      </div>
                    ) : (
                      <span className="text-gray-500">No resume uploaded</span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-center py-12">
              <svg className="w-16 h-16 mx-auto text-gray-400 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              <p className="text-gray-500 dark:text-gray-400 mb-4">No profile information saved yet</p>
              <button
                onClick={() => setIsEditing(true)}
                className="bg-gradient-to-r from-blue-500 to-cyan-600 hover:from-blue-600 hover:to-cyan-700 text-white px-6 py-2.5 rounded-xl font-semibold transition-all duration-300"
              >
                Create Profile
              </button>
            </div>
          )}
        </div>
      )}

      {/* Edit Form Section */}
      {isEditing && (
        <form onSubmit={handleSubmit} className="bg-white dark:bg-slate-800 rounded-2xl p-6 shadow-sm border border-gray-100 dark:border-slate-700">
          <h2 className="section-title flex items-center gap-2">
            <svg className="w-6 h-6 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
            Update Profile
          </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <FormInput
            label="Full Name"
            name="name"
            value={formData.name}
            onChange={handleChange}
            placeholder="John Doe"
            required
          />

          <FormInput
            label="Email"
            name="email"
            type="email"
            value={formData.email}
            onChange={handleChange}
            placeholder="john.doe@email.com"
            required
          />

          <FormInput
            label="Phone"
            name="phone"
            type="tel"
            value={formData.phone}
            onChange={handleChange}
            placeholder="+91 9876543210"
            required
          />

          <FormInput
            label="Location"
            name="location"
            value={formData.location}
            onChange={handleChange}
            placeholder="Bangalore, India"
            required
          />

          <FormInput
            label="Total Experience (years)"
            name="totalExperience"
            type="number"
            value={formData.totalExperience}
            onChange={handleChange}
            placeholder="4.5"
            required
          />

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Expected Salary (₹ per annum) *
            </label>
            <SalaryRangeInput
              name="expectedSalary"
              value={formData.expectedSalary}
              onChange={(value) => setFormData(prev => ({ ...prev, expectedSalary: value }))}
              className="input-field"
              placeholder="e.g., 800000 - 1200000"
              required={true}
            />
          </div>

          <FormInput
            label="Education Level"
            name="educationLevel"
            value={formData.educationLevel}
            onChange={handleChange}
            required
            options={[
              { value: 'High School', label: 'High School' },
              { value: 'Bachelor', label: 'Bachelor' },
              { value: 'Master', label: 'Master' },
              { value: 'PhD', label: 'PhD' },
            ]}
          />

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Upload Resume (PDF only)
            </label>
            <input
              type="file"
              accept=".pdf"
              onChange={handleFileChange}
              className="w-full px-4 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg text-gray-900 dark:text-gray-100 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-blue-600 file:text-white hover:file:bg-blue-700 shadow-sm"
            />
            <p className="text-gray-500 text-xs mt-1">Max size: 5MB</p>
          </div>
        </div>

        <div className="mt-6">
          <FormInput
            label="Skills"
            name="skills"
            value={formData.skills}
            onChange={handleChange}
            placeholder="React, TypeScript, Node.js, Python (comma separated)"
            required
          />
        </div>

        <div className="mt-8 flex space-x-4">
          <button
            type="submit"
            disabled={loading}
            className="bg-gradient-to-r from-blue-500 to-cyan-600 hover:from-blue-600 hover:to-cyan-700 text-white px-6 py-2.5 rounded-xl font-semibold transition-all duration-300 flex items-center gap-2 shadow-lg shadow-blue-500/20"
          >
            {loading ? (
              <>
                <svg className="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Saving...
              </>
            ) : (
              <>
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                Save Profile
              </>
            )}
          </button>
          <button
            type="button"
            onClick={() => setIsEditing(false)}
            className="px-6 py-2.5 rounded-xl font-semibold border border-gray-300 dark:border-slate-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-slate-700 transition-all duration-300"
          >
            Cancel
          </button>
        </div>
        </form>
      )}
    </div>
  )
}
