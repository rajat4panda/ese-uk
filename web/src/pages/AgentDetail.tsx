import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AGENT_EDITORS, api, ApiError, countryNames, downloadAgentDocument, downloadAgentEvidence, formatDate, formatDateTime,
  type AgentDetail as Agent, type AgentStatus, type EvidenceKind,
} from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, ErrorSummary, Layout, SelectField, Spinner, TextArea, TextField, detailsToErrors } from "../components/ui";
import { Confirm, LoadFailed } from "../components/workflow";

/**
 * One recruitment agent: its details, its status and the evidence held (BRD FR-90, FR-91, FR-94).
 * Also the "add an agent" screen, at /staff/agents/new.
 *
 * Status is changed with its own action and a reason, never through the edit form: suspending an
 * agent is a decision somebody is accountable for, and the reason is shown on the register.
 */

interface Form {
  name: string; country: string; contact_name: string; contact_email: string; contact_phone: string;
  due_diligence_date: string; next_review_date: string; refusal_rate_threshold_pct: string;
}
const EMPTY: Form = {
  name: "", country: "", contact_name: "", contact_email: "", contact_phone: "",
  due_diligence_date: "", next_review_date: "", refusal_rate_threshold_pct: "",
};
type Summary = { field: string | null; message: string }[];

const STATUS_LABEL: Record<AgentStatus, string> = {
  active: "Active", under_review: "Under review", suspended: "Suspended", deactivated: "Deactivated",
};
const STATUS_ACTIONS: Record<AgentStatus, { to: AgentStatus; label: string; consequence: string }[]> = {
  active: [
    { to: "under_review", label: "Put under review", consequence: "The agent can still be selected on applications while the review runs." },
    { to: "suspended", label: "Suspend", consequence: "No new application can be attributed to this agent until it is reactivated. Submitted applications keep their attribution; a draft that names this agent cannot be submitted until the applicant chooses again." },
    { to: "deactivated", label: "Deactivate", consequence: "For an agent ESE no longer works with. It stays on the register with its history and cannot be attributed new applications." },
  ],
  under_review: [
    { to: "active", label: "Review complete — reactivate", consequence: "The agent returns to active. Record the new due-diligence and review dates as well." },
    { to: "suspended", label: "Suspend", consequence: "No new application can be attributed to this agent until it is reactivated." },
    { to: "deactivated", label: "Deactivate", consequence: "The agent stays on the register with its history and cannot be attributed new applications." },
  ],
  suspended: [
    { to: "active", label: "Reactivate", consequence: "The agent can be selected on applications again." },
    { to: "deactivated", label: "Deactivate", consequence: "The agent stays on the register with its history and cannot be attributed new applications." },
  ],
  deactivated: [
    { to: "active", label: "Reactivate", consequence: "The agent can be selected on applications again." },
  ],
};
const EVIDENCE: { kind: EvidenceKind; label: string; hint: string }[] = [
  { kind: "agreement", label: "Agent agreement", hint: "The signed agreement between ESE and the agent." },
  { kind: "due_diligence", label: "Due-diligence record", hint: "The checks made on the agent, dated." },
];

/** Today as YYYY-MM-DD in the browser's own time zone (toISOString would give the UTC date). */
function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function toForm(a: Agent): Form {
  return {
    name: a.name, country: a.country_code, contact_name: a.contact_name ?? "", contact_email: a.contact_email ?? "",
    contact_phone: a.contact_phone ?? "", due_diligence_date: a.due_diligence_date ?? "",
    next_review_date: a.next_review_date ?? "",
    refusal_rate_threshold_pct: a.refusal_rate_threshold_pct === null ? "" : String(a.refusal_rate_threshold_pct),
  };
}

/** The request body: blanks become null, and on an edit only what changed is sent. */
function toBody(form: Form, original: Form | null): Record<string, string | number | null> {
  const body: Record<string, string | number | null> = {};
  (Object.keys(form) as (keyof Form)[]).forEach((k) => {
    if (original && form[k].trim() === original[k].trim()) return;
    const v = form[k].trim();
    // Sent as typed. The server validates the threshold, so "10%" is refused with a message rather
    // than turned into NaN, which JSON would send as null and silently clear the threshold.
    body[k] = v === "" ? null : v;
  });
  return body;
}

function errorsFrom(err: unknown, setErrors: (e: Record<string, string>) => void, setSummary: (s: Summary) => void) {
  if (err instanceof ApiError) {
    setErrors(detailsToErrors(err.details));
    setSummary(err.details.length
      ? err.details.map((d) => ({ field: d.field, message: d.message ?? err.message }))
      : [{ field: null, message: err.message }]);
  } else {
    setSummary([{ field: null, message: "Something went wrong. Try again." }]);
  }
}

export function AgentDetail() {
  const { id } = useParams();
  const isNew = id === "new";
  const navigate = useNavigate();
  const { user } = useAuth();
  const canEdit = !!user && AGENT_EDITORS.includes(user.role);

  const [agent, setAgent] = useState<Agent | null>(null);
  const [failed, setFailed] = useState(false);
  const [countries, setCountries] = useState<{ value: string; label: string }[]>([]);
  const [editing, setEditing] = useState(isNew);
  const [form, setForm] = useState<Form>(EMPTY);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [summary, setSummary] = useState<Summary>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ to: AgentStatus; label: string; consequence: string } | null>(null);
  const [reason, setReason] = useState("");
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    void countryNames().then((names) =>
      setCountries(Object.entries(names).map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label))));
  }, []);

  useEffect(() => {
    if (isNew) return;
    api.get<Agent>(`/agents/${id}`).then((a) => { setAgent(a); setForm(toForm(a)); }).catch(() => setFailed(true));
  }, [id, isNew]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setErrors({}); setSummary([]); setNotice(null); setBusy(true);
    try {
      if (isNew) {
        const created = await api.post<Agent>("/agents", toBody(form, null));
        setAgent(created); setForm(toForm(created)); setEditing(false);
        navigate(`/staff/agents/${created.id}`, { replace: true });
        setNotice(`${created.name} added to the register.`);
      } else if (agent) {
        const body = toBody(form, toForm(agent));
        if (Object.keys(body).length === 0) { setEditing(false); return; }
        const updated = await api.patch<Agent>(`/agents/${agent.id}`, body);
        setAgent(updated); setForm(toForm(updated)); setEditing(false);
        setNotice("Changes saved. The previous values are recorded in the audit trail.");
      }
    } catch (err) {
      errorsFrom(err, setErrors, setSummary);
    } finally {
      setBusy(false);
    }
  }

  async function changeStatus() {
    if (!agent || !pending) return;
    setBusy(true); setStatusError(null);
    try {
      const updated = await api.post<Agent>(`/agents/${agent.id}/status`,
        { status: pending.to, reason: pending.to === "active" ? null : reason.trim() });
      setAgent(updated); setPending(null); setReason("");
      setNotice(`${updated.name} is now ${STATUS_LABEL[updated.status].toLowerCase()}.`);
    } catch (err) {
      setStatusError(err instanceof ApiError ? (err.details[0]?.message ?? err.message) : "The change could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  if (failed) return <Layout><LoadFailed what="This agent" /></Layout>;
  if (!isNew && !agent) return <Layout><Spinner /></Layout>;
  if (isNew && !canEdit) return <Layout><Alert kind="error">Only compliance, the Authorising Officer or an administrator can add agents.</Alert></Layout>;

  const set = (k: keyof Form) => (v: string) => setForm({ ...form, [k]: v });
  const overdue = agent?.next_review_date && agent.next_review_date < localToday();

  return (
    <Layout>
      <p><Link to="/staff/agents">← All agents</Link></p>
      <div className="page-head">
        <h1>{isNew ? "Add an agent" : agent!.name}</h1>
        {agent && !editing && canEdit && (
          <button type="button" className="button button-secondary" onClick={() => { setEditing(true); setNotice(null); }}>Edit details</button>
        )}
      </div>
      {notice && <Alert kind="success">{notice}</Alert>}

      {agent && agent.status !== "active" && (
        <Alert kind={agent.status === "under_review" ? "warning" : "error"} title={STATUS_LABEL[agent.status]}>
          <p>{agent.status_reason}</p>
          {agent.status !== "under_review" && <p>New applications cannot be attributed to this agent.</p>}
        </Alert>
      )}
      {agent && overdue && agent.status !== "suspended" && agent.status !== "deactivated" && (
        <Alert kind="warning" title="Review overdue">
          <p>The review was due on {formatDate(agent.next_review_date!)}. Record the new due-diligence and next review dates once it is done.</p>
        </Alert>
      )}

      {editing ? (
        <section className="card" aria-labelledby="details-h">
          <h2 id="details-h" className="h3">{isNew ? "Agent details" : "Edit details"}</h2>
          <ErrorSummary title="There is a problem" errors={summary} />
          <form onSubmit={save} noValidate>
            <TextField name="name" label="Agent name" required value={form.name} onChange={set("name")} error={errors.name}
              hint="The trading name, as on the agreement." />
            <SelectField name="country" label="Country" required value={form.country} onChange={set("country")} error={errors.country}
              options={countries} />
            <TextField name="contact_name" label="Contact name" value={form.contact_name} onChange={set("contact_name")} error={errors.contact_name} />
            <TextField name="contact_email" label="Contact email" type="email" value={form.contact_email} onChange={set("contact_email")} error={errors.contact_email} />
            <TextField name="contact_phone" label="Contact phone" type="tel" width="m" value={form.contact_phone} onChange={set("contact_phone")} error={errors.contact_phone}
              hint="Include the country code, for example +234 803 123 4567." />
            <DateField name="due_diligence_date" label="Due diligence last completed" value={form.due_diligence_date} onChange={set("due_diligence_date")} error={errors.due_diligence_date} />
            <DateField name="next_review_date" label="Next review due" value={form.next_review_date} onChange={set("next_review_date")} error={errors.next_review_date}
              hint="The compliance inbox is reminded ahead of this date, and again if it passes." />
            <TextField name="refusal_rate_threshold_pct" label="Refusal-rate threshold (%)" width="s" inputMode="decimal"
              value={form.refusal_rate_threshold_pct} onChange={set("refusal_rate_threshold_pct")} error={errors.refusal_rate_threshold_pct}
              hint="The register flags the agent when its visa refusal rate goes above this. Leave blank for no threshold." />
            <div className="actions">
              <button className="button" type="submit" disabled={busy}>{busy ? "Saving…" : isNew ? "Add agent" : "Save changes"}</button>
              <button type="button" className="button button-secondary" disabled={busy}
                onClick={() => (isNew ? navigate("/staff/agents") : (setEditing(false), setForm(toForm(agent!)), setErrors({}), setSummary([])))}>
                Cancel
              </button>
            </div>
          </form>
        </section>
      ) : agent && (
        <section className="card" aria-labelledby="details-h">
          <h2 id="details-h" className="h3">Details</h2>
          <dl className="kv">
            <div><dt>Status</dt><dd><span className={`tag tag-${agent.status}`}>{STATUS_LABEL[agent.status]}</span></dd></div>
            <div><dt>Country</dt><dd>{agent.country}</dd></div>
            <div><dt>Contact</dt><dd>{agent.contact_name ?? <span className="hint">Not recorded</span>}</dd></div>
            <div><dt>Email</dt><dd>{agent.contact_email ? <a href={`mailto:${agent.contact_email}`}>{agent.contact_email}</a> : <span className="hint">Not recorded</span>}</dd></div>
            <div><dt>Phone</dt><dd>{agent.contact_phone ?? <span className="hint">Not recorded</span>}</dd></div>
            <div><dt>Due diligence</dt><dd>{agent.due_diligence_date ? formatDate(agent.due_diligence_date) : <span className="hint">Not recorded</span>}</dd></div>
            <div><dt>Next review</dt><dd>{agent.next_review_date ? formatDate(agent.next_review_date) : <span className="hint">Not set</span>}</dd></div>
            <div><dt>Refusal-rate threshold</dt><dd>{agent.refusal_rate_threshold_pct !== null ? `${agent.refusal_rate_threshold_pct}%` : <span className="hint">None</span>}</dd></div>
            <div><dt>Last changed</dt><dd>{formatDateTime(agent.updated_at)}</dd></div>
          </dl>
        </section>
      )}

      {agent && (
        <section className="card" aria-labelledby="evidence-h">
          <h2 id="evidence-h" className="h3">Agreement and due diligence</h2>
          {downloadError && <Alert kind="error">{downloadError}</Alert>}
          {EVIDENCE.map((e) => (
            <EvidenceRow key={e.kind} agent={agent} kind={e.kind} label={e.label} hint={e.hint} canEdit={canEdit}
              onDownloadError={setDownloadError}
              onUploaded={(a) => { setAgent(a); setNotice(`${e.label} uploaded and scanned.`); }} />
          ))}
          {agent.evidence_history.some((h) => h.status !== "current") && (
            <details className="evidence-history">
              <summary>Earlier files ({agent.evidence_history.filter((h) => h.status !== "current").length})</summary>
              <ul>
                {agent.evidence_history.filter((h) => h.status !== "current").map((h) => (
                  <li key={h.id}>
                    {h.status === "superseded" ? (
                      <button type="button" className="link-button"
                        onClick={() => { setDownloadError(null); downloadAgentDocument(agent.id, h.id, h.filename).catch(() => setDownloadError("That file could not be downloaded.")); }}>
                        {h.filename}
                      </button>
                    ) : <span>{h.filename}</span>}
                    <span className="hint"> · {h.kind === "agreement" ? "Agreement" : "Due diligence"} · {formatDateTime(h.uploaded_at)}{h.uploaded_by ? ` by ${h.uploaded_by}` : ""} · </span>
                    <span className={`tag tag-${h.status === "quarantined" ? "quarantined" : "pending"}`}>{h.status === "quarantined" ? "Failed security scan" : "Superseded"}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      {agent && canEdit && (
        <section className="card" aria-labelledby="status-h">
          <h2 id="status-h" className="h3">Status</h2>
          {pending ? (
            <Confirm title={pending.label} consequence={pending.consequence} confirmLabel={pending.label} busy={busy}
              disabled={pending.to !== "active" && reason.trim().length < 10}
              onConfirm={() => void changeStatus()} onCancel={() => { setPending(null); setReason(""); setStatusError(null); }}>
              {statusError && <Alert kind="error">{statusError}</Alert>}
              {pending.to !== "active" && (
                <TextArea name="reason" label="Reason" required rows={3} maxLength={1000} minLength={10} value={reason} onChange={setReason}
                  hint="Shown on the register and recorded in the audit trail." />
              )}
            </Confirm>
          ) : (
            <div className="actions">
              {STATUS_ACTIONS[agent.status].map((a) => (
                <button key={a.to} type="button"
                  className={`button ${a.to === "suspended" || a.to === "deactivated" ? "button-warning" : "button-secondary"}`}
                  onClick={() => { setPending(a); setNotice(null); }}>
                  {a.label}
                </button>
              ))}
            </div>
          )}
        </section>
      )}
    </Layout>
  );
}

function EvidenceRow({ agent, kind, label, hint, canEdit, onUploaded, onDownloadError }: {
  agent: Agent; kind: EvidenceKind; label: string; hint: string; canEdit: boolean;
  onUploaded: (a: Agent) => void; onDownloadError: (message: string | null) => void;
}) {
  const doc = agent.evidence[kind];
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) { setError("Choose a file to upload."); return; }
    setBusy(true); setError(null);
    const form = new FormData();
    form.append("kind", kind);
    form.append("file", file);
    try {
      const updated = await api.upload<Agent>(`/agents/${agent.id}/evidence`, form);
      setFile(null);
      if (input.current) input.current.value = "";
      onUploaded(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const inputId = `evidence-${kind}`;
  return (
    <div className="evidence-row">
      <h3 className="h4">{label}</h3>
      {doc ? (
        <p>
          <button type="button" className="link-button" disabled={!doc.available}
            onClick={() => {
              onDownloadError(null);
              downloadAgentEvidence(agent.id, kind, doc.filename)
                .catch((err) => onDownloadError(err instanceof ApiError ? err.message : "The file could not be downloaded. Try again."));
            }}>{doc.filename}</button>
          <span className="hint block">Uploaded {formatDateTime(doc.uploaded_at)}{doc.uploaded_by ? ` by ${doc.uploaded_by}` : ""}</span>
        </p>
      ) : (
        <p className="hint">Nothing held yet.</p>
      )}
      {canEdit && (
        <form onSubmit={upload} noValidate className="inline-upload">
          {error && <Alert kind="error">{error}</Alert>}
          <label htmlFor={inputId} className="label">{doc ? `Replace the ${label.toLowerCase()}` : `Choose the ${label.toLowerCase()} file`} <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
          <p className="hint" id={`${inputId}-hint`}>{hint}{doc ? " The current file is kept on record as superseded." : ""}</p>
          <input id={inputId} ref={input} type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
            aria-describedby={`${inputId}-hint`} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <button className="button button-secondary" type="submit" disabled={busy || !file}>{busy ? "Uploading and scanning…" : `Upload ${label.toLowerCase()}`}</button>
        </form>
      )}
    </div>
  );
}
