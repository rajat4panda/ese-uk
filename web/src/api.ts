// Thin client for /api/v1. Errors follow LLD 15: {error: {code, message, details, request_id}}.

export type Role = "applicant" | "admissions" | "compliance" | "finance" | "senior" | "admin";

export interface User {
  id: number;
  name: string;
  email: string;
  role: Role;
  email_verified: boolean;
}

export interface ErrorDetail {
  field: string | null;
  code: string;
  message?: string;
  step?: number | null;
}

export class ApiError extends Error {
  status: number;
  code: string;
  details: ErrorDetail[];
  requestId: string | null;
  constructor(status: number, code: string, message: string, details: ErrorDetail[], requestId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

export interface DocumentRow {
  id: number;
  category: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  scan_status: string;
  expiry_date: string | null;
  verified_at: string | null;
  verified_by_name: string | null;
  superseded_by: number | null;
  uploaded_at: string;
}

export type AppData = Record<string, unknown>;

export interface Application {
  id: number;
  ref: string;
  status: string;
  applicant_id: number;
  programme_id: number | null;
  intake_id: number | null;
  agent_id: number | null;
  data: AppData;
  created_at: string;
  updated_at: string;
  editable: boolean;
  documents: DocumentRow[];
  outstanding_documents: string[];
}

export interface Summary {
  id: number;
  ref: string;
  status: string;
  full_name: string | null;
  email: string | null;
  programme: string | null;
  intake: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditRow {
  id: number;
  at: string;
  user_name: string;
  user_role: string;
  action: string;
  detail: string;
}

let token: string | null = sessionStorage.getItem("ese.token");
let onUnauthenticated: ((err: ApiError) => void) | null = null;

export function setToken(value: string | null) {
  token = value;
  if (value) sessionStorage.setItem("ese.token", value);
  else sessionStorage.removeItem("ese.token");
}

export function getToken() {
  return token;
}

export function onSessionEnded(handler: (err: ApiError) => void) {
  onUnauthenticated = handler;
}

async function parse(res: Response): Promise<never> {
  let body: { error?: { code?: string; message?: string; details?: ErrorDetail[]; request_id?: string } } = {};
  try {
    body = await res.json();
  } catch {
    /* non-JSON error */
  }
  const e = body.error ?? {};
  throw new ApiError(
    res.status,
    e.code ?? "ERR_UNKNOWN",
    e.message ?? `Request failed (${res.status})`,
    e.details ?? [],
    e.request_id ?? res.headers.get("x-request-id"),
  );
}

export async function request<T>(
  method: string,
  path: string,
  options: { json?: unknown; form?: FormData; headers?: Record<string, string>; raw?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  const res = await fetch(`/api/v1${path}`, { method, headers, body });
  if (!res.ok) {
    try {
      await parse(res);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401 && token && !path.startsWith("/auth/")) {
        onUnauthenticated?.(err);
      }
      throw err;
    }
  }
  if (options.raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  const etag = res.headers.get("etag");
  const data = await res.json();
  if (etag && data && typeof data === "object") (data as Record<string, unknown>).__etag = etag;
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, json?: unknown) => request<T>("POST", path, { json: json ?? {} }),
  put: <T>(path: string, json: unknown, headers?: Record<string, string>) =>
    request<T>("PUT", path, { json, headers }),
  patch: <T>(path: string, json: unknown) => request<T>("PATCH", path, { json }),
  upload: <T>(path: string, form: FormData) => request<T>("POST", path, { form }),
};

export async function downloadDocument(doc: { id: number; filename: string }) {
  const res = await request<Response>("GET", `/documents/${doc.id}/download`, { raw: true });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = doc.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export const STATUS_LABELS: Record<string, string> = {
  draft: "Draft — not yet submitted",
  submitted: "Submitted — awaiting academic review",
  academic_review: "Under academic review",
  offer_pending: "Academic approved — offer to build",
  info_requested: "Further information requested",
  offer_issued: "Offer issued",
  offer_accepted: "Offer accepted",
  offer_declined: "Offer declined",
  compliance_review: "Pre-CAS checks in progress",
  precas_ready: "Ready for CAS approval",
  cas_assigned: "CAS assigned",
  rejected: "Unsuccessful",
  withdrawn: "Withdrawn",
};

export const CATEGORY_LABELS: Record<string, string> = {
  passport: "Passport (photo page)",
  qualification: "Academic qualifications",
  english: "English language evidence",
  statement: "Personal statement",
  financial: "Financial evidence",
  other: "Other supporting document",
  enrolment_evidence: "Enrolment evidence",
  report_evidence: "Reporting evidence",
  interview_notes: "Interview notes",
};

export const ROLE_LABELS: Record<Role, string> = {
  applicant: "Applicant",
  admissions: "Admissions",
  compliance: "Compliance",
  finance: "Finance",
  senior: "Senior / Authorising Officer",
  admin: "Administrator",
};

export function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Europe/London",
  });
}

export function formatDate(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString("en-GB", {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

export function formatBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// --- Sprint 2: workflow types ------------------------------------------------------------------
export interface ChecklistItem {
  item_key: string;
  label: string;
  mandatory: boolean;
  satisfied: boolean | null;
}

export interface Assessment {
  english_evidence?: string | null;
  english_standard?: string | null;
  interview_date?: string | null;
  interviewer?: string | null;
  interview_outcome?: string | null;
  interview_notes?: string | null;
  interview_notes_documents?: number;
  id: number | null;
  checklist: ChecklistItem[];
  comments: string;
  outcome: string | null;
  risk_rating: string | null;
  atas_required: boolean | null;
  atas_reference: string | null;
  completed_by: string | null;
  completed_by_id: number | null;
  completed_at: string | null;
  second_line_by: string | null;
  second_line_at: string | null;
  second_line_outcome: string | null;
  outstanding_mandatory: string[];
}

export interface OfferCondition {
  id: number;
  description: string;
  status: string;
  evidence_document_id: number | null;
  verified_at: string | null;
}

export interface Offer {
  id: number;
  offer_type: string;
  conditions: OfferCondition[];
  expiry_date: string | null;
  issued_at: string | null;
  issue_method: string | null;
  response: string | null;
  response_at: string | null;
  override_reason: string | null;
  has_letter: boolean;
}

export interface FeePosition {
  agreed_fee: string | null;
  minimum_deposit: string | null;
  paid: string;
  scholarship: string;
  refunded: string;
  outstanding: string | null;
  deposit_met: boolean;
  currency: string;
}

export interface PaymentRow {
  id: number;
  type: string;
  amount: string;
  payment_date: string;
  method: string | null;
  reference: string | null;
  reason: string | null;
  recorded_at: string;
}

export interface CasRecord {
  id: number;
  cas_number: string;
  course_title: string;
  start_date: string;
  end_date: string;
  tuition_fee: string;
  amount_paid: string;
  level1_user: string;
  authorised_by: string | null;
  status: string;
  assigned_at: string;
  allocation_period: string | null;
  application_ref?: string | null;
  applicant_name?: string | null;
}

export interface AllocationPosition {
  period_label: string | null;
  period_start: string | null;
  period_end: string | null;
  allocated: number;
  assigned: number;
  remaining: number;
}

export interface StudentRecord {
  id: number;
  enrolment_status: string;
  enrolment_status_reason: string | null;
  enrolment_date: string | null;
}

export interface ApplicationRecord {
  id: number;
  ref: string;
  status: string;
  applicant_id: number;
  applicant_name: string | null;
  programme: string | null;
  intake: string | null;
  data: AppData;
  created_at: string;
  updated_at: string;
  editable: boolean;
  documents: DocumentRow[];
  outstanding_documents: string[];
  offer: Offer | null;
  cas: CasRecord | null;
  student_record: StudentRecord | null;
  academic?: Assessment;
  compliance?: Assessment;
  fee_position?: FeePosition;
  payments?: PaymentRow[];
}

export interface PreCasSummary {
  academic: { complete: boolean; outcome: string | null; completed_by: string | null; completed_at: string | null };
  compliance: {
    complete: boolean; risk_rating: string | null; atas_required: boolean | null;
    completed_by: string | null; completed_at: string | null; second_line_at: string | null;
  };
  documents: { outstanding: string[]; complete: boolean };
  conditions: { outstanding: string[]; overridden: boolean };
  fees: { agreed_fee: string | null; minimum_deposit: string | null; paid: string; outstanding: string | null; deposit_met: boolean };
  course: { programme: string | null; intake: string | null; start_date: string | null };
  assignment: { can_assign: boolean; reasons: string[] };
  ready_for_cas: boolean;
  blockers: string[];
}

export const RISK_LABELS: Record<string, string> = { low: "Low", medium: "Medium", high: "High" };

/** What the caller's role can do next, given the application's state (LLD 14.2). */
export function nextActions(status: string, role: Role): { label: string; to: string }[] {
  const staffAdmissions = role === "admissions" || role === "senior" || role === "admin";
  const staffCompliance = role === "compliance" || role === "senior" || role === "admin";
  const senior = role === "senior" || role === "admin";
  const out: { label: string; to: string }[] = [];
  if (staffAdmissions && ["submitted", "academic_review", "offer_pending"].includes(status)) out.push({ label: "Academic assessment", to: "academic" });
  if (staffAdmissions && status === "offer_pending") out.push({ label: "Build offer", to: "offer" });
  if (staffAdmissions && ["offer_issued", "offer_accepted"].includes(status)) out.push({ label: "Offer", to: "offer" });
  if (staffCompliance && ["offer_accepted", "compliance_review"].includes(status)) out.push({ label: "Compliance review", to: "compliance" });
  if (staffCompliance && ["compliance_review", "precas_ready", "cas_assigned"].includes(status)) out.push({ label: "Pre-CAS summary", to: "precas" });
  if (senior && status === "precas_ready") out.push({ label: "Assign CAS", to: "cas" });
  if (role !== "applicant" && status !== "draft") out.push({ label: "Fees", to: "fees" });
  return out;
}

/** Opens the generated offer letter in a new tab (served inline, no-store). */
export async function openOfferLetter(offerId: number) {
  const res = await request<Response>("GET", `/offers/${offerId}/letter`, { raw: true });
  const url = URL.createObjectURL(await res.blob());
  window.open(url, "_blank", "noopener");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// --- Sprint 3: post-CAS compliance --------------------------------------------------------------
export interface AttendanceRow {
  id: number;
  session_date: string;
  module: string;
  status: string;
  absence_reason: string | null;
  recorded_by: string | null;
  recorded_at: string;
}

export interface AttendanceSummary {
  scheduled: number;
  attended: number;
  present: number;
  authorised_absence: number;
  absent: number;
  percentage: number | null;
  threshold: number;
  below_threshold: boolean;
}

export interface ReportableEvent {
  id: number;
  student_id: number;
  application_id: number | null;
  application_ref: string | null;
  student_name: string | null;
  event_type: string;
  event_date: string;
  deadline_date: string;
  days_remaining: number;
  overdue: boolean;
  source: string;
  detail: string;
  status: string;
  reported_by: string | null;
  reported_date: string | null;
  sms_reference: string | null;
  not_required_reason: string | null;
  escalated_at: string | null;
  created_at: string;
}

export interface EscalationRow {
  id: number;
  step: string;
  notes: string;
  raised_by: string | null;
  raised_at: string;
}

export interface VisaOutcomeRow {
  id: number;
  outcome: string;
  decision_date: string;
  refusal_reason_category: string | null;
  recorded_by: string | null;
  recorded_at: string;
}

export interface StudentRecordFull {
  id: number;
  application_id: number;
  application_ref: string;
  student_name: string | null;
  programme: string | null;
  intake: string | null;
  cas_number: string | null;
  cas_status: string | null;
  course_start: string | null;
  course_end: string | null;
  enrolment_status: string;
  enrolment_status_reason: string | null;
  enrolment_date: string | null;
  permission_expiry: string | null;
  completion_outcome: string | null;
  completion_date: string | null;
  last_status_check: string | null;
  latest_status_check_outcome: string | null;
  current_address: Address;
  contact_phone: string | null;
  contact_email: string | null;
  enrolment_evidence: { id: number; filename: string } | null;
  cas_withdrawal_type: string | null;
  cas_withdrawal_date: string | null;
  cas_withdrawal_reason: string | null;
  visa_outcome: VisaOutcomeRow | null;
  attendance: AttendanceSummary;
  attendance_records: AttendanceRow[];
  events: ReportableEvent[];
  escalations: EscalationRow[];
}

export interface Address {
  line1?: string | null; line2?: string | null; city?: string | null; region?: string | null;
  postcode?: string | null; country?: string | null;
}

export interface StatusCheckRow {
  id: number; check_date: string; method: string; method_label: string; reference: string | null;
  permission_type: string | null; permission_expiry: string | null; outcome: string; outcome_label: string;
  notes: string | null; evidence: { id: number; filename: string } | null; checked_by: string | null; created_at: string;
}

export interface ContactChangeRow {
  id: number; effective_from: string; address: Address; phone: string | null; email: string | null;
  reason: string | null; recorded_by: string | null; recorded_at: string;
}

export interface StudentSummaryRow {
  id: number;
  application_ref: string;
  student_name: string | null;
  programme: string | null;
  enrolment_status: string;
  course_start: string | null;
  attendance_percentage: number | null;
  open_events: number;
  overdue_events: number;
}

export interface BcaMetric {
  key: string;
  label: string;
  numerator: number;
  denominator: number;
  percentage: number | null;
  threshold: number;
  direction: string;
  rag: string;
  small_sample: boolean;
  note: string;
}

export interface BcaMetrics {
  period_start: string;
  period_end: string;
  metrics: BcaMetric[];
  overall_rag: string;
}

export interface ExportJobRow {
  id: string;
  job_type: string;
  status: string;
  subject_ref: string | null;
  result_filename: string | null;
  expires_at: string | null;
  error_detail: string | null;
  created_at: string;
  finished_at: string | null;
}

export const ENROLMENT_LABELS: Record<string, string> = {
  awaiting_arrival: "Awaiting arrival",
  enrolled: "Enrolled",
  deferred: "Deferred",
  interrupted: "Interrupted",
  withdrawn: "Withdrawn",
  completed: "Completed",
  non_enrolled: "Not enrolled",
};

export const EVENT_LABELS: Record<string, string> = {
  non_enrolment: "Non-enrolment",
  withdrawal: "Withdrawal",
  deferral: "Deferral",
  course_change: "Course change",
  interruption: "Interruption",
  curtailment: "Curtailment",
  change_of_circumstances: "Change of circumstances",
  cas_withdrawal: "CAS withdrawal",
  sponsor_details_change: "Sponsor details change",
};

export const VISA_LABELS: Record<string, string> = {
  granted: "Granted",
  refused: "Refused",
  withdrawn: "Withdrawn",
  not_used: "Not used",
};

export const ATTENDANCE_LABELS: Record<string, string> = {
  present: "Present",
  absent: "Absent",
  authorised_absence: "Authorised absence",
};

/** Downloads a finished export (evidence pack) as a file. */
export async function downloadExport(job: ExportJobRow) {
  const res = await request<Response>("GET", `/jobs/${job.id}/download`, { raw: true });
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = job.result_filename ?? "evidence-pack.zip";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** FR-55: the CAS register as a CSV file, honouring whatever search is applied. */
export async function downloadCasRegister(q: string) {
  const res = await request<Response>("GET", `/cas/export${q ? `?q=${encodeURIComponent(q)}` : ""}`, { raw: true });
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = `cas-register-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// --- Agents (FR-102–FR-107, prototype screen 11) ------------------------------------------------
export interface AgentRow {
  id: number;
  name: string;
  country: string;
  country_code: string;
  status: AgentStatus;
  status_reason: string | null;
  contact_name: string | null;
  contact_email: string | null;
  applications: number;
  offers: number;
  cas: number;
  enrolled: number;
  visa_decisions: number;
  refused: number;
  refusal_rate_pct: number | null;
  refusal_rate_threshold_pct: number | null;
  refusal_rate_over_threshold: boolean;
  due_diligence_date: string | null;
  next_review_date: string | null;
}

export type AgentStatus = "active" | "under_review" | "suspended" | "deactivated";
export type EvidenceKind = "agreement" | "due_diligence";

export interface AgentEvidence {
  id: number;
  filename: string;
  uploaded_at: string;
  uploaded_by: string | null;
  available: boolean;
}

/** One agent as the register holds it (BRD FR-90, FR-91, FR-94). */
export interface AgentDetail {
  id: number;
  name: string;
  country_code: string;
  country: string;
  contact_name: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  status: AgentStatus;
  status_reason: string | null;
  due_diligence_date: string | null;
  next_review_date: string | null;
  refusal_rate_threshold_pct: number | null;
  evidence: Record<EvidenceKind, AgentEvidence | null>;
  /** Every file uploaded against the agent, newest first. */
  evidence_history: { id: number; kind: EvidenceKind; filename: string; uploaded_at: string; uploaded_by: string | null;
    status: "current" | "superseded" | "quarantined" }[];
  created_at: string;
  updated_at: string;
}

/** Who may change the register. Admissions reads it and attributes applications to it. */
export const AGENT_EDITORS: Role[] = ["compliance", "senior", "admin"];

export async function downloadAgentEvidence(agentId: number, kind: EvidenceKind, filename: string) {
  await saveAs(`/agents/${agentId}/evidence/${kind}`, filename);
}

/** A specific file held against an agent, including a superseded one. */
export async function downloadAgentDocument(agentId: number, documentId: number, filename: string) {
  await saveAs(`/agents/${agentId}/documents/${documentId}`, filename);
}

async function saveAs(path: string, filename: string) {
  const res = await request<Response>("GET", path, { raw: true });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** One reason a student is flagged (BRD FR-70, FR-71), classified on the server. */
export interface ComplianceIssue {
  kind: "event_overdue" | "event_open" | "attendance" | "visa_expired" | "visa_expiring" | "status_check_failed"
    | "status_check_missing" | "enrolment_evidence_missing" | "address_update";
  severity: "critical" | "attention";
  label: string;
  since: string | null;
  detail: string;
}
export interface ComplianceOverview {
  as_of: string; total: number; compliant: number; attention: number; critical: number;
  issue_counts: Partial<Record<ComplianceIssue["kind"], number>>;
  students: { student_id: number; application_ref: string; student_name: string | null; programme: string | null;
    enrolment_status: string; severity: "critical" | "attention"; issues: ComplianceIssue[] }[];
}

/** ISO alpha-2 → country name, fetched once per session. Falls back to the code on failure. */
let countryCache: Record<string, string> | null = null;
export async function countryNames(): Promise<Record<string, string>> {
  if (countryCache) return countryCache;
  try {
    const rows = await api.get<{ code: string; name: string }[]>("/reference/countries");
    countryCache = Object.fromEntries(rows.map((c) => [c.code, c.name]));
  } catch {
    countryCache = {};
  }
  return countryCache;
}
