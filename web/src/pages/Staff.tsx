import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  api, ApiError, CATEGORY_LABELS, RISK_LABELS, ROLE_LABELS, STATUS_LABELS, countryNames, downloadDocument,
  formatBytes, formatDate, formatDateTime, type ApplicationRecord, type AuditRow, type DocumentRow,
  type Role, type Summary,
} from "../api";
import { useAuth } from "../auth";
import { Alert, Layout, SelectField, Spinner, StatusBadge, TextArea, TextField } from "../components/ui";
import { ActionErrors, Confirm, LoadFailed, RecordShell, money, useAction } from "../components/workflow";

const CAN_SEE_AUDIT: Role[] = ["admissions", "compliance", "senior", "admin"];

/**
 * Pipeline tabs — prototype screen 5. A stage is a set of statuses, sent to the API as a repeated
 * `status` parameter so paging stays correct; the counts come from /applications/stats in one call.
 */
const STAGES: { key: string; label: string; statuses: string[] }[] = [
  { key: "", label: "All", statuses: [] },
  { key: "review", label: "Under review", statuses: ["submitted", "academic_review", "compliance_review"] },
  { key: "approved", label: "Awaiting offer", statuses: ["offer_pending"] },
  { key: "assessment", label: "Academic", statuses: ["submitted", "academic_review"] },
  { key: "compliance", label: "Compliance", statuses: ["compliance_review", "precas_ready"] },
  { key: "offers", label: "Offers", statuses: ["offer_issued", "offer_accepted"] },
  { key: "cas", label: "CAS assigned", statuses: ["cas_assigned"] },
];

export function StaffApplications() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [data, setData] = useState<{ items: Summary[]; total: number; page: number; page_size: number } | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [listFailed, setListFailed] = useState(false);
  const stage = params.get("stage") ?? "";
  const status = params.get("status") ?? "";
  const page = Number(params.get("page") ?? 1);
  const current = STAGES.find((s) => s.key === stage) ?? STAGES[0];

  useEffect(() => {
    const qs = new URLSearchParams();
    if (status) qs.append("status", status);
    else for (const s of current.statuses) qs.append("status", s);
    if (params.get("q")) qs.set("q", params.get("q")!);
    qs.set("page", String(page));
    api.get<typeof data>(`/applications?${qs}`).then((d) => { setData(d); setListFailed(false); })
      .catch(() => setListFailed(true));
  }, [params, status, page, current]);

  useEffect(() => {
    api.get<Record<string, number>>("/applications/stats").then(setCounts).catch(() => setCounts({}));
  }, []);

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    next.delete("page");
    if (key === "stage") next.delete("status");
    if (key === "status") next.delete("stage");
    setParams(next);
  }

  function search(e: FormEvent) {
    e.preventDefault();
    setParam("q", q);
  }

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const countFor = (s: { key: string; statuses: string[] }) =>
    s.key === "" ? total : s.statuses.reduce((sum, st) => sum + (counts[st] ?? 0), 0);

  return (
    <Layout>
      <div className="page-head">
        <h1>Applications</h1>
        {user && <span className="role-chip">{ROLE_LABELS[user.role]}</span>}
      </div>

      <ul className="filter-chips">
        {STAGES.map((s) => (
          <li key={s.key || "all"}>
            <a
              href={s.key ? `/staff?stage=${s.key}` : "/staff"}
              aria-current={!status && s.key === stage ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); setParam("stage", s.key); }}
            >
              {s.label}{Object.keys(counts).length > 0 && ` (${countFor(s)})`}
            </a>
          </li>
        ))}
      </ul>

      <form className="filters" onSubmit={search} role="search">
        <TextField name="q" label="Search" hint="Name, email or reference" value={q} onChange={setQ} width="m" hideOptional />
        <SelectField name="status" label="Exact status" value={status} placeholder="Any in this tab" hideOptional
          onChange={(v) => setParam("status", v)}
          options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))} />
        <button className="button" type="submit">Search</button>
      </form>

      {listFailed ? <LoadFailed what="The application list" /> : !data ? <Spinner /> : data.items.length === 0 ? (
        <p className="empty">No applications match. Applications appear here as soon as an applicant starts one.</p>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Applications, most recently updated first</caption>
              <thead><tr><th scope="col">Reference</th><th scope="col">Applicant</th><th scope="col">Programme</th><th scope="col">Intake</th><th scope="col">Status</th><th scope="col">Updated</th></tr></thead>
              <tbody>
                {data.items.map((a) => (
                  <tr key={a.id}>
                    <td><Link to={`/staff/applications/${a.id}`}>{a.ref}</Link></td>
                    <td><b>{a.full_name ?? "—"}</b><span className="hint block">{a.email}</span></td>
                    <td>{a.programme ?? "—"}</td>
                    <td>{a.intake ?? "—"}</td>
                    <td><StatusBadge status={a.status} label={STATUS_LABELS[a.status] ?? a.status} /></td>
                    <td className="nowrap">{formatDateTime(a.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pager" aria-label="Pages">
            <span>
              Showing {(page - 1) * data.page_size + 1}–{Math.min(page * data.page_size, data.total)} of {data.total}
            </span>
            <span>
              {page > 1 && <button type="button" className="link-button" onClick={() => { const n = new URLSearchParams(params); n.set("page", String(page - 1)); setParams(n); }}>‹ Previous</button>}
              {" "}<span className="page-now">{page}</span>{" "}
              {page * data.page_size < data.total && <button type="button" className="link-button" onClick={() => { const n = new URLSearchParams(params); n.set("page", String(page + 1)); setParams(n); }}>Next ›</button>}
            </span>
          </nav>
        </>
      )}
    </Layout>
  );
}

const SUMMARY_FIELDS: [string, string][] = [
  ["full_name", "Full name"], ["date_of_birth", "Date of birth"], ["email", "Email"], ["mobile", "Mobile"],
  ["nationality", "Nationality"], ["passport_number", "Passport number"], ["passport_expiry_date", "Passport expiry"],
  ["study_mode", "Study mode"], ["english_route", "English route"], ["highest_level", "Highest level"],
  ["previous_visa_refusal", "Previous visa refusal"], ["funding_source", "Funding"], ["submitted_at", "Submitted"],
  ["privacy_notice_version", "Privacy notice version"],
];

function show(v: unknown, countries?: Record<string, string>) {
  if (typeof v === "string" && countries && /^[A-Z]{2}$/.test(v) && countries[v]) return countries[v];
  if (v === true) return "Yes";
  if (v === false) return "No";
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return formatDateTime(v);
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) return formatDate(v);
  return String(v).replace(/_/g, " ");
}

/** Exception actions available to admissions and compliance: return for information, or reject. */
function ExceptionActions({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const { user } = useAuth();
  const [mode, setMode] = useState<"request_info" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const { summary, busy, run } = useAction();
  if (!user) return null;

  // LLD 7.1: both actions need the assessment open, so neither is offered while the record is
  // merely Submitted.
  // offer_pending included deliberately: a duplicate or a failed reference can surface after the
  // academic sign-off and before the offer is built, and the lifecycle allows both actions there.
  const canRequestInfo = ["admissions", "compliance", "senior", "admin"].includes(user.role)
    && ["academic_review", "offer_pending", "compliance_review"].includes(app.status);
  const canReject = ["admissions", "senior", "admin"].includes(user.role)
    && ["academic_review", "offer_pending"].includes(app.status);
  if (!canRequestInfo && !canReject) return null;

  const tooShort = reason.trim().length < 10;

  async function send(kind: "request_info" | "reject") {
    await run(
      () => api.post(`/applications/${app.id}/${kind === "request_info" ? "request-info" : "reject"}`, { reason }),
      async () => { setMode(null); setReason(""); await reload(); },
    );
  }

  return (
    <section className="card">
      <h2 className="h3">Other actions</h2>
      <ActionErrors summary={summary} />
      {mode === null ? (
        <div className="actions">
          {canRequestInfo && <button type="button" className="button button-secondary" onClick={() => setMode("request_info")}>Request more information</button>}
          {canReject && <button type="button" className="button button-secondary" onClick={() => setMode("reject")}>Reject application</button>}
        </div>
      ) : (
        <>
          <TextArea
            name="reason" required rows={4} maxLength={2000} value={reason} onChange={setReason}
            label={mode === "request_info" ? "What does the applicant need to provide?" : "Why is the application unsuccessful?"}
            hint="This reason is sent to the applicant and recorded in the audit trail."
            error={tooShort ? "Give a reason of at least 10 characters." : undefined}
          />
          {mode === "reject" ? (
            <Confirm
              title={`Reject application ${app.ref}?`}
              consequence={`${app.applicant_name ?? "The applicant"} will be told the application was unsuccessful. A rejected application cannot be reopened.`}
              confirmLabel="Reject application" busy={busy} disabled={tooShort}
              onConfirm={() => void send("reject")} onCancel={() => { setMode(null); setReason(""); }}
            />
          ) : (
            <div className="actions">
              <button type="button" className="button" disabled={busy || tooShort} onClick={() => void send("request_info")}>
                Return to the applicant
              </button>
              <button type="button" className="button button-secondary" onClick={() => { setMode(null); setReason(""); }}>Cancel</button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

const VERIFY_ROLES: Role[] = ["admissions", "compliance", "senior", "admin"];

/**
 * FR-24 — marking a document checked against its original.
 *
 * The evidence pack repeats whatever this records, which is why taking a verification back is a
 * visible action of its own rather than a quiet edit: a tick that could be removed unnoticed
 * would be worth nothing to a visiting officer.
 */
function VerifyControl({ doc, reload }: { doc: DocumentRow; reload: () => Promise<void> }) {
  const { user } = useAuth();
  const { summary, busy, run } = useAction();
  if (!user || !VERIFY_ROLES.includes(user.role)) {
    return doc.verified_at
      ? <span className="hint block">Verified {formatDateTime(doc.verified_at)}</span>
      : null;
  }
  if (doc.scan_status !== "clean" || doc.superseded_by !== null) return null;

  const act = (verified: boolean) =>
    void run(() => api.post(`/documents/${doc.id}/verify`, { verified }), reload);

  return (
    <span className="verify-control">
      {doc.verified_at ? (
        <>
          <span className="tag tag-clean">Verified</span>
          <span className="hint block">
            {doc.verified_by_name ?? "—"} · {formatDateTime(doc.verified_at)}{" "}
            <button type="button" className="link-button" disabled={busy} onClick={() => act(false)}>
              Remove verification
            </button>
          </span>
        </>
      ) : (
        <button type="button" className="button button-secondary small" disabled={busy} onClick={() => act(true)}>
          {busy ? "Saving…" : "Mark verified"}
        </button>
      )}
      <ActionErrors summary={summary} />
    </span>
  );
}

function ProgressPanel({ app }: { app: ApplicationRecord }) {
  const rows: [string, string][] = [];
  if (app.academic) {
    rows.push(["Academic assessment", app.academic.completed_at
      ? `${(app.academic.outcome ?? "").replace(/_/g, " ")} — ${app.academic.completed_by} on ${formatDateTime(app.academic.completed_at)}`
      : app.academic.id ? "In progress" : "Not started"]);
  }
  if (app.compliance) {
    rows.push(["Compliance review", app.compliance.completed_at
      ? `${RISK_LABELS[app.compliance.risk_rating ?? ""] ?? "—"} risk — ${app.compliance.completed_by} on ${formatDateTime(app.compliance.completed_at)}`
      : app.compliance.id ? "In progress" : "Not started"]);
  }
  if (app.offer) {
    const met = app.offer.conditions.filter((c) => c.status === "met").length;
    rows.push(["Offer", `${app.offer.offer_type} · ${app.offer.issued_at ? `issued ${formatDate(app.offer.issued_at.slice(0, 10))}` : "not issued"}`
      + (app.offer.conditions.length ? ` · conditions ${met}/${app.offer.conditions.length} met` : "")
      + (app.offer.response ? ` · ${app.offer.response}` : "")]);
  }
  if (app.fee_position) {
    rows.push(["Fees", `${money(app.fee_position.paid, app.fee_position.currency)} paid of `
      + `${money(app.fee_position.agreed_fee, app.fee_position.currency)} · deposit `
      + (app.fee_position.deposit_met ? "satisfied" : "not met")]);
  }
  if (app.cas) rows.push(["CAS", `${app.cas.cas_number} assigned ${formatDateTime(app.cas.assigned_at)} by ${app.cas.authorised_by ?? "—"}`]);
  if (app.student_record) rows.push(["Student record", app.student_record.enrolment_status.replace(/_/g, " ")]);
  if (!rows.length) return null;

  return (
    <section className="card">
      <h2 className="h3">Progress</h2>
      <dl className="kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    </section>
  );
}

export function StaffApplication() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const [app, setApp] = useState<ApplicationRecord | null>(null);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [auditFailed, setAuditFailed] = useState(false);
  const [dupes, setDupes] = useState<{ id: number; ref: string; status: string; full_name: string | null; matched_on: string[] }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [countries, setCountries] = useState<Record<string, string>>({});
  const canAudit = user ? CAN_SEE_AUDIT.includes(user.role) : false;

  useEffect(() => { void countryNames().then(setCountries); }, []);

  const load = useCallback(async () => {
    try {
      setApp(await api.get<ApplicationRecord>(`/applications/${id}`));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The record could not be loaded.");
    }
  }, [id]);

  useEffect(() => {
    void load();
    if (canAudit) {
      api.get<AuditRow[]>(`/applications/${id}/audit`).then((rows) => { setAudit(rows); setAuditFailed(false); })
        .catch(() => setAuditFailed(true));
      api.get<typeof dupes>(`/applications/${id}/duplicates`).then(setDupes).catch(() => setDupes(null));
    }
  }, [id, canAudit, load]);

  if (error) return <Layout><Alert kind="error">{error}</Alert></Layout>;
  if (!app) return <Layout><Spinner /></Layout>;

  const reload = async () => {
    await load();
    if (canAudit) {
      api.get<AuditRow[]>(`/applications/${id}/audit`).then((rows) => { setAudit(rows); setAuditFailed(false); })
        .catch(() => setAuditFailed(true));
    }
  };

  const tabs = [
    { key: "summary", label: "Summary" },
    ...(canAudit ? [{ key: "audit", label: "Audit trail" }] : []),
  ];
  const tab = tabs.some((t) => t.key === params.get("tab")) ? params.get("tab")! : "summary";

  return (
    <RecordShell app={app} title="Application">
      {dupes && dupes.length > 0 && (
        <Alert kind="warning" title="Possible duplicate applicant">
          <p>Review before progressing. Records are never merged automatically.</p>
          <ul>{dupes.slice(0, 5).map((d) => <li key={d.id}><Link to={`/staff/applications/${d.id}`}>{d.ref}</Link> — {d.full_name} ({STATUS_LABELS[d.status]}) · matched on {d.matched_on.join(", ").replace(/_/g, " ")}</li>)}</ul>
          {dupes.length > 5 && <p className="hint">and {dupes.length - 5} more.</p>}
        </Alert>
      )}

      {tabs.length > 1 && (
        <nav className="tabs" aria-label="Record sections">
          {tabs.map((t) => (
            <a key={t.key} href={`?tab=${t.key}`} aria-current={tab === t.key ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); setParams(t.key === "summary" ? {} : { tab: t.key }); }}>
              {t.label}
            </a>
          ))}
        </nav>
      )}

      {tab === "summary" && (<>
      <div className="record-grid">
        <section className="card">
          <h2 className="h3">Applicant details</h2>
          <dl className="kv">
            {SUMMARY_FIELDS.map(([k, label]) => (
              <div key={k}><dt>{label}</dt><dd>{show(app.data[k], countries)}</dd></div>
            ))}
          </dl>
        </section>
        <section className="card">
          <h2 className="h3">Documents</h2>
          {app.documents.length === 0 ? <p className="empty">No documents uploaded yet.</p> : (
            <ul className="doc-list">
              {app.documents.map((d) => (
                <li key={d.id}>
                  <span className="doc-cat">{CATEGORY_LABELS[d.category] ?? d.category}</span>
                  {d.scan_status === "clean" && user?.role !== "finance"
                    ? <button type="button" className="link-button" onClick={() => void downloadDocument(d)}>{d.filename}</button>
                    : <span>{d.filename}</span>}
                  <span className={`tag tag-${d.scan_status}`}>{d.scan_status}</span>
                  <span className="hint block">{formatBytes(d.size_bytes)} · {formatDateTime(d.uploaded_at)}</span>
                  <VerifyControl doc={d} reload={reload} />
                </li>
              ))}
            </ul>
          )}
          {app.outstanding_documents.length > 0 && <p className="hint">Outstanding: {app.outstanding_documents.map((c) => CATEGORY_LABELS[c]).join(", ")}</p>}
          {user && STAFF_UPLOAD_ROLES.includes(user.role) && <StaffUpload appId={app.id} reload={reload} />}
        </section>
      </div>

      <ProgressPanel app={app} />
      <ExceptionActions app={app} reload={reload} />
      </>)}

      {tab === "audit" && canAudit && (
        <section aria-labelledby="audit-h" className="audit">
          <h2 id="audit-h" className="h3">Audit trail</h2>
          <p className="hint">Every action on this record, oldest first. Entries cannot be edited or deleted.</p>
          {auditFailed ? <LoadFailed what="The audit trail" /> : !audit ? <Spinner /> : audit.length === 0 ? (
            <p className="empty">No entries.</p>
          ) : (
            <div className="table-wrap"><table className="table">
              <caption className="visually-hidden">Audit trail</caption>
              <thead><tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">Action</th><th scope="col">Detail</th></tr></thead>
              <tbody>
                {audit.map((a) => (
                  <tr key={a.id}>
                    <td className="nowrap">{formatDateTime(a.at)}</td>
                    <td>{a.user_name}<span className="hint block">{ROLE_LABELS[a.user_role as Role] ?? a.user_role}</span></td>
                    <td><code>{a.action}</code></td>
                    <td>{a.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </section>
      )}
    </RecordShell>
  );
}

const STAFF_UPLOAD_ROLES: Role[] = ["admissions", "compliance", "senior", "admin"];
const STAFF_UPLOAD_CATEGORIES = ["interview_notes", "qualification", "english", "financial", "passport", "other"];

/** Staff add evidence to an application — interview notes above all (FR-37, prototype screen 6). */
function StaffUpload({ appId, reload }: { appId: number; reload: () => Promise<void> }) {
  const [category, setCategory] = useState("interview_notes");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) { setError("Choose a file to upload."); return; }
    setBusy(true); setError(null); setDone(null);
    const form = new FormData();
    form.append("category", category);
    form.append("file", file);
    try {
      await api.upload(`/applications/${appId}/documents`, form);
      setDone(`${CATEGORY_LABELS[category]} uploaded and scanned.`);
      setFile(null);
      (e.target as HTMLFormElement).reset();
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The upload failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={upload} noValidate className="subform inline-upload">
      <h3 className="h4">Add a document</h3>
      {error && <Alert kind="error">{error}</Alert>}
      {done && <Alert kind="success">{done}</Alert>}
      <SelectField name="staff_category" label="Document type" required value={category} onChange={setCategory}
        options={STAFF_UPLOAD_CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABELS[c] ?? c }))} />
      <label htmlFor="staff-file" className="label">File <span className="optional">(PDF, JPG or PNG, up to 5MB)</span></label>
      <input id="staff-file" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
        onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      <button className="button button-secondary" type="submit" disabled={busy || !file}>{busy ? "Uploading and scanning…" : "Upload"}</button>
    </form>
  );
}
