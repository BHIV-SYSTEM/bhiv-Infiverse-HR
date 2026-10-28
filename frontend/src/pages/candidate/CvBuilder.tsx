import { useCallback, useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { Link } from 'react-router-dom'
import api from '../../services/api'
import { authStorage } from '../../utils/authStorage'

/**
 * CV Builder — one content model, three editable templates.
 *
 * AI boundaries enforced by this page + the gateway:
 * - Prefilled fields always show their provenance badge (profile / needs input).
 * - Missing information is asked for, never invented; blanks export as blanks.
 * - "Improve" only proposes whitespace/capitalisation fixes and must be
 *   reviewed and accepted by the user before anything is applied.
 * - Export requires an explicit approval step (recorded server-side).
 * - Drafts are strictly the signed-in user's own: recruiters only ever edit
 *   their own CV here, never a candidate's private CV.
 */

type PrefillField = { value: unknown; source: string; verified: boolean }
type Entry = { id: string; title?: string | null; subtext?: string | null; text?: string | null; url?: string | null }
type Section = { id: string; type: string; heading: string; visible: boolean; entries: Entry[] }
type Content = { schema_version: string; sections: Section[] }
type Question = { field: string; question: string; required: boolean; kind: string }
type Proposal = {
  proposal_id: string
  field_ref: string
  original: string
  proposed: string
  reason: string
}

const TEMPLATES = [
  { id: 'professional', label: 'Professional', desc: 'Clean, conventional, suitable for general employment' },
  { id: 'modern', label: 'Modern', desc: 'Contemporary layout with flexible sections' },
  { id: 'minimal', label: 'Minimal', desc: 'Simple, compact, readable and easy to print' },
] as const

const APPROVAL_PHRASE = 'I have reviewed this CV'

const uid = () => Math.random().toString(36).slice(2, 12)

const defaultContent = (): Content => ({
  schema_version: 'cv-1.0',
  sections: [
    { id: 'header', type: 'header', heading: 'Header', visible: true, entries: [{ id: uid(), text: '', subtext: '' }] },
    { id: 'summary', type: 'summary', heading: 'Professional Summary', visible: true, entries: [] },
    { id: 'experience', type: 'experience', heading: 'Work Experience', visible: true, entries: [] },
    { id: 'education', type: 'education', heading: 'Education', visible: true, entries: [] },
    { id: 'skills', type: 'skills', heading: 'Skills', visible: true, entries: [] },
    { id: 'projects', type: 'projects', heading: 'Projects', visible: true, entries: [] },
    { id: 'certifications', type: 'certifications', heading: 'Certifications', visible: true, entries: [] },
    { id: 'languages', type: 'languages', heading: 'Languages', visible: true, entries: [] },
    { id: 'awards', type: 'awards', heading: 'Awards & Honours', visible: true, entries: [] },
    { id: 'references', type: 'references', heading: 'References', visible: true, entries: [] },
  ],
})

const authHeaders = () => {
  const token = authStorage.getItem('auth_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

const sourceBadge = (f?: PrefillField) => {
  if (!f) return null
  if (f.source === 'profile')
    return (
      <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
        from profile
      </span>
    )
  if (f.source === 'auth')
    return (
      <span className="ml-2 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700">
        from sign-in
      </span>
    )
  return (
    <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
      needs your input
    </span>
  )
}

export default function CvBuilder() {
  const isRecruiter = authStorage.getItem('user_role') === 'recruiter'
  const candidateId =
    authStorage.getItem('candidate_id') || authStorage.getItem('backend_candidate_id') || undefined

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [prefill, setPrefill] = useState<Record<string, PrefillField> | null>(null)
  const [questions, setQuestions] = useState<Question[]>([])
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [content, setContent] = useState<Content>(defaultContent)
  const [template, setTemplate] = useState<string>('professional')
  const [draftId, setDraftId] = useState<string | null>(null)
  const [approved, setApproved] = useState(false)
  const [showApprove, setShowApprove] = useState(false)
  const [previewHtml, setPreviewHtml] = useState<string | null>(null)
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [busyProposal, setBusyProposal] = useState<string | null>(null)

  // ---------------------------------------------------------------------------
  // Load: authorised prefill + any existing draft
  // ---------------------------------------------------------------------------
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = candidateId ? { candidate_id: candidateId } : {}
      const [prefillRes, draftsRes] = await Promise.all([
        api.get('/api/v1/cv/prefill', { params, headers: authHeaders() }),
        api.get('/api/v1/cv/drafts', { headers: authHeaders() }),
      ])
      setPrefill(prefillRes.data.prefill)
      setQuestions(prefillRes.data.questions || [])

      const drafts = draftsRes.data.drafts || []
      if (drafts.length > 0) {
        const latest = drafts[drafts.length - 1]
        const detail = await api.get(`/api/v1/cv/drafts/${latest.draft_id}`, { headers: authHeaders() })
        setDraftId(detail.data.draft.draft_id)
        setContent(detail.data.draft.content as Content)
        setTemplate(detail.data.draft.template || 'professional')
        setApproved(Boolean(detail.data.draft.approved))
      } else if (prefillRes.data.prefill) {
        // Prefill ONLY verified fields — blanks stay blank, never invented.
        const p = prefillRes.data.prefill as Record<string, PrefillField>
        const next = defaultContent()
        const header = next.sections[0]
        header.entries[0].text = String(p.full_name?.value || '')
        header.entries[0].subtext = [p.email?.value, p.phone?.value, p.location?.value]
          .filter(Boolean)
          .join(' · ')
        if (p.skills?.value && Array.isArray(p.skills.value) && p.skills.value.length) {
          const skillsSection = next.sections.find((s) => s.id === 'skills')!
          skillsSection.entries = [{ id: uid(), text: (p.skills.value as string[]).join(', ') }]
        }
        if (p.education_level?.value) {
          const edu = next.sections.find((s) => s.id === 'education')!
          edu.entries = [{ id: uid(), text: String(p.education_level.value) }]
        }
        if (typeof p.experience_years?.value === 'number') {
          const exp = next.sections.find((s) => s.id === 'experience')!
          exp.entries = [{ id: uid(), text: `${p.experience_years.value} years of experience` }]
        }
        setContent(next)
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Could not load the CV builder')
    } finally {
      setLoading(false)
    }
  }, [candidateId])

  useEffect(() => {
    load()
  }, [load])

  // ---------------------------------------------------------------------------
  // Content editing helpers (no AI involved — plain user edits)
  // ---------------------------------------------------------------------------
  const updateSection = (sectionId: string, patch: Partial<Section>) =>
    setContent((c) => ({
      ...c,
      sections: c.sections.map((s) => (s.id === sectionId ? { ...s, ...patch } : s)),
    }))

  const updateEntry = (sectionId: string, entryId: string, patch: Partial<Entry>) =>
    setContent((c) => ({
      ...c,
      sections: c.sections.map((s) =>
        s.id === sectionId
          ? { ...s, entries: s.entries.map((e) => (e.id === entryId ? { ...e, ...patch } : e)) }
          : s,
      ),
    }))

  const addEntry = (sectionId: string) =>
    setContent((c) => ({
      ...c,
      sections: c.sections.map((s) =>
        s.id === sectionId ? { ...s, entries: [...s.entries, { id: uid(), text: '' }] } : s,
      ),
    }))

  const removeEntry = (sectionId: string, entryId: string) =>
    setContent((c) => ({
      ...c,
      sections: c.sections.map((s) =>
        s.id === sectionId ? { ...s, entries: s.entries.filter((e) => e.id !== entryId) } : s,
      ),
    }))

  const moveSection = (index: number, dir: -1 | 1) =>
    setContent((c) => {
      const sections = [...c.sections]
      const target = index + dir
      if (index <= 0 || target <= 0 || target >= sections.length) return c
      ;[sections[index], sections[target]] = [sections[target], sections[index]]
      return { ...c, sections }
    })

  const removeSection = (sectionId: string) =>
    setContent((c) => ({ ...c, sections: c.sections.filter((s) => s.id !== sectionId || s.type === 'header') }))

  const addSection = () =>
    setContent((c) => ({
      ...c,
      sections: [
        ...c.sections,
        { id: uid(), type: 'custom', heading: 'New section', visible: true, entries: [{ id: uid(), text: '' }] },
      ],
    }))

  // ---------------------------------------------------------------------------
  // Answers to missing-information questions -> user-provided content
  // ---------------------------------------------------------------------------
  const applyAnswer = (field: string) => {
    const text = (answers[field] || '').trim()
    if (!text) {
      toast.error('Add your own text first — nothing is filled in for you')
      return
    }
    const sectionId =
      field === 'professional_summary' ? 'summary'
      : field === 'education' ? 'education'
      : field
    const exists = content.sections.some((s) => s.id === sectionId)
    if (!exists) {
      setContent((c) => ({
        ...c,
        sections: [
          ...c.sections,
          { id: sectionId, type: sectionId, heading: field, visible: true, entries: [{ id: uid(), text }] },
        ],
      }))
    } else {
      setContent((c) => ({
        ...c,
        sections: c.sections.map((s) =>
          s.id === sectionId ? { ...s, entries: [...s.entries, { id: uid(), text }] } : s,
        ),
      }))
    }
    setAnswers((a) => ({ ...a, [field]: '' }))
    setQuestions((qs) => qs.filter((q) => q.field !== field))
    toast.success('Added — this is your own wording')
  }

  // ---------------------------------------------------------------------------
  // Save / load drafts
  // ---------------------------------------------------------------------------
  const save = async (approve = false) => {
    setSaving(true)
    try {
      const payload: Record<string, unknown> = { template, content }
      if (approve) payload.approve = true
      if (!draftId) {
        const res = await api.post('/api/v1/cv/drafts', payload, { headers: authHeaders() })
        setDraftId(res.data.draft.draft_id)
        setApproved(Boolean(res.data.draft.approved))
        toast.success('Draft created')
      } else {
        const res = await api.put(`/api/v1/cv/drafts/${draftId}`, payload, { headers: authHeaders() })
        setApproved(Boolean(res.data.draft.approved))
        toast.success(approve ? 'Saved and approved' : 'Draft saved')
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Save failed — nothing was lost, try again')
    } finally {
      setSaving(false)
    }
  }

  const approveCv = async () => {
    try {
      if (!draftId) await save(false)
      const id = draftId
      if (!id) return
      await api.post(
        `/api/v1/cv/drafts/${id}/approve`,
        { confirmation: APPROVAL_PHRASE },
        { headers: authHeaders() },
      )
      setApproved(true)
      setShowApprove(false)
      toast.success('CV approved for export')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Approval failed')
    }
  }

  const exportCv = async () => {
    if (!draftId) {
      toast.error('Save the draft first')
      return
    }
    if (!approved) {
      setShowApprove(true)
      toast('Review and approve your CV before exporting', { icon: '🔒' })
      return
    }
    try {
      const res = await api.post('/api/v1/cv/export', { draft_id: draftId }, { headers: authHeaders() })
      const blob = new Blob([res.data.html], { type: 'text/html' })
      const url = URL.createObjectURL(blob)
      const win = window.open(url, '_blank')
      if (!win) {
        const a = document.createElement('a')
        a.href = url
        a.download = res.data.filename
        a.click()
      }
      toast.success('Export ready — print to PDF with Ctrl/Cmd+P')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Export failed')
    }
  }

  const buildPreview = async () => {
    try {
      const res = await api.post(
        '/api/v1/cv/assemble',
        { content, template },
        { headers: authHeaders() },
      )
      setPreviewHtml(res.data.preview_html)
      const warnings = res.data.validation?.warnings || []
      if (warnings.length) toast(warnings.map((w: any) => w.message).join('\n'), { icon: '⚠️', duration: 6000 })
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Preview failed')
    }
  }

  // ---------------------------------------------------------------------------
  // AI-assisted suggestions (review before applying)
  // ---------------------------------------------------------------------------
  const requestSuggestions = async (sectionId: string, entry: Entry) => {
    if (!entry.text || !entry.text.trim()) {
      toast.error('There is no text to check yet')
      return
    }
    setBusyProposal(entry.id)
    try {
      const res = await api.post(
        '/api/v1/cv/suggest',
        { text: entry.text, field_ref: `${sectionId}:${entry.id}` },
        { headers: authHeaders() },
      )
      const found: Proposal[] = res.data.proposals || []
      if (!found.length) {
        toast.success('No changes needed — wording untouched')
      } else {
        setProposals(found)
      }
    } catch {
      toast.error('Suggestion service unavailable')
    } finally {
      setBusyProposal(null)
    }
  }

  const applyProposal = (p: Proposal, accept: boolean) => {
    if (accept) {
      const [sectionId, entryId] = p.field_ref.split(':')
      updateEntry(sectionId, entryId, { text: p.proposed })
      toast.success('Accepted — review before saving')
    }
    setProposals((list) => list.filter((x) => x.proposal_id !== p.proposal_id))
  }

  const summarySectionVisible = content.sections.some((s) => s.type === 'summary' && s.visible)

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center text-gray-500">Loading your CV builder…</div>
    )
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">CV Builder</h1>
          <p className="text-sm text-gray-500">
            {isRecruiter
              ? 'Building your own CV — recruiters can only ever edit their own CV here.'
              : 'Prefilled from your authorised profile. You write the rest; nothing is invented.'}
          </p>
        </div>
        <div className="flex gap-2">
          <Link
            to={isRecruiter ? '/recruiter' : '/candidate/profile'}
            className="rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-600 hover:bg-gray-50"
          >
            Back
          </Link>
          <button
            onClick={() => save(false)}
            disabled={saving}
            className="rounded-lg bg-gray-800 px-4 py-2 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save draft'}
          </button>
          <button
            onClick={buildPreview}
            className="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
          >
            Preview
          </button>
          <button
            onClick={exportCv}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700"
          >
            {approved ? 'Export' : 'Approve & Export'}
          </button>
        </div>
      </div>

      {draftId && (
        <div className="mb-4 rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500 dark:bg-gray-800">
          Draft <span className="font-mono">{draftId}</span> ·{' '}
          {approved ? (
            <span className="font-medium text-emerald-600">approved {''}— ready to export</span>
          ) : (
            <span className="font-medium text-amber-600">not approved yet — export stays locked</span>
          )}
        </div>
      )}

      {summarySectionVisible &&
        !content.sections
          .find((s) => s.type === 'summary')
          ?.entries.some((e) => (e.text || '').trim()) && (
          <div className="mb-4 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
            Professional summary is empty — it will export blank rather than be invented. Use the
            question panel to write it in your own words.
          </div>
        )}

      <div className="grid gap-6 lg:grid-cols-[320px,1fr]">
        {/* ---------------- Left: prefill provenance + questions + templates --- */}
        <div className="space-y-4">
          <section className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
            <h2 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-200">
              1. Your information
            </h2>
            <ul className="space-y-1.5 text-sm">
              {prefill &&
                Object.entries(prefill).map(([key, f]) => (
                  <li key={key} className="flex items-center justify-between gap-2">
                    <span className="capitalize text-gray-600 dark:text-gray-300">
                      {key.replace(/_/g, ' ')}
                    </span>
                    <span className="text-right text-xs">
                      {f.source === 'none' || f.value === null || f.value === undefined ? (
                        <span className="text-gray-400">—</span>
                      ) : (
                        <span className="max-w-[150px] truncate text-gray-800 dark:text-gray-100">
                          {Array.isArray(f.value) ? f.value.join(', ') : String(f.value)}
                        </span>
                      )}
                      {sourceBadge(f)}
                    </span>
                  </li>
                ))}
            </ul>
            <p className="mt-3 text-[11px] leading-relaxed text-gray-400">
              Blank means we have no authorised record of it. We ask you instead of guessing —
              an honest blank beats a polished lie.
            </p>
          </section>

          {questions.length > 0 && (
            <section className="rounded-xl border border-amber-200 bg-amber-50/50 p-4 dark:border-amber-800">
              <h2 className="mb-2 text-sm font-semibold text-amber-800">2. Missing information</h2>
              <div className="space-y-3">
                {questions.map((q) => (
                  <div key={q.field}>
                    <label className="mb-1 block text-xs text-gray-600">
                      {q.question}{' '}
                      {q.required && <span className="text-red-500">*</span>}
                    </label>
                    <textarea
                      className="w-full rounded-lg border border-gray-300 p-2 text-sm focus:border-blue-500 focus:outline-none"
                      rows={q.kind === 'long_text' ? 3 : 2}
                      value={answers[q.field] || ''}
                      onChange={(e) => setAnswers((a) => ({ ...a, [q.field]: e.target.value }))}
                      placeholder="Your words…"
                    />
                    <button
                      onClick={() => applyAnswer(q.field)}
                      className="mt-1 rounded bg-gray-800 px-2 py-1 text-xs text-white hover:bg-gray-900"
                    >
                      Add to CV
                    </button>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
            <h2 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-200">
              {questions.length > 0 ? '3' : '2'}. Template
            </h2>
            <div className="space-y-2">
              {TEMPLATES.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTemplate(t.id)}
                  className={`w-full rounded-lg border p-3 text-left text-sm transition ${
                    template === t.id
                      ? 'border-blue-600 bg-blue-50 text-blue-800'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <span className="font-medium">{t.label}</span>
                  <span className="mt-0.5 block text-xs text-gray-500">{t.desc}</span>
                </button>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-gray-400">
              Switching templates only restyles — your content is never erased.
            </p>
          </section>

          {proposals.length > 0 && (
            <section className="rounded-xl border border-sky-200 bg-sky-50/60 p-4">
              <h2 className="mb-2 text-sm font-semibold text-sky-800">Proposed changes — review first</h2>
              <div className="space-y-3">
                {proposals.map((p) => (
                  <div key={p.proposal_id} className="rounded-lg border border-sky-200 bg-white p-2 text-xs">
                    <div className="text-gray-500 line-through">{p.original}</div>
                    <div className="text-gray-900">{p.proposed}</div>
                    <div className="mt-1 text-[10px] text-gray-400">{p.reason}</div>
                    <div className="mt-2 flex gap-2">
                      <button
                        onClick={() => applyProposal(p, true)}
                        className="rounded bg-sky-600 px-2 py-1 text-white"
                      >
                        Accept
                      </button>
                      <button
                        onClick={() => applyProposal(p, false)}
                        className="rounded border border-gray-300 px-2 py-1 text-gray-600"
                      >
                        Discard
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        {/* ---------------- Right: editor + preview ---------------------------- */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200">Editor</h2>
            <button
              onClick={addSection}
              className="rounded-lg border border-dashed border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
            >
              + Add section
            </button>
          </div>

          {content.sections.map((section, index) => (
            <div
              key={section.id}
              className={`rounded-xl border p-4 ${
                section.visible
                  ? 'border-gray-200 dark:border-gray-700'
                  : 'border-gray-200 opacity-50 dark:border-gray-700'
              }`}
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <input
                  value={section.heading}
                  onChange={(e) => updateSection(section.id, { heading: e.target.value })}
                  className="flex-1 rounded border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-gray-800 hover:border-gray-200 focus:border-blue-400 focus:outline-none dark:text-gray-100"
                />
                <button onClick={() => moveSection(index, -1)} title="Move up" className="text-xs text-gray-400 hover:text-gray-700">▲</button>
                <button onClick={() => moveSection(index, 1)} title="Move down" className="text-xs text-gray-400 hover:text-gray-700">▼</button>
                <label className="flex items-center gap-1 text-xs text-gray-500">
                  <input
                    type="checkbox"
                    checked={section.visible}
                    onChange={(e) => updateSection(section.id, { visible: e.target.checked })}
                  />
                  show
                </label>
                {section.type !== 'header' && (
                  <button
                    onClick={() => removeSection(section.id)}
                    className="text-xs text-red-400 hover:text-red-600"
                  >
                    remove
                  </button>
                )}
              </div>

              <div className="space-y-2">
                {section.entries.map((entry) => (
                  <div key={entry.id} className="rounded-lg bg-gray-50 p-2 dark:bg-gray-800">
                    <div className="flex gap-2">
                      {section.type !== 'summary' && (
                        <input
                          placeholder="Title (role, degree…)"
                          value={entry.title || ''}
                          onChange={(e) => updateEntry(section.id, entry.id, { title: e.target.value })}
                          className="w-1/3 rounded border border-gray-200 px-2 py-1 text-xs"
                        />
                      )}
                      <input
                        placeholder="Subtext (organisation, dates…)"
                        value={entry.subtext || ''}
                        onChange={(e) => updateEntry(section.id, entry.id, { subtext: e.target.value })}
                        className="w-1/4 rounded border border-gray-200 px-2 py-1 text-xs"
                      />
                      <button
                        onClick={() => removeEntry(section.id, entry.id)}
                        className="ml-auto text-xs text-red-400 hover:text-red-600"
                      >
                        delete
                      </button>
                    </div>
                    <textarea
                      placeholder="Your text…"
                      rows={section.type === 'summary' ? 3 : 2}
                      value={entry.text || ''}
                      onChange={(e) => updateEntry(section.id, entry.id, { text: e.target.value })}
                      className="mt-1 w-full rounded border border-gray-200 px-2 py-1 text-sm"
                    />
                    <div className="mt-1 flex items-center justify-between">
                      <span className="text-[10px] text-gray-400">You wrote this — edits are yours</span>
                      <button
                        onClick={() => requestSuggestions(section.id, entry)}
                        disabled={busyProposal === entry.id}
                        className="rounded border border-sky-300 px-2 py-0.5 text-[11px] text-sky-700 hover:bg-sky-50 disabled:opacity-50"
                      >
                        {busyProposal === entry.id ? 'Checking…' : 'Suggest grammar fix'}
                      </button>
                    </div>
                  </div>
                ))}
                <button
                  onClick={() => addEntry(section.id)}
                  className="text-xs text-blue-600 hover:underline"
                >
                  + Add entry
                </button>
              </div>
            </div>
          ))}

          {showApprove && (
            <div className="rounded-xl border border-emerald-300 bg-emerald-50 p-4 text-sm">
              <p className="mb-2 font-medium text-emerald-800">
                Confirm that you have reviewed every word below. Approval is recorded and required
                before export; any later edit locks export again.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={approveCv}
                  className="rounded-lg bg-emerald-600 px-3 py-1.5 text-white"
                >
                  I understand — approve
                </button>
                <button
                  onClick={() => setShowApprove(false)}
                  className="rounded-lg border border-gray-300 px-3 py-1.5"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {previewHtml && (
        <div className="mt-6">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700">Preview ({template})</h2>
            <button onClick={() => setPreviewHtml(null)} className="text-xs text-gray-400">
              close
            </button>
          </div>
          <iframe title="CV preview" srcDoc={previewHtml} className="h-[70vh] w-full rounded-xl border" />
        </div>
      )}
    </div>
  )
}
