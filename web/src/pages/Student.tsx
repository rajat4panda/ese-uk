import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  ATTENDANCE_LABELS, CATEGORY_LABELS, ENROLMENT_LABELS, EVENT_LABELS, VISA_LABELS, api, ApiError, downloadDocument,
  countryNames, downloadExport, formatDate, formatDateTime, type Address, type ContactChangeRow, type DocumentRow,
  type ExportJobRow, type StatusCheckRow, type StudentRecordFull, type StudentSummaryRow,
} from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, Layout, RadioGroup, SelectField, Spinner, StatusBadge, TextArea, TextField } from "../components/ui";
import { initials } from "../nav";
import { ActionErrors, Confirm, LoadFailed, useAction } from "../components/workflow";

const STATUS_CHANGES = [
  { value: "deferral", label: "Deferral" },
  { value: "interruption", label: "Interruption of study" },
  { value: "withdrawal", label: "Withdrawal" },
  { value: "course_change", label: "Change of course" },
  { value: "curtailment", label: "Curtailment" },
  { value: "change_of_circumstances", label: "Change of circumstances" },
];

const ESCALATION_STEPS = [
  { value: "email", label: "Email to the student" },
  { value: "meeting", label: "Meeting held" },
  { value: "formal_warning", label: "Formal warning" },
  { value: "final_warning", label: "Final warning" },
  { value: "reported", label: "Reported" },
];

/** Today in UK time — the day the server and UKVI count from, whatever the browser's clock zone. */
function today() {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" });
}

/** The deadline clock, shown the way a compliance officer reads it: days, then the date. */
export function DeadlineChip({ event }: { event: { deadline_date: string; days_remaining: number; status: string } }) {
  if (event.status !== "pending") {
    return <span className="tag tag-clean">Closed</span>;
  }
  const days = event.days_remaining;
  const tone = days < 0 ? "overdue" : days <= 3 ? "pending" : "clean";
  const text = days < 0
    ? `${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"} overdue`
    : days === 0 ? "Due today" : `${days} day${days === 1 ? "" : "s"} left`;
  return (
    <span className={`tag tag-clock tag-${tone}`} title={`Deadline ${formatDate(event.deadline_date)}`}>{text}</span>
  );
}

// ---------------------------------------------------------------------------------------------
// The student list
// ---------------------------------------------------------------------------------------------
export function Students() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [data, setData] = useState<{ items: StudentSummaryRow[]; total: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const status = params.get("status") ?? "";

  useEffect(() => {
    const qs = new URLSearchParams();
    if (params.get("q")) qs.set("q", params.get("q")!);
    if (status) qs.set("status", status);
    api.get<typeof data>(`/students?${qs}`).then((d) => { setData(d); setFailed(false); }).catch(() => setFailed(true));
  }, [params, status]);

  function search(e: FormEvent) {
    e.preventDefault();
    const next = new URLSearchParams(params);
    if (q) next.set("q", q); else next.delete("q");
    setParams(next);
  }

  return (
    <Layout wide>
      <h1>Students</h1>
      <p className="hint">Every applicant with a CAS, from assignment through to completion.</p>
      <form className="filters" onSubmit={search} role="search">
        <TextField name="q" label="Search" hint="Name or application reference" value={q} onChange={setQ} width="m" />
        <SelectField
          name="status" label="Enrolment status" value={status} placeholder="All statuses"
          onChange={(v) => { const n = new URLSearchParams(params); if (v) n.set("status", v); else n.delete("status"); setParams(n); }}
          options={Object.entries(ENROLMENT_LABELS).map(([value, label]) => ({ value, label }))}
        />
        <button className="button" type="submit">Search</button>
      </form>
      {failed ? <LoadFailed what="The student list" /> : !data ? <Spinner /> : data.items.length === 0 ? (
        <p className="empty">No student records yet. One is created automatically when a CAS is assigned.</p>
      ) : (
        <table className="table">
          <caption className="visually-hidden">Student records</caption>
          <thead>
            <tr>
              <th scope="col">Student</th><th scope="col">Programme</th><th scope="col">Course start</th>
              <th scope="col">Status</th><th scope="col">Attendance</th><th scope="col">Open events</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((s) => (
              <tr key={s.id}>
                <td>
                  <Link to={`/staff/students/${s.id}`}>{s.student_name ?? "—"}</Link>
                  <span className="hint block">{s.application_ref}</span>
                </td>
                <td>{s.programme ?? "—"}</td>
                <td className="nowrap">{formatDate(s.course_start)}</td>
                <td><StatusBadge status={s.enrolment_status} label={ENROLMENT_LABELS[s.enrolment_status] ?? s.enrolment_status} /></td>
                <td>{s.attendance_percentage === null ? "—" : `${s.attendance_percentage}%`}</td>
                <td>
                  {s.open_events === 0 ? "None" : `${s.open_events} open`}
                  {s.overdue_events > 0 && <span className="tag tag-overdue">{s.overdue_events} overdue</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Layout>
  );
}

// ---------------------------------------------------------------------------------------------
// One student record
// ---------------------------------------------------------------------------------------------
/** Why a record closed, in words. Status changes store "type: reason"; closures store a code. */
const CLOSURE_REASONS: Record<string, string> = {
  cas_withdrawn: "The CAS was withdrawn before a visa decision.",
  cas_cancelled: "The CAS was cancelled before a visa decision.",
  visa_refused: "The visa application was refused.",
  visa_withdrawn: "The visa application was withdrawn.",
  cas_not_used: "The CAS was not used.",
  non_enrolment: "The student did not enrol.",
};

export function Student() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const [record, setRecord] = useState<StudentRecordFull | null>(null);
  const [failed, setFailed] = useState(false);
  const senior = user?.role === "senior" || user?.role === "admin";
  const compliance = user?.role === "compliance" || senior;

  const reload = useCallback(async () => {
    try {
      setRecord(await api.get<StudentRecordFull>(`/students/${id}`));
      setFailed(false);
    } catch (e) {
      if (e instanceof ApiError) setFailed(true);
    }
  }, [id]);

  useEffect(() => { void reload(); }, [reload]);

  if (failed) return <Layout><LoadFailed what="The student record" /></Layout>;
  if (!record) return <Layout><Spinner /></Layout>;

  const closed = ["non_enrolled", "withdrawn", "completed"].includes(record.enrolment_status);
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "right-to-study", label: "Right to study" },
    { key: "contact", label: "Contact details" },
    { key: "attendance", label: "Attendance" },
    { key: "engagement", label: "Engagement" },
    { key: "reporting", label: "Reporting" },
    { key: "notes", label: "Notes" },
    { key: "documents", label: "Documents" },
    ...(senior ? [{ key: "evidence", label: "Evidence pack" }] : []),
  ];
  const tab = tabs.some((t) => t.key === params.get("tab")) ? params.get("tab")! : "overview";
  const open = record.events.filter((e) => e.status === "pending").length;

  return (
    <Layout>
      {/* Prototype screen 12: the record identifies itself once, at the top, and the work is
          divided into tabs rather than stacked down one page. */}
      <p><Link to="/staff/students">← All students</Link></p>
      <div className="record-head">
        <span className="avatar lg" aria-hidden="true">{initials(record.student_name ?? record.application_ref)}</span>
        <div style={{ flex: 1, minWidth: "220px" }}>
          <h1>{record.student_name ?? record.application_ref}</h1>
          <p className="sub">
            <Link to={`/staff/applications/${record.application_id}`}>{record.application_ref}</Link>
            {" · "}{record.programme ?? "—"}{" · CAS "}{record.cas_number ?? "—"}
          </p>
        </div>
        <StatusBadge status={record.enrolment_status} label={ENROLMENT_LABELS[record.enrolment_status] ?? record.enrolment_status} />
      </div>

      {record.enrolment_status_reason && (
        <Alert kind={closed ? "warning" : "info"}>
          <p>{CLOSURE_REASONS[record.enrolment_status_reason] ?? record.enrolment_status_reason.replace(/_/g, " ")}</p>
        </Alert>
      )}

      <nav className="tabs" aria-label="Student record sections">
        {tabs.map((t) => (
          <a
            key={t.key}
            href={`?tab=${t.key}`}
            aria-current={tab === t.key ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setParams({ tab: t.key }); }}
          >
            {t.label}
            {t.key === "reporting" && open > 0 && <span className="tab-count">{open}</span>}
          </a>
        ))}
      </nav>

      {tab === "overview" && (
        <>
          <section className="card">
            <h2 className="h3">Sponsorship</h2>
            <dl className="kv">
              <div><dt>CAS number</dt><dd>{record.cas_number ?? "—"}</dd></div>
              <div><dt>CAS status</dt><dd>
                {record.cas_withdrawal_date
                  ? `${record.cas_withdrawal_type === "cancelled" ? "Cancelled" : "Withdrawn"} with effect from ${formatDate(record.cas_withdrawal_date)}`
                  : record.cas_status ?? "—"}
                {record.cas_withdrawal_reason && <span className="hint block">{record.cas_withdrawal_reason}</span>}
              </dd></div>
              <div><dt>Course dates</dt><dd>{formatDate(record.course_start)} to {formatDate(record.course_end)}</dd></div>
              <div><dt>Visa outcome</dt><dd>
                {record.visa_outcome
                  ? `${VISA_LABELS[record.visa_outcome.outcome] ?? record.visa_outcome.outcome} on ${formatDate(record.visa_outcome.decision_date)}`
                    + (record.visa_outcome.refusal_reason_category ? ` (${record.visa_outcome.refusal_reason_category})` : "")
                  : "No decision recorded"}
              </dd></div>
              <div><dt>Permission expires</dt><dd>{formatDate(record.permission_expiry)}</dd></div>
              <div><dt>Enrolled</dt><dd>{formatDate(record.enrolment_date)}</dd></div>
              <div><dt>Enrolment evidence</dt><dd>
                {record.enrolment_evidence
                  ? <button type="button" className="link-button" onClick={() => void downloadDocument(record.enrolment_evidence!)}>{record.enrolment_evidence.filename}</button>
                  : record.enrolment_date ? <span className="warning-text">None on file</span> : "—"}
              </dd></div>
              <div><dt>Completion</dt><dd>{record.completion_outcome
                ? `${record.completion_outcome.replace(/_/g, " ")} on ${formatDate(record.completion_date)}` : "—"}</dd></div>
              <div><dt>Right to study last checked</dt><dd>
                {record.last_status_check ? formatDate(record.last_status_check) : "Never"}
                {record.latest_status_check_outcome && record.latest_status_check_outcome !== "confirmed" && (
                  <span className="tag tag-overdue">{record.latest_status_check_outcome === "discrepancy" ? "Discrepancy" : "No valid permission"}</span>
                )}
              </dd></div>
            </dl>
          </section>
          {record.enrolment_status === "awaiting_arrival" && <EnrolPanel record={record} reload={reload} />}
          {record.enrolment_date && !closed && (
            <EnrolmentEvidencePanel key={record.enrolment_evidence?.id ?? 0} record={record} reload={reload} />
          )}
          {!record.visa_outcome && compliance && <VisaPanel record={record} reload={reload} />}
          {senior && record.cas_status === "assigned" && !record.visa_outcome && <CasWithdrawPanel record={record} reload={reload} />}
          {!closed && <StatusChangePanel record={record} reload={reload} />}
        </>
      )}

      {tab === "right-to-study" && <RightToStudyPanel record={record} reload={reload} canEdit={!closed} />}

      {tab === "contact" && <ContactPanel record={record} reload={reload} canEdit={!closed} />}

      {tab === "attendance" && (
        <>
          <section className="card">
            <h2 className="h3">Attendance</h2>
            {record.attendance.scheduled === 0 ? (
              <p className="empty">No sessions recorded yet.</p>
            ) : (
              <div className="row" style={{ gap: "26px", alignItems: "center" }}>
                <AttendanceDonut summary={record.attendance} />
                <dl className="kv compact">
                  <div><dt>Sessions scheduled</dt><dd>{record.attendance.scheduled}</dd></div>
                  <div><dt>Present</dt><dd>{record.attendance.present}</dd></div>
                  <div><dt>Authorised absence</dt><dd>{record.attendance.authorised_absence}</dd></div>
                  <div><dt>Absent</dt><dd>{record.attendance.absent}</dd></div>
                  <div><dt>Threshold</dt><dd>{record.attendance.threshold}%</dd></div>
                </dl>
              </div>
            )}
            {record.attendance.below_threshold && (
              <p className="warning-text">Attendance is below the {record.attendance.threshold}% threshold.</p>
            )}
            <p className="hint">Authorised absence counts towards engagement and is shown separately.</p>
          </section>

          {!closed && <AttendancePanel record={record} reload={reload} />}

          {record.attendance_records.length > 0 && (
            <section className="card">
              <h2 className="h3">Attendance history</h2>
              <div className="table-wrap">
                <table className="table">
                  <caption className="visually-hidden">Attendance records</caption>
                  <thead><tr><th scope="col">Session</th><th scope="col">Module</th><th scope="col">Status</th><th scope="col">Reason</th><th scope="col">Recorded</th></tr></thead>
                  <tbody>
                    {record.attendance_records.map((a) => (
                      <tr key={a.id}>
                        <td className="nowrap">{formatDate(a.session_date)}</td>
                        <td>{a.module}</td>
                        <td><span className={`tag tag-${a.status === "absent" ? "overdue" : a.status === "present" ? "clean" : "pending"}`}>{ATTENDANCE_LABELS[a.status] ?? a.status}</span></td>
                        <td>{a.absence_reason ?? "—"}</td>
                        <td className="nowrap">{a.recorded_by ?? "—"}<span className="hint block">{formatDateTime(a.recorded_at)}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}

      {tab === "engagement" && (
        <>
          {compliance && !closed && <EscalationPanel record={record} reload={reload} />}
          <section className="card">
            <h2 className="h3">Escalations</h2>
            {record.escalations.length === 0 ? (
              <p className="empty">No escalation has been raised on this record.</p>
            ) : (
              <ul className="doc-list">
                {record.escalations.map((e) => (
                  <li key={e.id}>
                    <span className="doc-cat">{ESCALATION_STEPS.find((s) => s.value === e.step)?.label ?? e.step}</span>
                    {e.notes}
                    <span className="hint block">{e.raised_by} · {formatDateTime(e.raised_at)}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="hint">
              The policy runs contact, then warning, then withdrawal, then a report to UKVI. Each step is
              recorded here in the order it was taken.
            </p>
          </section>
        </>
      )}

      {tab === "reporting" && <EventsPanel record={record} reload={reload} />}

      {tab === "notes" && <NotesPanel studentId={record.id} />}

      {tab === "documents" && <DocumentsPanel studentId={record.id} />}

      {tab === "evidence" && (
        <EvidencePackPanel applicationId={record.application_id} reference={record.application_ref} senior={senior} />
      )}
    </Layout>
  );
}

/** Attendance ring — prototype screen 12. */
function AttendanceDonut({ summary }: { summary: { percentage: number | null; below_threshold: boolean } }) {
  const pct = summary.percentage ?? 0;
  const stroke = summary.below_threshold ? "#b91c1c" : "#166534";
  return (
    <div className="donut">
      <svg viewBox="0 0 42 42" width="110" height="110" role="img" aria-label={`${pct} per cent attendance`}>
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="#e6ecf5" strokeWidth="5" />
        <circle cx="21" cy="21" r="15.9" fill="none" stroke={stroke} strokeWidth="5"
          strokeDasharray={`${pct} ${100 - pct}`} strokeDashoffset="25" strokeLinecap="round" />
      </svg>
      <div className="donut-centre" aria-hidden="true">
        <b>{pct}%</b>
        <span>Attendance</span>
      </div>
    </div>
  );
}

/** Upload one file to the student's own file and return its id. Throws ApiError on a refused upload. */
async function uploadToStudent(studentId: number, category: string, file: File): Promise<number> {
  const form = new FormData();
  form.append("category", category);
  form.append("file", file);
  const doc = await api.upload<DocumentRow>(`/students/${studentId}/documents`, form);
  return doc.id;
}

/** A file chosen for an action, uploaded once: if the action is then refused, a retry reuses the
 *  stored document instead of adding another copy to the student's file. */
function useStagedFile(studentId: number, category: string) {
  const [file, setFileState] = useState<File | null>(null);
  const staged = useRef<{ file: File; id: number } | null>(null);
  return {
    file,
    setFile: (f: File | null) => setFileState(f),
    async id(): Promise<number | null> {
      if (!file) return null;
      if (staged.current?.file !== file) staged.current = { file, id: await uploadToStudent(studentId, category, file) };
      return staged.current.id;
    },
    reset() { setFileState(null); staged.current = null; },
  };
}

/** The share code as the server will read it: letters and digits only, nine of them. */
function shareCodeOk(raw: string) {
  return raw.replace(/[^A-Za-z0-9]/g, "").length === 9;
}

const FILE_ACCEPT = ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png";

function EnrolPanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ enrolment_date: today(), permission_expiry: "" });
  const evidence = useStagedFile(record.id, "enrolment_evidence");
  const { summary, busy, run } = useAction();
  return (
    <section className="card">
      <h2 className="h3">Record enrolment</h2>
      <ActionErrors summary={summary} />
      <p className="hint">Enrolment opens attendance capture. Until it is recorded, the non-enrolment clock runs.</p>
      <DateField name="enrolment_date" label="Enrolment date" required value={form.enrolment_date}
        onChange={(v) => setForm({ ...form, enrolment_date: v })} />
      <DateField name="permission_expiry" label="Permission to stay expires" value={form.permission_expiry}
        hint="From the eVisa, visa vignette or BRP. Used for the expiry alert. A right-to-study check also sets it."
        onChange={(v) => setForm({ ...form, permission_expiry: v })} />
      <div className="field">
        <label htmlFor="enrolment-evidence" className="label">Evidence of enrolment <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
        <p className="hint">For example the signed registration form or the enrolment confirmation. It can also be added later.</p>
        <input id="enrolment-evidence" type="file" accept={FILE_ACCEPT} onChange={(e) => evidence.setFile(e.target.files?.[0] ?? null)} />
      </div>
      <div className="actions">
        <button type="button" className="button" disabled={busy || !form.enrolment_date}
          onClick={() => void run(async () => api.post(`/students/${record.id}/enrol`, {
            enrolment_date: form.enrolment_date, permission_expiry: form.permission_expiry || null,
            evidence_document_id: await evidence.id(),
          }), () => reload())}>
          {busy ? "Saving…" : "Record enrolment"}
        </button>
      </div>
    </section>
  );
}

/** FR-61: evidence of enrolment added after the enrolment, or replaced (the old file is kept and the
 *  change audited). */
function EnrolmentEvidencePanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const evidence = useStagedFile(record.id, "enrolment_evidence");
  const [open, setOpen] = useState(!record.enrolment_evidence);
  const { summary, busy, run } = useAction();
  const replacing = !!record.enrolment_evidence;
  if (!open) {
    return (
      <p><button type="button" className="link-button" onClick={() => setOpen(true)}>Replace the evidence of enrolment</button></p>
    );
  }
  return (
    <section className="card">
      <h2 className="h3">{replacing ? "Replace the evidence of enrolment" : "Add evidence of enrolment"}</h2>
      <ActionErrors summary={summary} />
      <p className="hint">
        {replacing
          ? `The current file, ${record.enrolment_evidence!.filename}, stays on the student's file; the replacement is recorded in the audit trail.`
          : `The enrolment on ${formatDate(record.enrolment_date)} has no evidence on file. A sponsor must be able to show how it confirmed the student enrolled.`}
      </p>
      <div className="field">
        <label htmlFor="late-enrolment-evidence" className="label">File <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
        <input id="late-enrolment-evidence" type="file" accept={FILE_ACCEPT} onChange={(e) => evidence.setFile(e.target.files?.[0] ?? null)} />
      </div>
      <div className="actions">
        <button type="button" className="button button-secondary" disabled={busy || !evidence.file}
          onClick={() => void run(async () => api.post(`/students/${record.id}/enrolment-evidence`, { document_id: await evidence.id() }),
            async () => { evidence.reset(); if (replacing) setOpen(false); await reload(); })}>
          {busy ? "Uploading and scanning…" : "Upload evidence"}
        </button>
        {replacing && <button type="button" className="button button-secondary" onClick={() => setOpen(false)}>Cancel</button>}
      </div>
    </section>
  );
}

/** FR-58: withdraw or cancel a CAS before a visa decision. The Authorising Officer's decision. */
function CasWithdrawPanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ withdrawal_type: "withdrawn", withdrawal_date: today(), reason: "" });
  const [confirming, setConfirming] = useState(false);
  const { summary, busy, run } = useAction();
  const tooShort = form.reason.trim().length < 10;
  const verb = form.withdrawal_type === "cancelled" ? "Cancel" : "Withdraw";
  return (
    <section className="card">
      <h2 className="h3">Withdraw or cancel the CAS</h2>
      <ActionErrors summary={summary} />
      <p className="hint">
        Before a visa decision only. The CAS must also be withdrawn or cancelled on the sponsor management system; this
        records it here, closes the student record and raises the event to report.
      </p>
      <RadioGroup name="withdrawal_type" label="What is happening to the CAS" required value={form.withdrawal_type}
        onChange={(v) => setForm({ ...form, withdrawal_type: v })} options={[
          { value: "withdrawn", label: "Withdrawn", hint: "The student is not coming, or ESE will no longer sponsor them." },
          { value: "cancelled", label: "Cancelled", hint: "Assigned in error, for example with the wrong details; usually reissued." },
        ]} />
      <DateField name="withdrawal_date" label="Date the withdrawal took effect" required value={form.withdrawal_date}
        hint="The reporting deadline is counted from this date. It cannot be before the CAS was assigned."
        onChange={(v) => setForm({ ...form, withdrawal_date: v })} />
      <TextArea name="withdrawal_reason" label="Reason for the withdrawal" required rows={3} maxLength={2000} value={form.reason}
        hint="Recorded on the CAS and in the audit trail."
        error={form.reason && tooShort ? "Give a reason of at least 10 characters." : undefined}
        onChange={(v) => setForm({ ...form, reason: v })} />
      <div className="actions">
        <button type="button" className="button button-warning" disabled={busy || tooShort || !form.withdrawal_date}
          onClick={() => setConfirming(true)}>
          {verb} CAS {record.cas_number}
        </button>
      </div>
      {confirming && (
        <Confirm
          title={`${verb} CAS ${record.cas_number}?`}
          consequence={`The CAS will be recorded as ${form.withdrawal_type} with effect from ${formatDate(form.withdrawal_date)}, ${record.student_name ?? record.application_ref}'s record will close, and a CAS withdrawal event will be raised for reporting. This cannot be undone.`}
          confirmLabel={`${verb} the CAS`} busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void run(() => api.post(`/applications/${record.application_id}/cas/withdraw`, form),
            async () => { setConfirming(false); await reload(); })}
        />
      )}
    </section>
  );
}

const CHECK_METHODS = [
  { value: "evisa_share_code", label: "Online check with a share code (eVisa)" },
  { value: "brp", label: "Biometric residence permit" },
  { value: "passport_vignette", label: "Passport and entry clearance vignette" },
  { value: "other", label: "Other document" },
];
const CHECK_OUTCOMES = [
  { value: "confirmed", label: "Right to study confirmed" },
  { value: "discrepancy", label: "Discrepancy found" },
  { value: "no_permission", label: "No valid permission" },
];

/** FR-68: each right-to-study check, what it was made with, and what it found. */
function RightToStudyPanel({ record, reload, canEdit }: { record: StudentRecordFull; reload: () => Promise<void>; canEdit: boolean }) {
  const blank = { check_date: today(), method: "evisa_share_code", reference: "", permission_type: "Student",
    permission_expiry: "", outcome: "confirmed", notes: "" };
  const [form, setForm] = useState(blank);
  const evidence = useStagedFile(record.id, "other");
  const [checks, setChecks] = useState<StatusCheckRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const { summary, busy, run } = useAction();
  const load = useCallback(async () => {
    try { setChecks(await api.get(`/students/${record.id}/status-checks`)); } catch { setFailed(true); }
  }, [record.id]);
  useEffect(() => { void load(); }, [load]);
  const confirmed = form.outcome === "confirmed";
  const shareCode = form.method === "evisa_share_code";

  return (
    <>
      {canEdit && (
        <section className="card">
          <h2 className="h3">Record a right-to-study check</h2>
          <ActionErrors summary={summary} />
          <p className="hint">
            Checks are kept in full and cannot be edited. The latest check sets the permission expiry the alerts run from;
            to correct a mistake, record the check again.
          </p>
          <DateField name="check_date" label="Date of the check" required value={form.check_date}
            onChange={(v) => setForm({ ...form, check_date: v })} />
          <SelectField name="method" label="How it was checked" required value={form.method}
            onChange={(v) => setForm({ ...form, method: v })} options={CHECK_METHODS} />
          <TextField name="reference" label={shareCode ? "Share code" : "Document number"} required={shareCode} width="m"
            hint={shareCode ? "9 characters, for example W4K 9P2 7XA." : "For example the BRP or vignette number."}
            value={form.reference} onChange={(v) => setForm({ ...form, reference: v })} />
          <RadioGroup name="outcome" label="Outcome" required value={form.outcome}
            onChange={(v) => setForm({ ...form, outcome: v })} options={CHECK_OUTCOMES} />
          <TextField name="permission_type" label="Permission" width="m" hint="For example Student, or Graduate."
            value={form.permission_type} onChange={(v) => setForm({ ...form, permission_type: v })} />
          <DateField name="permission_expiry" label="Permission expires" required={confirmed} value={form.permission_expiry}
            onChange={(v) => setForm({ ...form, permission_expiry: v })} />
          <TextArea name="check_notes" label={confirmed ? "Notes" : "What was found"} required={!confirmed} rows={2}
            maxLength={2000} value={form.notes} onChange={(v) => setForm({ ...form, notes: v })} />
          <div className="field">
            <label htmlFor="check-evidence" className="label">Evidence <span className="optional">(optional — PDF, JPG or PNG, up to 5MB)</span></label>
            <p className="hint">For example the downloaded result of the online check.</p>
            <input id="check-evidence" type="file" accept={FILE_ACCEPT} onChange={(e) => evidence.setFile(e.target.files?.[0] ?? null)} />
          </div>
          <div className="actions">
            <button type="button" className="button" disabled={busy || (shareCode && !form.reference.trim())
              || (confirmed && !form.permission_expiry) || (!confirmed && !form.notes.trim())}
              onClick={() => void run(async () => {
                if (shareCode && !shareCodeOk(form.reference)) {
                  throw new ApiError(422, "ERR_INVALID_FORMAT", "Enter the 9-character share code, for example W4K 9P2 7XA.",
                    [{ field: "reference", code: "ERR_INVALID_FORMAT" }], null);
                }
                return api.post(`/students/${record.id}/status-checks`, {
                  ...form, reference: form.reference || null, permission_type: form.permission_type || null,
                  permission_expiry: form.permission_expiry || null, notes: form.notes || null,
                  evidence_document_id: await evidence.id(),
                });
              }, async () => { setForm(blank); evidence.reset(); await load(); await reload(); })}>
              {busy ? "Saving…" : "Record check"}
            </button>
          </div>
        </section>
      )}
      <section className="card">
        <h2 className="h3">Check history</h2>
        {failed ? <LoadFailed what="The checks" /> : !checks ? <Spinner /> : checks.length === 0 ? (
          <p className="empty">No right-to-study check has been recorded.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Right-to-study checks, newest first</caption>
              <thead><tr><th scope="col">Date</th><th scope="col">Method</th><th scope="col">Outcome</th><th scope="col">Permission</th><th scope="col">Recorded</th></tr></thead>
              <tbody>
                {checks.map((c) => (
                  <tr key={c.id}>
                    <td className="nowrap">{formatDate(c.check_date)}</td>
                    <td>{c.method_label}{c.reference && <span className="hint block">{c.reference}</span>}
                      {c.evidence && <button type="button" className="link-button block" onClick={() => void downloadDocument(c.evidence!)}>{c.evidence.filename}</button>}
                    </td>
                    <td><span className={`tag tag-${c.outcome === "confirmed" ? "clean" : "overdue"}`}>{c.outcome_label}</span>
                      {c.notes && <span className="hint block">{c.notes}</span>}</td>
                    <td>{c.permission_type ?? "—"}{c.permission_expiry && <span className="hint block">to {formatDate(c.permission_expiry)}</span>}</td>
                    <td className="nowrap">{c.checked_by ?? "—"}<span className="hint block">{formatDateTime(c.created_at)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function addressLine(a: Address | null | undefined, countries: Record<string, string>): string {
  if (!a) return "—";
  return [a.line1, a.line2, a.city, a.region, a.postcode, a.country ? countries[a.country] ?? a.country : null]
    .filter(Boolean).join(", ") || "—";
}

/** FR-67: the current address and contact details, every change to them, and when each took effect. */
function ContactPanel({ record, reload, canEdit }: { record: StudentRecordFull; reload: () => Promise<void>; canEdit: boolean }) {
  const start = () => ({
    effective_from: today(), line1: "", line2: "", city: "", postcode: "", country: "GB",
    phone: record.contact_phone ?? "", email: record.contact_email ?? "", reason: "",
  });
  const [form, setForm] = useState(start);
  const [history, setHistory] = useState<ContactChangeRow[] | null>(null);
  const [countries, setCountries] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState(false);
  const { summary, busy, run } = useAction();
  const load = useCallback(async () => {
    try { setHistory(await api.get(`/students/${record.id}/contact`)); } catch { setFailed(true); }
  }, [record.id]);
  useEffect(() => { void load(); void countryNames().then(setCountries); }, [load]);
  // After a save the record reloads; take phone and email from it, so the next change starts from what
  // is now on file and cannot quietly put the old values back.
  useEffect(() => {
    setForm((f) => ({ ...f, phone: record.contact_phone ?? "", email: record.contact_email ?? "" }));
  }, [record.contact_phone, record.contact_email]);
  const ukAddress = record.current_address?.country === "GB" && !!record.current_address?.line1;
  const set = (k: keyof ReturnType<typeof start>) => (v: string) => setForm({ ...form, [k]: v });

  return (
    <>
      <section className="card">
        <h2 className="h3">Current contact details</h2>
        {!ukAddress && ["enrolled", "interrupted"].includes(record.enrolment_status) && (
          <Alert kind="warning"><p>No UK address is on record. Record the student's UK address below.</p></Alert>
        )}
        <dl className="kv">
          <div><dt>Address</dt><dd>{addressLine(record.current_address, countries)}</dd></div>
          <div><dt>Phone</dt><dd>{record.contact_phone ?? "—"}</dd></div>
          <div><dt>Email</dt><dd>{record.contact_email ?? "—"}</dd></div>
        </dl>
      </section>
      {canEdit && (
        <section className="card">
          <h2 className="h3">Record a change</h2>
          <ActionErrors summary={summary} />
          <p className="hint">Enter the full set of details from the date they changed. The earlier details stay in the history.</p>
          <DateField name="effective_from" label="Effective from" required value={form.effective_from} onChange={set("effective_from")} />
          <TextField name="line1" label="Address line 1" required value={form.line1} onChange={set("line1")} />
          <TextField name="line2" label="Address line 2" value={form.line2} onChange={set("line2")} />
          <TextField name="city" label="Town or city" required width="m" value={form.city} onChange={set("city")} />
          <TextField name="postcode" label="Postcode" required width="s" value={form.postcode} onChange={set("postcode")} />
          <SelectField name="country" label="Country" required value={form.country} onChange={set("country")}
            options={Object.entries(countries).sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label }))} />
          <TextField name="phone" label="Phone" width="m" hint="International format, for example +447700900123."
            value={form.phone} onChange={set("phone")} />
          <TextField name="email" label="Email" width="l" value={form.email} onChange={set("email")} />
          <TextArea name="contact_reason" label="Reason or source" rows={2} maxLength={1000}
            hint="For example: student emailed the new address on 3 October." value={form.reason} onChange={set("reason")} />
          <div className="actions">
            <button type="button" className="button" disabled={busy || !form.line1.trim() || !form.city.trim() || !form.postcode.trim()}
              onClick={() => void run(() => api.post(`/students/${record.id}/contact`, {
                effective_from: form.effective_from,
                address: { line1: form.line1, line2: form.line2 || null, city: form.city, postcode: form.postcode, country: form.country },
                phone: form.phone || null, email: form.email || null, reason: form.reason || null,
              }), async () => {
                setForm((f) => ({ ...f, effective_from: today(), line1: "", line2: "", city: "", postcode: "", country: "GB", reason: "" }));
                await reload(); await load();
              })}>
              {busy ? "Saving…" : "Record change"}
            </button>
          </div>
        </section>
      )}
      <section className="card">
        <h2 className="h3">History</h2>
        {failed ? <LoadFailed what="The contact history" /> : !history ? <Spinner /> : history.length === 0 ? (
          <p className="empty">No change has been recorded since the CAS was assigned. The details above are from the application.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Changes of address and contact details, newest first</caption>
              <thead><tr><th scope="col">From</th><th scope="col">Address</th><th scope="col">Phone and email</th><th scope="col">Recorded</th></tr></thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td className="nowrap">{formatDate(h.effective_from)}</td>
                    <td>{addressLine(h.address, countries)}{h.reason && <span className="hint block">{h.reason}</span>}</td>
                    <td>{[h.phone, h.email].filter(Boolean).join(" · ") || "—"}</td>
                    <td className="nowrap">{h.recorded_by ?? "—"}<span className="hint block">{formatDateTime(h.recorded_at)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function VisaPanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ outcome: "granted", decision_date: today(), refusal_reason_category: "" });
  const { summary, busy, run } = useAction();
  const needsCategory = form.outcome === "refused";
  return (
    <section className="card">
      <h2 className="h3">Record the visa decision</h2>
      <ActionErrors summary={summary} />
      <p className="hint">
        A refusal closes the record and counts towards the refusal rate. It is not itself reportable.
      </p>
      <SelectField name="outcome" label="Outcome" required value={form.outcome}
        onChange={(v) => setForm({ ...form, outcome: v })}
        options={Object.entries(VISA_LABELS).map(([value, label]) => ({ value, label }))} />
      <DateField name="decision_date" label="Decision date" required value={form.decision_date}
        onChange={(v) => setForm({ ...form, decision_date: v })} />
      {needsCategory && (
        <TextField name="refusal_reason_category" label="Refusal reason category" required width="m"
          hint="For example: financial requirement, credibility interview, documents."
          value={form.refusal_reason_category}
          onChange={(v) => setForm({ ...form, refusal_reason_category: v })} />
      )}
      <div className="actions">
        <button type="button" className="button"
          disabled={busy || (needsCategory && !form.refusal_reason_category.trim())}
          onClick={() => void run(() => api.post(`/applications/${record.application_id}/visa-outcome`, {
            outcome: form.outcome, decision_date: form.decision_date,
            refusal_reason_category: form.refusal_reason_category || null,
          }), () => reload())}>
          Record decision
        </button>
      </div>
    </section>
  );
}

function AttendancePanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ session_date: today(), module: "", status: "present", absence_reason: "" });
  const { summary, busy, run } = useAction();
  const needsReason = form.status === "authorised_absence";
  return (
    <section className="card">
      <h2 className="h3">Record attendance</h2>
      <ActionErrors summary={summary} />
      <DateField name="session_date" label="Session date" required value={form.session_date}
        onChange={(v) => setForm({ ...form, session_date: v })} />
      <TextField name="module" label="Module or session" required width="m" value={form.module}
        onChange={(v) => setForm({ ...form, module: v })} />
      <SelectField name="status" label="Attendance" required value={form.status}
        onChange={(v) => setForm({ ...form, status: v })}
        options={Object.entries(ATTENDANCE_LABELS).map(([value, label]) => ({ value, label }))} />
      {needsReason && (
        <TextArea name="absence_reason" label="Reason for the authorised absence" required rows={2}
          hint="Authorised absence counts towards engagement, so the basis must be on file."
          value={form.absence_reason} onChange={(v) => setForm({ ...form, absence_reason: v })} />
      )}
      <div className="actions">
        <button type="button" className="button"
          disabled={busy || !form.module.trim() || (needsReason && !form.absence_reason.trim())}
          onClick={() => void run(() => api.post(`/students/${record.id}/attendance`, {
            session_date: form.session_date, module: form.module, status: form.status,
            absence_reason: form.absence_reason || null,
          }), async () => { setForm({ ...form, module: "", absence_reason: "" }); await reload(); })}>
          Record attendance
        </button>
      </div>
    </section>
  );
}

function StatusChangePanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ type: "deferral", effective_date: today(), reason: "" });
  const [confirming, setConfirming] = useState(false);
  const { summary, busy, run } = useAction();
  const label = STATUS_CHANGES.find((c) => c.value === form.type)?.label ?? form.type;
  const tooShort = form.reason.trim().length < 10;

  return (
    <section className="card">
      <h2 className="h3">Record a change of circumstances</h2>
      <ActionErrors summary={summary} />
      <p className="hint">
        Each of these is reportable to UKVI. Recording it here raises the reportable event and starts the
        10 working day clock in the same step — the two cannot come apart.
      </p>
      <SelectField name="type" label="What has changed" required value={form.type}
        onChange={(v) => setForm({ ...form, type: v })} options={STATUS_CHANGES} />
      <DateField name="effective_date" label="Date it took effect" required value={form.effective_date}
        hint="The reporting deadline is counted from this date, not from today."
        onChange={(v) => setForm({ ...form, effective_date: v })} />
      <TextArea name="reason" label="Reason" required rows={3} maxLength={2000} value={form.reason}
        hint="Reported to UKVI alongside the change and recorded in the audit trail."
        error={form.reason && tooShort ? "Give a reason of at least 10 characters." : undefined}
        onChange={(v) => setForm({ ...form, reason: v })} />
      <div className="actions">
        <button type="button" className="button" disabled={busy || tooShort} onClick={() => setConfirming(true)}>
          Record {label.toLowerCase()}
        </button>
      </div>
      {confirming && (
        <Confirm
          title={`Record a ${label.toLowerCase()} for ${record.student_name ?? record.application_ref}?`}
          consequence={`This changes the student's sponsorship status and raises a reportable event that ESE must report to UKVI within 10 working days of ${formatDate(form.effective_date)}. It cannot be undone, only followed by a further change.`}
          confirmLabel="Record and raise the event" busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void run(() => api.post(`/students/${record.id}/status-change`, {
            type: form.type, effective_date: form.effective_date, reason: form.reason,
          }), async () => { setConfirming(false); setForm({ ...form, reason: "" }); await reload(); })}
        />
      )}
    </section>
  );
}

function EscalationPanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  const [form, setForm] = useState({ step: "email", notes: "" });
  const { summary, busy, run } = useAction();
  return (
    <section className="card">
      <h2 className="h3">Record an engagement escalation</h2>
      <ActionErrors summary={summary} />
      <SelectField name="step" label="Step taken" required value={form.step}
        onChange={(v) => setForm({ ...form, step: v })} options={ESCALATION_STEPS} />
      <TextArea name="notes" label="Notes" rows={2} value={form.notes}
        onChange={(v) => setForm({ ...form, notes: v })} />
      <div className="actions">
        <button type="button" className="button" disabled={busy}
          onClick={() => void run(() => api.post(`/students/${record.id}/escalation`, form),
            async () => { setForm({ ...form, notes: "" }); await reload(); })}>
          Record escalation
        </button>
      </div>
    </section>
  );
}

function EventsPanel({ record, reload }: { record: StudentRecordFull; reload: () => Promise<void> }) {
  if (record.events.length === 0) {
    return (
      <section className="card">
        <h2 className="h3">Reportable events</h2>
        <p className="empty">None. Events appear here when something reportable happens to this student.</p>
      </section>
    );
  }
  return (
    <section className="card">
      <h2 className="h3">Reportable events</h2>
      <EventTable events={record.events} reload={reload} showStudent={false} />
    </section>
  );
}

function EvidencePackPanel({ applicationId, reference, senior }: {
  applicationId: number; reference: string; senior: boolean;
}) {
  const [job, setJob] = useState<ExportJobRow | null>(null);
  const { summary, busy, run } = useAction();
  return (
    <section className="card">
      <h2 className="h3">Evidence pack</h2>
      <ActionErrors summary={summary} />
      <p className="hint">
        Everything held on {reference} in one file: the application, documents, both sign-offs, the offer,
        fees, the CAS, the visa outcome, attendance, every reportable event and the full audit trail.
        The download expires 24 hours after it is produced.
      </p>
      {!senior && (
        <p className="hint">
          A pack contains the full payment history, so it is produced by a senior user or the
          Authorising Officer.
        </p>
      )}
      <div className="actions">
        <button type="button" className="button" disabled={busy || !senior}
          onClick={() => void run(() => api.post<ExportJobRow>(`/applications/${applicationId}/evidence-pack`), setJob)}>
          {busy ? "Building…" : "Build evidence pack"}
        </button>
        {job?.status === "ready" && (
          <button type="button" className="button button-secondary"
            onClick={() => void downloadExport(job)}>
            Download {job.result_filename}
          </button>
        )}
      </div>
      {job?.status === "failed" && <Alert kind="error">{job.error_detail ?? "The pack could not be built."}</Alert>}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// The reporting queue
// ---------------------------------------------------------------------------------------------
export function EventTable({ events, reload, showStudent = true }: {
  events: ReportableEventRow[];
  reload: () => Promise<void>;
  showStudent?: boolean;
}) {
  const { user } = useAuth();
  const senior = user?.role === "senior" || user?.role === "admin";
  const compliance = user?.role === "compliance" || senior;
  const [reporting, setReporting] = useState<number | null>(null);
  const [closing, setClosing] = useState<number | null>(null);
  const [form, setForm] = useState({ sms_reference: "", reported_date: today() });
  const [closeReason, setCloseReason] = useState("");
  const { summary, busy, run } = useAction();

  return (
    <>
      <ActionErrors summary={summary} />
      <table className="table">
        <caption className="visually-hidden">Reportable events</caption>
        <thead>
          <tr>
            {showStudent && <th scope="col">Student</th>}
            <th scope="col">Event</th><th scope="col">Occurred</th><th scope="col">Report by</th>
            <th scope="col">Clock</th><th scope="col">Status</th><th scope="col"><span className="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id} className={e.overdue ? "row-overdue" : undefined}>
              {showStudent && (
                <td>
                  <Link to={`/staff/students/${e.student_id}`}>{e.student_name ?? "—"}</Link>
                  <span className="hint block">{e.application_ref}</span>
                </td>
              )}
              <td>
                {EVENT_LABELS[e.event_type] ?? e.event_type}
                <span className="hint block">{e.source === "automatic" ? "Raised automatically" : "Recorded by staff"}</span>
              </td>
              <td className="nowrap">{formatDate(e.event_date)}</td>
              <td className="nowrap">{formatDate(e.deadline_date)}</td>
              <td><DeadlineChip event={e} />{e.escalated_at && <span className="tag tag-overdue">Escalated</span>}</td>
              <td>
                {e.status === "reported"
                  ? <>Reported {formatDate(e.reported_date)}<span className="hint block">{e.sms_reference}</span></>
                  : e.status === "not_required"
                    ? <>Not reportable<span className="hint block">{e.not_required_reason}</span></>
                    : "Pending"}
              </td>
              <td className="row-actions">
                <Link to={`/staff/events/${e.id}`}>View<span className="visually-hidden"> {EVENT_LABELS[e.event_type] ?? e.event_type} event</span></Link>
                {e.status === "pending" && compliance && (
                  <button type="button" className="link-button" onClick={() => { setReporting(e.id); setClosing(null); }}>
                    Record report
                  </button>
                )}
                {e.status === "pending" && senior && (
                  <button type="button" className="link-button" onClick={() => { setClosing(e.id); setReporting(null); }}>
                    Not reportable
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {reporting !== null && (
        <div className="card nested">
          <h3 className="h4">Record the report to UKVI</h3>
          <p className="hint">Report on the Sponsorship Management System first, then record its reference here.</p>
          <TextField name="sms_reference" label="SMS reference" required width="m" value={form.sms_reference}
            onChange={(v) => setForm({ ...form, sms_reference: v })} />
          <DateField name="reported_date" label="Date reported" required value={form.reported_date}
            onChange={(v) => setForm({ ...form, reported_date: v })} />
          <div className="actions">
            <button type="button" className="button" disabled={busy || !form.sms_reference.trim()}
              onClick={() => void run(() => api.post(`/events/${reporting}/report`, form),
                async () => { setReporting(null); setForm({ sms_reference: "", reported_date: today() }); await reload(); })}>
              Record report
            </button>
            <button type="button" className="button button-secondary" onClick={() => setReporting(null)}>Cancel</button>
          </div>
        </div>
      )}

      {closing !== null && (
        <Confirm
          title="Close this event without reporting it?"
          consequence="The event stays on the record as not reportable, with your name and reason against it. If it should have been reported, this is what an audit will ask about."
          confirmLabel="Close as not reportable" busy={busy} disabled={closeReason.trim().length < 10}
          onCancel={() => setClosing(null)}
          onConfirm={() => void run(() => api.post(`/events/${closing}/not-required`, { reason: closeReason }),
            async () => { setClosing(null); setCloseReason(""); await reload(); })}
        >
          <TextArea name="reason" label="Why is this not reportable?" required rows={3} value={closeReason}
            error={closeReason.trim().length < 10 ? "Give a reason of at least 10 characters." : undefined}
            onChange={setCloseReason} />
        </Confirm>
      )}
    </>
  );
}

type ReportableEventRow = StudentRecordFull["events"][number];

export function ReportingQueue() {
  const [params, setParams] = useSearchParams();
  const [creating, setCreating] = useState(false);
  const [events, setEvents] = useState<ReportableEventRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const view = params.get("view") ?? "pending";

  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (view === "pending" || view === "overdue") qs.set("status", "pending");
    if (view === "overdue") qs.set("overdue", "true");
    if (view === "closed") qs.set("status", "reported");
    try {
      setEvents(await api.get<ReportableEventRow[]>(`/events?${qs}`));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [view]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Layout wide>
      <div className="page-head">
        <h1>Reporting queue</h1>
        {!creating && <button type="button" className="button" onClick={() => setCreating(true)}>+ New report</button>}
      </div>
      {creating && <NewReport onDone={async () => { setCreating(false); await load(); }} onCancel={() => setCreating(false)} />}
      <p className="hint">
        Reportable events, soonest deadline first. UKVI must be told within 10 working days of the event —
        weekends and England and Wales bank holidays do not count.
      </p>
      <nav className="stage-nav" aria-label="Views">
        {[["pending", "Open"], ["overdue", "Overdue"], ["closed", "Reported"]].map(([key, label]) => (
          <button
            key={key} type="button"
            className={`link-button${view === key ? " current" : ""}`}
            aria-current={view === key ? "page" : undefined}
            onClick={() => { const n = new URLSearchParams(params); n.set("view", key); setParams(n); }}
          >
            {label}
          </button>
        ))}
      </nav>
      {failed ? <LoadFailed what="The reporting queue" /> : !events ? <Spinner /> : events.length === 0 ? (
        <p className="empty">
          {view === "overdue" ? "Nothing is overdue." : view === "closed" ? "Nothing reported yet." : "No open events."}
        </p>
      ) : (
        <EventTable events={events} reload={load} />
      )}
    </Layout>
  );
}

/**
 * "+ New report" on the queue (prototype screen 14). A report always belongs to a student and a
 * change in their circumstances, so this picks the student and then uses the same form as the
 * student record — one way to raise an event, whichever screen it starts from.
 */
function NewReport({ onDone, onCancel }: { onDone: () => Promise<void>; onCancel: () => void }) {
  const [options, setOptions] = useState<{ value: string; label: string }[] | null>(null);
  const [studentId, setStudentId] = useState("");
  const [record, setRecord] = useState<StudentRecordFull | null>(null);

  useEffect(() => {
    api.get<{ items: { id: number; student_name: string | null; application_ref: string; enrolment_status: string }[] }>("/students?page_size=100")
      .then((d) => setOptions(d.items
        .filter((x) => !["completed", "withdrawn", "non_enrolled"].includes(x.enrolment_status))
        .map((x) => ({ value: String(x.id), label: `${x.student_name ?? "—"} (${x.application_ref})` }))))
      .catch(() => setOptions([]));
  }, []);
  useEffect(() => {
    setRecord(null);
    if (studentId) void api.get<StudentRecordFull>(`/students/${studentId}`).then(setRecord);
  }, [studentId]);

  return (
    <section className="card nested" aria-labelledby="new-report-h">
      <h2 className="h3" id="new-report-h">New report</h2>
      {!options ? <Spinner /> : (
        <SelectField name="new_report_student" label="Student" required value={studentId} onChange={setStudentId}
          placeholder="Choose a sponsored student…" options={options}
          hint="Only students still sponsored are listed. Non-enrolment is raised automatically." />
      )}
      {record && <StatusChangePanel record={record} reload={onDone} />}
      <p><button type="button" className="link-button" onClick={onCancel}>Cancel</button></p>
    </section>
  );
}

/** One reportable event: what, when, the deadline, the report, its evidence and its history. */
export function EventDetail() {
  const { id } = useParams();
  const { user } = useAuth();
  const canEvidence = user?.role === "compliance" || user?.role === "senior" || user?.role === "admin";
  const [data, setData] = useState<{
    event: ReportableEventRow;
    evidence: { id: number; filename: string; uploaded_at: string; uploaded_by: string | null } | null;
    history: { at: string; user_name: string; action: string; detail: string }[];
  } | null>(null);
  const [failed, setFailed] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setData(await api.get(`/events/${id}`)); } catch { setFailed(true); }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  if (failed) return <Layout><LoadFailed what="This event" /></Layout>;
  if (!data) return <Layout><Spinner /></Layout>;
  const e = data.event;

  async function upload(ev: FormEvent) {
    ev.preventDefault();
    if (!file) { setError("Choose the file to upload."); return; }
    setBusy(true); setError(null); setNotice(null);
    const form = new FormData();
    form.append("file", file);
    try {
      setData(await api.upload(`/events/${id}/evidence`, form));
      setFile(null);
      (ev.target as HTMLFormElement).reset();
      setNotice("Evidence uploaded and scanned. It is kept with the event and in the student's evidence pack.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <p><Link to="/staff/events">← Reporting queue</Link></p>
      <div className="page-head">
        <h1>{EVENT_LABELS[e.event_type] ?? e.event_type}</h1>
        <DeadlineChip event={e} />
      </div>
      {notice && <Alert kind="success">{notice}</Alert>}
      <section className="card">
        <dl className="kv">
          <div><dt>Student</dt><dd><Link to={`/staff/students/${e.student_id}`}>{e.student_name ?? "—"}</Link> <span className="hint">{e.application_ref}</span></dd></div>
          <div><dt>Occurred</dt><dd>{formatDate(e.event_date)}</dd></div>
          <div><dt>Report to UKVI by</dt><dd>{formatDate(e.deadline_date)}</dd></div>
          <div><dt>Raised</dt><dd>{e.source === "automatic" ? "Automatically" : "By staff"}, {formatDate(e.created_at)}</dd></div>
          {e.detail && <div><dt>Detail</dt><dd>{e.detail}</dd></div>}
          <div><dt>Status</dt><dd>
            {e.status === "reported" ? <>Reported {formatDate(e.reported_date)} by {e.reported_by ?? "—"} · SMS reference <b>{e.sms_reference}</b></>
              : e.status === "not_required" ? <>Closed as not reportable: {e.not_required_reason}</> : "Pending — not yet reported"}
          </dd></div>
        </dl>
      </section>

      <section className="card" aria-labelledby="evidence-h">
        <h2 className="h3" id="evidence-h">Evidence of the report</h2>
        <p className="hint">The confirmation from the Sponsorship Management System — a PDF or a screenshot of the submission.</p>
        {data.evidence ? (
          <p>
            <button type="button" className="link-button" onClick={() => void downloadDocument({ id: data.evidence!.id, filename: data.evidence!.filename } as DocumentRow)}>
              {data.evidence.filename}
            </button>
            <span className="hint block">Uploaded {formatDateTime(data.evidence.uploaded_at)}{data.evidence.uploaded_by ? ` by ${data.evidence.uploaded_by}` : ""}</span>
          </p>
        ) : <p className="empty">No evidence held yet.</p>}
        {canEvidence && e.status !== "not_required" && (
          <form onSubmit={upload} noValidate className="inline-upload">
            {error && <Alert kind="error">{error}</Alert>}
            <label htmlFor="event-evidence" className="label">{data.evidence ? "Replace the evidence" : "Choose the evidence file"} <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
            <input id="event-evidence" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
              onChange={(ev) => setFile(ev.target.files?.[0] ?? null)} />
            <button className="button button-secondary" type="submit" disabled={busy || !file}>{busy ? "Uploading and scanning…" : "Upload evidence"}</button>
          </form>
        )}
      </section>

      <section className="card" aria-labelledby="history-h">
        <h2 className="h3" id="history-h">History</h2>
        <ol className="history">
          {data.history.map((h, n) => (
            <li key={n}><span className="hint">{formatDateTime(h.at)} · {h.user_name}</span><br />{h.detail}</li>
          ))}
        </ol>
      </section>
    </Layout>
  );
}

/** Case notes (prototype screen 12). Append-only: a correction is a further note, never an edit. */
function NotesPanel({ studentId }: { studentId: number }) {
  const [notes, setNotes] = useState<{ id: number; body: string; author: string | null; created_at: string }[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [body, setBody] = useState("");
  const { summary, busy, run } = useAction();
  const load = useCallback(async () => {
    try { setNotes(await api.get(`/students/${studentId}/notes`)); } catch { setFailed(true); }
  }, [studentId]);
  useEffect(() => { void load(); }, [load]);

  return (
    <>
      <section className="card">
        <h2 className="h3">Add a note</h2>
        <ActionErrors summary={summary} />
        <TextArea name="note" label="Note" required rows={3} maxLength={4000} value={body} onChange={setBody}
          hint="Notes are part of the sponsor record and cannot be edited or deleted. Correct a note by adding another." />
        <div className="actions">
          <button type="button" className="button" disabled={busy || body.trim().length < 3}
            onClick={() => void run(() => api.post(`/students/${studentId}/notes`, { body }), async () => { setBody(""); await load(); })}>
            Add note
          </button>
        </div>
      </section>
      <section className="card">
        <h2 className="h3">Notes</h2>
        {failed ? <LoadFailed what="Notes" /> : !notes ? <Spinner /> : notes.length === 0 ? <p className="empty">No notes yet.</p> : (
          <ol className="history">
            {notes.map((n) => (
              <li key={n.id}><span className="hint">{formatDateTime(n.created_at)} · {n.author ?? "—"}</span><br />{n.body}</li>
            ))}
          </ol>
        )}
      </section>
    </>
  );
}

const STUDENT_DOC_CATEGORIES = ["passport", "enrolment_evidence", "financial", "other"];

/** Everything on the student's file (prototype screen 12): the application's documents and those added since. */
function DocumentsPanel({ studentId }: { studentId: number }) {
  const [docs, setDocs] = useState<DocumentRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [category, setCategory] = useState("passport");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setDocs(await api.get(`/students/${studentId}/documents`)); } catch { setFailed(true); }
  }, [studentId]);
  useEffect(() => { void load(); }, [load]);

  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) { setError("Choose a file to upload."); return; }
    setBusy(true); setError(null);
    const form = new FormData();
    form.append("category", category);
    form.append("file", file);
    try {
      await api.upload(`/students/${studentId}/documents`, form);
      setFile(null);
      (e.target as HTMLFormElement).reset();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className="card">
        <h2 className="h3">Documents</h2>
        {failed ? <LoadFailed what="Documents" /> : !docs ? <Spinner /> : docs.length === 0 ? <p className="empty">No documents on file.</p> : (
          <table className="table">
            <caption className="visually-hidden">Documents on the student's file</caption>
            <thead><tr><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Uploaded</th><th scope="col">Status</th></tr></thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.id}>
                  <td>{d.scan_status === "clean"
                    ? <button type="button" className="link-button" onClick={() => void downloadDocument(d)}>{d.filename}</button>
                    : d.filename}</td>
                  <td>{CATEGORY_LABELS[d.category] ?? d.category}</td>
                  <td className="nowrap">{formatDateTime(d.uploaded_at)}</td>
                  <td>
                    <span className={`tag tag-${d.scan_status}`}>{d.scan_status}</span>
                    {d.verified_at && <span className="tag tag-clean">Verified</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card narrow-card">
        <h2 className="h3">Add a document</h2>
        <p className="hint">For example a renewed passport or BRP, enrolment evidence, or a change of funding.</p>
        <form onSubmit={upload} noValidate className="inline-upload">
          {error && <Alert kind="error">{error}</Alert>}
          <SelectField name="student_doc_category" label="Document type" required value={category} onChange={setCategory}
            options={STUDENT_DOC_CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABELS[c] ?? c }))} />
          <label htmlFor="student-file" className="label">File <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
          <input id="student-file" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <button className="button button-secondary" type="submit" disabled={busy || !file}>{busy ? "Uploading and scanning…" : "Upload"}</button>
        </form>
      </section>
    </>
  );
}
