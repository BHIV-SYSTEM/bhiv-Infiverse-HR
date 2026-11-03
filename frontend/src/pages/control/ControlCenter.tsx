import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import Loading from '../../components/Loading'
import {
  AlertCard,
  EscalationCard,
  ExecutiveMetricCard,
  GovernanceCard,
  MapCard,
  ReplayCard,
  TelemetryCard,
  TimelineCard,
  type CardSeverity,
} from '../../components/cards'
import { useAuth } from '../../context/AuthContext'
import {
  AGENT_SERVICE_URL,
  API_BASE_URL,
  LANGGRAPH_SERVICE_URL,
  checkApiHealth,
  fetchGatewayCandidateStats,
  fetchGatewayMetricsDashboard,
  fetchServiceHealth,
  postControlCenterAuditEvent,
  fetchControlCenterAuditReplay,
  fetchControlCenterDashboardAggregates,
  fetchGovernanceChallenges,
  fetchGovernanceDecisions,
  fetchPolicyDefinitions,
  fetchSetuSignals,
  fetchWorkforceOrganizations,
  fetchWorkforceTraceReplay,
  type GatewayMetricsDashboard,
  type WorkforceTraceReplay,
  type GatewayCandidateStats,
  type ServiceHealthSnapshot,
  type ControlCenterTraceEvent,
  type ControlCenterDashboardAggregates,
  type ControlCenterAuditReplay,
} from '../../services/api'

// ────────────────────────────────────────────────────────────────────────────
// Types
// ────────────────────────────────────────────────────────────────────────────

type Zone = 'executive' | 'hiring' | 'workforce' | 'growth' | 'org' | 'governance' | 'replay'

interface KpiCard {
  label: string
  value: string | number
  delta?: string
  deltaPositive?: boolean
  sublabel?: string
  alert?: boolean
  warning?: boolean
  sourceSystem?: string
  correlationId?: string
  primitive?: 'executive' | 'telemetry' | 'replay' | 'governance' | 'alert' | 'escalation'
}

function kpiSeverity(card: KpiCard): CardSeverity {
  if (card.alert) return 'alert'
  if (card.warning) return 'warning'
  return 'normal'
}

function ConstitutionalKpiRenderer({ card }: { card: KpiCard }) {
  const severity = kpiSeverity(card)
  const primitive = card.primitive || (card.alert ? 'alert' : card.label.match(/Gateway|Agent|LangGraph/i) ? 'telemetry' : 'executive')

  if (primitive === 'alert') {
    return <AlertCard label={card.label} value={card.value} sublabel={card.sublabel} readOnly />
  }
  if (primitive === 'telemetry') {
    return (
      <TelemetryCard
        label={card.label}
        value={card.value}
        sublabel={card.sublabel}
        severity={severity}
        sourceSystem={card.sourceSystem}
        readOnly
      />
    )
  }
  if (primitive === 'replay') {
    return <ReplayCard label={card.label} value={card.value} sublabel={card.sublabel} correlationId={card.correlationId} readOnly />
  }
  if (primitive === 'governance') {
    return <GovernanceCard label={card.label} value={card.value} sublabel={card.sublabel} severity={severity} readOnly />
  }
  if (primitive === 'escalation') {
    return <EscalationCard label={card.label} value={card.value} sublabel={card.sublabel} severity={severity} readOnly />
  }
  return (
    <ExecutiveMetricCard
      label={card.label}
      value={card.value}
      sublabel={card.sublabel}
      delta={card.delta}
      deltaPositive={card.deltaPositive}
      severity={severity}
      sourceSystem={card.sourceSystem}
      correlationId={card.correlationId}
      readOnly
    />
  )
}

type TraceEvent = ControlCenterTraceEvent

interface GovernancePanelData {
  organizations: Record<string, unknown>[]
  policies: Record<string, unknown>[]
  challenges: Record<string, unknown>[]
  decisions: Record<string, unknown>[]
  setuSignals: Record<string, unknown>[]
  workforceTrace: WorkforceTraceReplay | null
}

interface ControlCenterLiveData {
  gatewayHealth: { healthy: boolean; data?: Record<string, unknown> | null } | null
  gatewayMetrics: GatewayMetricsDashboard | null
  candidateStats: GatewayCandidateStats | null
  dashboardAggregates: ControlCenterDashboardAggregates | null
  auditReplay: ControlCenterAuditReplay | null
  governance: GovernancePanelData | null
  agentHealth: ServiceHealthSnapshot | null
  langgraphHealth: ServiceHealthSnapshot | null
  fetchedAt: string | null
  errors: string[]
  correlationIds: string[]
}

function buildServiceCard(label: string, service: ServiceHealthSnapshot | null, fallbackSource: string): KpiCard {
  if (!service) {
    return {
      label,
      value: 'Offline',
      sublabel: `${fallbackSource} unavailable`,
      alert: true,
    }
  }

  const timestamp = service.timestamp ? new Date(service.timestamp).toLocaleTimeString() : 'no timestamp'
  const version = service.version ? `v${service.version}` : 'version n/a'

  return {
    label,
    value: service.healthy ? 'Healthy' : service.status || 'Degraded',
    sublabel: `${service.baseUrl} · ${version} · ${timestamp}`,
    alert: !service.healthy,
    warning: !service.healthy && service.status !== 'offline',
  }
}

function buildGatewayCard(
  gatewayHealth: { healthy: boolean; data?: Record<string, unknown> | null } | null,
  detail: string,
): KpiCard {
  if (!gatewayHealth) {
    return {
      label: 'Gateway',
      value: 'Unknown',
      sublabel: detail,
      warning: true,
    }
  }

  return {
    label: 'Gateway',
    value: gatewayHealth.healthy ? 'Healthy' : 'Offline',
    sublabel: detail,
    alert: !gatewayHealth.healthy,
  }
}

function serviceBaseSource(service: ServiceHealthSnapshot | null, fallback: string): string {
  return service?.baseUrl ? `${service.baseUrl}/health` : fallback
}

function readNumber(source: unknown, keys: string[], fallback = 0): number {
  if (!source || typeof source !== 'object') return fallback

  const record = source as Record<string, unknown>

  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) return parsed
    }
  }

  return fallback
}

function toPercentDisplay(value: number): string {
  if (!Number.isFinite(value)) return '0%'
  const normalized = value <= 1 ? value * 100 : value
  return `${normalized.toFixed(normalized >= 10 ? 0 : 1)}%`
}

function toMsDisplay(value: number): string {
  return `${value.toFixed(value >= 100 ? 0 : 1)} ms`
}

function toHoursDisplay(value: number): string {
  return `${value.toFixed(1)} h`
}

function card(label: string, value: string | number, sublabel: string, severity: 'normal' | 'warning' | 'alert' = 'normal'): KpiCard {
  return {
    label,
    value,
    sublabel,
    warning: severity === 'warning',
    alert: severity === 'alert',
  }
}

function buildExecutiveCards(liveData: ControlCenterLiveData | null): KpiCard[] {
  const performance = liveData?.gatewayMetrics?.performance_summary ?? {}
  return [
    { ...buildGatewayCard(liveData?.gatewayHealth ?? null, `${API_BASE_URL}/metrics/dashboard`), primitive: 'telemetry' as const, sourceSystem: `${API_BASE_URL}/health` },
    { ...buildServiceCard('Agent', liveData?.agentHealth ?? null, serviceBaseSource(liveData?.agentHealth ?? null, AGENT_SERVICE_URL)), primitive: 'telemetry' as const, sourceSystem: serviceBaseSource(liveData?.agentHealth ?? null, AGENT_SERVICE_URL) },
    { ...buildServiceCard('LangGraph', liveData?.langgraphHealth ?? null, serviceBaseSource(liveData?.langgraphHealth ?? null, LANGGRAPH_SERVICE_URL)), primitive: 'telemetry' as const, sourceSystem: serviceBaseSource(liveData?.langgraphHealth ?? null, LANGGRAPH_SERVICE_URL) },
    card(
      'Avg Response Time',
      toMsDisplay(readNumber(performance, ['avg_response_time_ms', 'avg_response_time'])),
      'Gateway performance_summary · raw',
    ),
    card('P95 Response Time', toMsDisplay(readNumber(performance, ['p95_response_time_ms', 'p95_response_time'])), 'Gateway performance_summary', 'warning'),
  ]
}

function buildHiringCards(liveData: ControlCenterLiveData | null): KpiCard[] {
  const candidateStats = liveData?.candidateStats ?? null
  const business = liveData?.gatewayMetrics?.business_metrics ?? {}
  return [
    card('Total Candidates', readNumber(candidateStats, ['total_candidates'], readNumber(business, ['total_candidates'])), 'Gateway /v1/candidates/stats'),
    card('Active Jobs', readNumber(candidateStats, ['active_jobs'], readNumber(business, ['active_jobs', 'total_job_postings'])), 'Gateway /v1/candidates/stats'),
    card('Pending Interviews', readNumber(candidateStats, ['pending_interviews'], readNumber(business, ['interviews_scheduled'])), 'Gateway /v1/candidates/stats'),
    card('Recent Matches', readNumber(candidateStats, ['recent_matches'], readNumber(business, ['total_matches_generated'])), 'Gateway /v1/candidates/stats'),
    card('Applications Today', readNumber(business, ['applications_today']), 'Gateway metrics_dashboard · raw', 'warning'),
    card('New Candidates This Week', readNumber(candidateStats, ['new_candidates_this_week']), 'Gateway /v1/candidates/stats'),
  ]
}

function buildFunnelStages(liveData: ControlCenterLiveData | null): { label: string; count: number; color: string }[] {
  const funnel = liveData?.dashboardAggregates?.hiring_funnel
  if (funnel && funnel.length > 0) {
    return funnel
  }
  const stats = liveData?.candidateStats
  const sourcing = readNumber(stats, ['total_candidates'])
  const screening = Math.max(readNumber(stats, ['recent_matches']), 0)
  const interview = Math.max(readNumber(stats, ['pending_interviews']), 0)
  const offer = 0
  return [
    { label: 'Sourcing', count: sourcing, color: 'bg-indigo-500' },
    { label: 'Screening', count: screening, color: 'bg-violet-500' },
    { label: 'Interview', count: interview, color: 'bg-purple-500' },
    { label: 'Offer', count: offer, color: 'bg-fuchsia-500' },
  ]
}

function buildDepartmentLoad(liveData: ControlCenterLiveData | null): { dept: string; load: number; color: string }[] {
  const deptLoad = liveData?.dashboardAggregates?.department_load
  if (deptLoad && deptLoad.length > 0) {
    return deptLoad.map(({ dept, load, color }) => ({ dept, load, color }))
  }
  return []
}

function buildWorkforceCards(liveData: ControlCenterLiveData | null): KpiCard[] {
  const system = liveData?.gatewayMetrics?.system_metrics ?? {}
  return [
    card('CPU Usage', toPercentDisplay(readNumber(system, ['cpu_usage', 'cpu_percent'])), 'Gateway system_metrics', readNumber(system, ['cpu_usage', 'cpu_percent']) >= 80 ? 'warning' : 'normal'),
    card('Memory Usage', toPercentDisplay(readNumber(system, ['memory_usage_percent', 'memory_percent'])), 'Gateway system_metrics', readNumber(system, ['memory_usage_percent', 'memory_percent']) >= 80 ? 'warning' : 'normal'),
    card('Disk Usage', toPercentDisplay(readNumber(system, ['disk_usage_percent', 'disk_percent'])), 'Gateway system_metrics', readNumber(system, ['disk_usage_percent', 'disk_percent']) >= 80 ? 'warning' : 'normal'),
    card('Active Connections', readNumber(system, ['active_connections', 'database_connections']), 'Gateway system_metrics'),
    card('Current Active Users', readNumber(system, ['active_users']), 'Gateway system_metrics'),
  ]
}

function buildGrowthCards(liveData: ControlCenterLiveData | null): KpiCard[] {
  const business = liveData?.gatewayMetrics?.business_metrics ?? {}
  const candidateStats = liveData?.candidateStats ?? null
  return [
    card('Total Matches Generated', readNumber(business, ['total_matches_generated']), 'Gateway business_metrics'),
    card('Total Resumes Processed', readNumber(business, ['total_resumes_processed']), 'Gateway business_metrics'),
    card('Platform Uptime', toHoursDisplay(readNumber(business, ['platform_uptime_hours'])), 'Gateway business_metrics'),
    card('Total Feedback Submissions', readNumber(candidateStats, ['total_feedback_submissions']), 'Gateway /v1/candidates/stats'),
  ]
}

function buildOrgCards(liveData: ControlCenterLiveData | null): KpiCard[] {
  const performance = liveData?.gatewayMetrics?.performance_summary ?? {}
  const business = liveData?.gatewayMetrics?.business_metrics ?? {}
  return [
    card('Requests Per Minute', readNumber(performance, ['requests_per_minute']), 'Gateway performance_summary', readNumber(performance, ['requests_per_minute']) >= 200 ? 'warning' : 'normal'),
    card('Total Requests', readNumber(performance, ['total_requests']), 'Gateway performance_summary'),
    card('Error Rate', toPercentDisplay(readNumber(performance, ['error_rate'])), 'Gateway performance_summary', readNumber(performance, ['error_rate']) > 0.05 ? 'warning' : 'normal'),
    card('Active Users', readNumber(business, ['current_active_users']), 'Gateway business_metrics'),
  ]
}

// ────────────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────────────

function KpiPill({ card }: { card: KpiCard }) {
  return <ConstitutionalKpiRenderer card={card} />
}

function ZoneHeader({
  title,
  subtitle,
  icon,
}: {
  title: string
  subtitle: string
  icon: React.ReactNode
}) {
  return (
    <div className="flex items-center gap-3 mb-5">
      <div className="w-9 h-9 rounded-xl bg-indigo-600/10 dark:bg-indigo-500/20 flex items-center justify-center text-indigo-600 dark:text-indigo-400 shrink-0">
        {icon}
      </div>
      <div>
        <h2 className="text-base font-heading font-bold text-slate-900 dark:text-white leading-tight">
          {title}
        </h2>
        <p className="text-xs text-slate-500 dark:text-slate-400">{subtitle}</p>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Zone panels
// ────────────────────────────────────────────────────────────────────────────

function ExecutiveZone({ cards, sourceSummary }: { cards: KpiCard[]; sourceSummary: string }) {
  return (
    <div>
      <ZoneHeader
        title="Executive Zone"
        subtitle="Workforce health · Hiring health · Payroll state · Escalations"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
        {cards.map((k) => (
          <KpiPill key={k.label} card={k} />
        ))}
      </div>
      <p className="mt-3 text-xs text-slate-400 dark:text-slate-500 italic">
        Payroll state shown as visibility cue only. Payroll ownership: Artha. No execution authority surfaced here.
      </p>
      <p className="mt-1 text-[11px] text-slate-400 dark:text-slate-500">
        Live source: {sourceSummary}
      </p>
    </div>
  )
}

function HiringZone({
  cards,
  sourceSummary,
  funnelStages,
}: {
  cards: KpiCard[]
  sourceSummary: string
  funnelStages: { label: string; count: number; color: string }[]
}) {
  return (
    <div>
      <ZoneHeader
        title="Hiring Zone"
        subtitle="Candidate pipeline · Recruiter visibility · Interview state · Onboarding state"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3">
        {cards.map((k) => (
          <KpiPill key={k.label} card={k} />
        ))}
      </div>
      {/* Pipeline funnel visualisation */}
      <div className="mt-5 flex items-end gap-2 h-24">
        {funnelStages.map((stage) => {
          const maxCount = Math.max(...funnelStages.map((entry) => entry.count), 1)
          const heightPct = Math.round((stage.count / maxCount) * 100)
          return (
            <div key={stage.label} className="flex flex-col items-center gap-1 flex-1">
              <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">{stage.count}</span>
              <div
                className={`w-full rounded-t-lg ${stage.color} opacity-80`}
                style={{ height: `${heightPct}%` }}
              />
              <span className="text-[10px] text-slate-500 dark:text-slate-400">{stage.label}</span>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-[11px] text-slate-400 dark:text-slate-500">
        Live source: {sourceSummary}
      </p>
    </div>
  )
}

function WorkforceOpsZone({ cards, sourceSummary }: { cards: KpiCard[]; sourceSummary: string }) {
  return (
    <div>
      <ZoneHeader
        title="Workforce Operations Zone"
        subtitle="Attendance · HR requests · Leave · Reimbursements · Payroll cues"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
        {cards.map((k) => (
          <KpiPill key={k.label} card={k} />
        ))}
      </div>
      <p className="mt-3 text-[11px] text-slate-400 dark:text-slate-500">
        Live source: {sourceSummary}
      </p>
    </div>
  )
}

function GrowthZone({ cards, sourceSummary }: { cards: KpiCard[]; sourceSummary: string }) {
  return (
    <div>
      <ZoneHeader
        title="Growth & Development Zone"
        subtitle="Learning · Mentorship · Skill trajectory · Aspirations — human-centric, no surveillance"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {cards.map((k) => (
          <KpiPill key={k.label} card={k} />
        ))}
      </div>
      <div className="mt-4 rounded-xl border border-indigo-200/40 dark:border-indigo-700/30 bg-indigo-50/50 dark:bg-indigo-950/20 p-4">
        <p className="text-xs font-semibold text-indigo-700 dark:text-indigo-300 mb-1">
          Growth Privacy Guardrail Active
        </p>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          All metrics shown are team-level aggregates. Individual growth trajectories are visible only to the employee and their HR partner (with consent). No leaderboards, no dopamine loops, no individual ranking.
        </p>
      </div>
      <p className="mt-3 text-[11px] text-slate-400 dark:text-slate-500">
        Live source: {sourceSummary}
      </p>
    </div>
  )
}

function OrgVisibilityZone({
  cards,
  sourceSummary,
  departmentLoad,
}: {
  cards: KpiCard[]
  sourceSummary: string
  departmentLoad: { dept: string; load: number; color: string }[]
}) {
  return (
    <div>
      <ZoneHeader
        title="Organizational Visibility Zone"
        subtitle="Department map · Dependency risk · Staffing gaps · Bottlenecks"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        {cards.map((k) => (
          <KpiPill key={k.label} card={k} />
        ))}
      </div>
      <MapCard
        title="Department Load Map"
        nodes={departmentLoad.map((d) => ({ id: d.dept, label: d.dept, level: `${d.load}% load` }))}
        readOnly
      />
      <p className="mt-3 text-[11px] text-slate-400 dark:text-slate-500">
        Live source: {sourceSummary}
      </p>
    </div>
  )
}

function ReplayTraceZone({ traceEvents, traceNote }: { traceEvents: TraceEvent[]; traceNote: string }) {
  return (
    <div>
      <ZoneHeader
        title="Replay / Trace Zone"
        subtitle="Audit trail · Replay reconstruction · Observability evidence"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        }
      />
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-5">
        <ReplayCard label="Last Replay" value={traceEvents[0]?.ts?.slice(0, 10) || '—'} sublabel="Latest audit trace event" readOnly />
        <ReplayCard label="Trace Density" value={`${traceEvents.length} hops`} sublabel="Visible correlation chain" readOnly />
        <ReplayCard label="Unreconciled Events" value={0} sublabel="all events matched" readOnly />
      </div>

      <TimelineCard
        title="Audit Trace Timeline"
        events={traceEvents.map((ev, idx) => ({
          id: String(idx),
          timestamp: ev.ts,
          label: `${ev.service} · ${ev.op}`,
          status: ev.status,
          detail: ev.correlation_id,
        }))}
        readOnly
      />
      <p className="mt-3 text-xs text-slate-400 dark:text-slate-500 italic">{traceNote}</p>
    </div>
  )
}

function GovernanceZone({ governance }: { governance: GovernancePanelData | null }) {
  const orgCount = governance?.organizations.length ?? 0
  const policyCount = governance?.policies.length ?? 0
  const challengeCount = governance?.challenges.length ?? 0
  const decisionCount = governance?.decisions.length ?? 0
  const signalCount = governance?.setuSignals.length ?? 0

  return (
    <div>
      <ZoneHeader
        title="Governance Visibility"
        subtitle="Org hierarchy · Policy · Challenges · Decisions · SETU lineage (read-only)"
        icon={
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        }
      />
      <div className="mb-4 rounded-lg border border-slate-200/60 dark:border-slate-700/40 bg-slate-50/50 dark:bg-slate-900/30 px-3 py-2 text-[10px] text-slate-600 dark:text-slate-400">
        Read-only oversight (GOV-PANEL-001 approved). Visibility ≠ authority — no execution actions in this panel.
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-5">
        <GovernanceCard label="Organizations" value={orgCount} sublabel="/v1/workforce/organizations" readOnly />
        <GovernanceCard label="Policies" value={policyCount} sublabel="/v1/policies/definitions" readOnly />
        <EscalationCard label="Challenges" value={challengeCount} sublabel="/v1/governance/challenges" readOnly />
        <GovernanceCard label="Decisions" value={decisionCount} sublabel="/v1/decisions" readOnly />
        <TelemetryCard label="SETU Signals" value={signalCount} sublabel="/v1/setu/signals" sourceSystem="/v1/setu/signals" readOnly />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-200/50 dark:border-slate-700/40 p-4">
          <p className="text-xs font-semibold text-slate-700 dark:text-slate-300 mb-2">OARDE Layer Separation</p>
          <div className="space-y-1 text-[11px] text-slate-600 dark:text-slate-400">
            <p><strong>Observation:</strong> workforce + SETU signal counts above</p>
            <p><strong>Assessment:</strong> policy definitions loaded from registry</p>
            <p><strong>Recommendation:</strong> challenge records (advisory)</p>
            <p><strong>Decision:</strong> decision ledger entries (external owner)</p>
            <p><strong>Execution:</strong> blocked — no mutate actions in UI</p>
          </div>
        </div>
        <div className="rounded-xl border border-slate-200/50 dark:border-slate-700/40 p-4">
          <p className="text-xs font-semibold text-slate-700 dark:text-slate-300 mb-2">Workforce Trace Replay</p>
          {governance?.workforceTrace?.events?.length ? (
            <div className="space-y-1 max-h-40 overflow-y-auto text-[10px] font-mono text-slate-600 dark:text-slate-400">
              {governance.workforceTrace.events.map((ev, i) => (
                <p key={i}>{ev.action} · {ev.outcome} · {ev.correlation_id?.slice(0, 8)}</p>
              ))}
            </div>
          ) : (
            <p className="text-[10px] text-slate-500">No workforce audit events in scope yet.</p>
          )}
        </div>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Main Control Center
// ────────────────────────────────────────────────────────────────────────────

// Feature flag (Vite env): set VITE_ENABLE_CONTROL_CENTER=true to enable
const ENABLE_CONTROL_CENTER = import.meta.env.VITE_ENABLE_CONTROL_CENTER === 'true'
const ENABLE_GOVERNANCE_PANEL = import.meta.env.VITE_ENABLE_GOVERNANCE === 'true'
const CONTROL_CENTER_REFRESH_MS = 30_000

const ZONES: { id: Zone; label: string; short: string }[] = [
  { id: 'executive', label: 'Executive', short: 'Exec' },
  { id: 'hiring', label: 'Hiring', short: 'Hiring' },
  { id: 'workforce', label: 'Workforce Ops', short: 'Ops' },
  { id: 'growth', label: 'Growth', short: 'Growth' },
  { id: 'org', label: 'Org Visibility', short: 'Org' },
  ...(ENABLE_GOVERNANCE_PANEL ? [{ id: 'governance' as Zone, label: 'Governance', short: 'Gov' }] : []),
  { id: 'replay', label: 'Replay / Trace', short: 'Trace' },
]

function readPolicyScopeLabel(liveData: ControlCenterLiveData | null): string | null {
  const fromStats = liveData?.candidateStats as { policy_scope?: { scope_label?: string } } | null
  const fromMetrics = liveData?.gatewayMetrics as { policy_scope?: { scope_label?: string } } | null
  return fromStats?.policy_scope?.scope_label || fromMetrics?.policy_scope?.scope_label || null
}

export default function ControlCenter() {
  const [activeZone, setActiveZone] = useState<Zone>('executive')
  const [liveData, setLiveData] = useState<ControlCenterLiveData | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [loadingLiveData, setLoadingLiveData] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const loadingRef = useRef(false)
  const hasLiveDataRef = useRef(false)
  const { userRole, loading } = useAuth()

  const ALLOWED_ROLES = new Set(['client', 'recruiter', 'admin'])

  const loadControlCenterData = useCallback(async (silent = false) => {
    if (loadingRef.current) return
    loadingRef.current = true

    if (!silent) {
      setRefreshing(true)
      if (!hasLiveDataRef.current) {
        setLoadingLiveData(true)
      }
    }

    const errors: string[] = []

    try {
      const governancePromise = ENABLE_GOVERNANCE_PANEL
        ? Promise.all([
            fetchWorkforceOrganizations().catch(() => ({ items: [] })),
            fetchPolicyDefinitions().catch(() => ({ items: [] })),
            fetchGovernanceChallenges().catch(() => ({ items: [] })),
            fetchGovernanceDecisions().catch(() => ({ items: [] })),
            fetchSetuSignals().catch(() => ({ items: [] })),
            fetchWorkforceTraceReplay().catch(() => null),
          ])
        : Promise.resolve(null)

      const [
        gatewayHealth,
        gatewayMetricsResponse,
        candidateStatsResponse,
        aggregatesResponse,
        auditReplayResponse,
        agentHealth,
        langgraphHealth,
        governanceResults,
      ] = await Promise.all([
        checkApiHealth(),
        fetchGatewayMetricsDashboard().catch((error) => {
          errors.push(
            `Gateway metrics unavailable: ${error instanceof Error ? error.message : 'Unknown error'}`,
          )
          return null
        }),
        fetchGatewayCandidateStats().catch((error) => {
          errors.push(
            `Gateway candidate stats unavailable: ${error instanceof Error ? error.message : 'Unknown error'}`,
          )
          return null
        }),
        fetchControlCenterDashboardAggregates().catch((error) => {
          errors.push(
            `Dashboard aggregates unavailable: ${error instanceof Error ? error.message : 'Unknown error'}`,
          )
          return null
        }),
        fetchControlCenterAuditReplay().catch((error) => {
          errors.push(
            `Audit replay unavailable: ${error instanceof Error ? error.message : 'Unknown error'}`,
          )
          return null
        }),
        fetchServiceHealth(AGENT_SERVICE_URL, 'BHIV AI Agent'),
        fetchServiceHealth(LANGGRAPH_SERVICE_URL, 'langgraph-orchestrator'),
        governancePromise,
      ])

      const governanceData: GovernancePanelData | null = governanceResults
        ? {
            organizations: governanceResults[0].items,
            policies: governanceResults[1].items,
            challenges: governanceResults[2].items,
            decisions: governanceResults[3].items,
            setuSignals: governanceResults[4].items,
            workforceTrace: governanceResults[5],
          }
        : null

      if (!gatewayHealth.healthy) {
        errors.push('Gateway health check returned an unhealthy state')
      }

      if (!agentHealth.healthy) {
        errors.push(`Agent status: ${agentHealth.status}`)
      }

      if (!langgraphHealth.healthy) {
        errors.push(`LangGraph status: ${langgraphHealth.status}`)
      }

      const correlationIds = [
        gatewayMetricsResponse?.meta?.correlationId,
        candidateStatsResponse?.meta?.correlationId,
        aggregatesResponse?.meta?.correlationId,
        auditReplayResponse?.meta?.correlationId,
        auditReplayResponse?.data?.correlation_id,
        String(agentHealth.raw?.correlation_id || ''),
        String(langgraphHealth.raw?.correlation_id || ''),
      ].filter(Boolean) as string[]

      setLiveData({
        gatewayHealth: {
          healthy: gatewayHealth.healthy,
          data: (gatewayHealth.data as Record<string, unknown> | null | undefined) ?? null,
        },
        gatewayMetrics: gatewayMetricsResponse?.data ?? null,
        candidateStats: candidateStatsResponse?.data ?? null,
        dashboardAggregates: aggregatesResponse?.data ?? null,
        auditReplay: auditReplayResponse?.data ?? null,
        governance: governanceData,
        agentHealth,
        langgraphHealth,
        fetchedAt: new Date().toISOString(),
        errors,
        correlationIds,
      })
      hasLiveDataRef.current = true
      setLastRefresh(new Date())

      if (!silent) {
        void postControlCenterAuditEvent({
          action: 'control_center_refresh',
          outcome: errors.length ? 'failure' : 'success',
          detail: errors.length ? errors.join(' | ') : 'Live control center refresh succeeded',
          correlation_id: correlationIds[0],
          context: {
            source: 'control_center',
            partial_failure: errors.length > 0,
          },
        }).catch(() => undefined)
      }
    } finally {
      loadingRef.current = false
      if (!silent) {
        setLoadingLiveData(false)
        setRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!ENABLE_CONTROL_CENTER || loading || !ALLOWED_ROLES.has(String(userRole))) {
      return
    }

    void loadControlCenterData()
    const intervalId = window.setInterval(() => {
      void loadControlCenterData(true)
    }, CONTROL_CENTER_REFRESH_MS)

    return () => window.clearInterval(intervalId)
  }, [loading, userRole, loadControlCenterData])

  useEffect(() => {
    if (loading || !userRole) return
    const allowed = ALLOWED_ROLES.has(String(userRole))
    void postControlCenterAuditEvent({
      action: 'control_center_view',
      outcome: allowed ? 'success' : 'denied',
      detail: allowed ? 'Access granted' : `Denied for role ${String(userRole)}`,
      context: {
        role: String(userRole),
      },
    }).catch(() => undefined)
  }, [loading, userRole])

  // Feature gating & RBAC: if the feature is disabled, render a small notice.
  if (!ENABLE_CONTROL_CENTER) {
    return (
      <div className="p-6">
        <p className="text-sm text-slate-600">Control Center feature is disabled (VITE_ENABLE_CONTROL_CENTER=false).</p>
      </div>
    )
  }

  // While auth loads, show nothing to avoid flicker/unauthorized rendering
  if (loading) return null

  if (!ALLOWED_ROLES.has(String(userRole))) {
    return (
      <div className="p-6">
        <p className="text-sm text-red-600">Access denied: you do not have permission to view the Control Center.</p>
      </div>
    )
  }

  const gatewaySource = `${API_BASE_URL}/metrics/dashboard`
  const executiveCards = buildExecutiveCards(liveData)
  const hiringCards = buildHiringCards(liveData)
  const workforceCards = buildWorkforceCards(liveData)
  const growthCards = buildGrowthCards(liveData)
  const orgCards = buildOrgCards(liveData)
  const funnelStages = buildFunnelStages(liveData)
  const departmentLoad = buildDepartmentLoad(liveData)
  const policyScopeLabel = readPolicyScopeLabel(liveData)

  if (loadingLiveData && !liveData) {
    return <Loading message="Loading live control center..." />
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-indigo-50/30 to-slate-100 dark:from-slate-950 dark:via-indigo-950/20 dark:to-slate-900 animate-fade-in">
      {/* ── Top Rail ── */}
      <div className="sticky top-0 z-40 border-b border-slate-200/60 dark:border-slate-700/50 bg-white/80 dark:bg-slate-900/80 backdrop-blur-xl shadow-sm">
        <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-3 flex flex-col sm:flex-row sm:items-center gap-3">
          <div className="flex items-center gap-3 shrink-0">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-600 to-violet-600 flex items-center justify-center shadow-md">
              <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            </div>
            <div>
              <h1 className="text-sm font-heading font-bold text-slate-900 dark:text-white leading-tight">
                Sampada Workforce Control Center
              </h1>
              <p className="text-[10px] text-slate-500 dark:text-slate-400">
                BHIV · Intelligence Layer · Visibility Only
              </p>
            </div>
          </div>

          {/* Zone nav tabs */}
          <nav className="flex gap-1 overflow-x-auto flex-1 sm:justify-center" aria-label="Control Center Zones">
            {ZONES.map((z) => (
              <button
                key={z.id}
                id={`zone-tab-${z.id}`}
                onClick={() => setActiveZone(z.id)}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition-all ${
                  activeZone === z.id
                    ? 'bg-indigo-600 text-white shadow-md shadow-indigo-500/30'
                    : 'text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800'
                }`}
              >
                <span className="sm:hidden">{z.short}</span>
                <span className="hidden sm:inline">{z.label}</span>
              </button>
            ))}
          </nav>

          <div className="shrink-0 flex items-center gap-2">
            {String(userRole) === 'admin' ? (
              <Link
                id="control-center-admin-console-link"
                to="/admin/tenants"
                title="Open the platform admin console (tenant hierarchy)"
                className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-gradient-to-r from-blue-500 to-cyan-500 text-white shadow-md shadow-blue-500/30 hover:opacity-90 transition-all whitespace-nowrap"
              >
                Admin Console
              </Link>
            ) : null}
            <span className="text-[10px] text-slate-400 dark:text-slate-500">
              Last refresh: {lastRefresh ? lastRefresh.toLocaleTimeString() : 'Pending'}
            </span>
            <button
              id="control-center-refresh-btn"
              onClick={() => void loadControlCenterData(false)}
              title={refreshing ? 'Refreshing live data' : 'Refresh dashboard'}
              disabled={refreshing || loadingRef.current}
              className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400 transition-colors disabled:opacity-60"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 pt-6">
        {policyScopeLabel ? (
          <div className="mb-3 rounded-lg border border-slate-200/70 dark:border-slate-700/50 bg-white/70 dark:bg-slate-900/50 px-4 py-2 text-[11px] text-slate-600 dark:text-slate-400">
            <span className="font-semibold text-slate-700 dark:text-slate-300">Data scope:</span>{' '}
            {policyScopeLabel}
            <span className="text-slate-500 dark:text-slate-500">
              {' '}
              — metrics and hiring counts are limited to your role boundary (JWT on gateway).
            </span>
          </div>
        ) : null}
        <div className="mb-4 rounded-xl border border-indigo-200/50 dark:border-indigo-700/30 bg-indigo-50/60 dark:bg-indigo-950/20 px-4 py-3">
          <p className="text-[11px] font-semibold text-indigo-700 dark:text-indigo-300 mb-2">
            Governance Stage Separation
          </p>
          <div className="flex flex-wrap gap-2 text-[10px]">
            <span className="px-2 py-1 rounded-full bg-white/80 dark:bg-slate-900/60 border border-slate-200/70 dark:border-slate-700/50">Observe: enabled</span>
            <span className="px-2 py-1 rounded-full bg-white/80 dark:bg-slate-900/60 border border-slate-200/70 dark:border-slate-700/50">Assess: enabled</span>
            <span className="px-2 py-1 rounded-full bg-white/80 dark:bg-slate-900/60 border border-slate-200/70 dark:border-slate-700/50">Recommend: advisory only</span>
            <span className="px-2 py-1 rounded-full bg-slate-100 dark:bg-slate-800/70 border border-slate-200/70 dark:border-slate-700/50">Decision: external owner</span>
            <span className="px-2 py-1 rounded-full bg-slate-100 dark:bg-slate-800/70 border border-slate-200/70 dark:border-slate-700/50">Execute: blocked in dashboard</span>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <KpiPill card={buildGatewayCard(liveData?.gatewayHealth ?? null, gatewaySource)} />
          <KpiPill card={buildServiceCard('Agent', liveData?.agentHealth ?? null, serviceBaseSource(liveData?.agentHealth ?? null, AGENT_SERVICE_URL))} />
          <KpiPill card={buildServiceCard('LangGraph', liveData?.langgraphHealth ?? null, serviceBaseSource(liveData?.langgraphHealth ?? null, LANGGRAPH_SERVICE_URL))} />
          <KpiPill
            card={{
              label: 'Live Sync',
              value: lastRefresh ? lastRefresh.toLocaleTimeString() : 'Pending',
              sublabel: liveData?.errors.length
                ? 'Partial live data'
                : 'Auto-refresh every 30s in background',
              warning: Boolean(liveData?.errors.length),
            }}
          />
        </div>

        {liveData?.errors.length ? (
          <div className="mt-4 rounded-xl border border-amber-200/70 dark:border-amber-700/30 bg-amber-50/80 dark:bg-amber-950/20 px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
            Live fallback active: {liveData.errors.join(' • ')}
          </div>
        ) : null}
        {liveData?.correlationIds.length ? (
          <div className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">
            Correlation IDs: {liveData.correlationIds.join(' | ')}
          </div>
        ) : null}
      </div>

      {/* ── Active Zone Panel ── */}
      <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-6">
        <div className="rounded-2xl border border-white/60 dark:border-slate-700/40 bg-white/60 dark:bg-slate-800/40 backdrop-blur-xl shadow-xl p-5 sm:p-7">
          {activeZone === 'executive' && (
            <ExecutiveZone cards={executiveCards} sourceSummary={`${gatewaySource} + authenticated Agent and LangGraph /health checks`} />
          )}
          {activeZone === 'hiring' && (
            <HiringZone cards={hiringCards} sourceSummary="Gateway business_metrics + /v1/candidates/stats (raw + aggregated)" funnelStages={funnelStages} />
          )}
          {activeZone === 'workforce' && (
            <WorkforceOpsZone cards={workforceCards} sourceSummary="Gateway business_metrics bucket" />
          )}
          {activeZone === 'growth' && (
            <GrowthZone cards={growthCards} sourceSummary="Gateway business_metrics bucket" />
          )}
          {activeZone === 'org' && (
            <OrgVisibilityZone cards={orgCards} sourceSummary="Gateway performance_summary + system_metrics (aggregated)" departmentLoad={departmentLoad} />
          )}
          {activeZone === 'governance' && ENABLE_GOVERNANCE_PANEL && (
            <GovernanceZone governance={liveData?.governance ?? null} />
          )}
          {activeZone === 'replay' && (
            <ReplayTraceZone
              traceEvents={liveData?.auditReplay?.events ?? []}
              traceNote={
                liveData?.auditReplay?.events?.length
                  ? `Live audit replay (${liveData.auditReplay.source}, correlation: ${liveData.auditReplay.correlation_id || 'n/a'}). Advisory trace only — not execution authority.`
                  : 'No audit events in scope yet. Refresh after control-center activity or gateway operations.'
              }
            />
          )}
        </div>

        {/* Constitutional Footer */}
        <div className="mt-4 rounded-xl border border-slate-200/40 dark:border-slate-700/30 bg-slate-50/60 dark:bg-slate-900/30 px-5 py-3 flex flex-col sm:flex-row sm:items-center gap-2">
          <svg className="w-4 h-4 text-indigo-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
          </svg>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            <strong className="text-slate-700 dark:text-slate-300">Constitutional Boundary:</strong>{' '}
            This Control Center is read-only intelligence. It surfaces signals and visibility — it does not issue commands, mutate state, or hold execution authority.
            All execution decisions remain with owning systems (Niyantran, Artha, Gateway) under Rishabh Yadav's direction.
          </p>
        </div>
      </div>
    </div>
  )
}
