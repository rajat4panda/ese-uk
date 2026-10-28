import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  CATEGORY_LABELS, RISK_LABELS, api, downloadCasRegister, formatDate, formatDateTime,
  type AllocationPosition, type ApplicationRecord, type CasRecord, type PreCasSummary,
} from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, Layout, Spinner, TextArea, TextField, Tick } from "../components/ui";
import { ActionErrors, BlockedAction, Confirm, LoadFailed, RecordPage, money, useAction } from "../components/workflow";

function Tile({ title, ok, children }: { title: string; ok: boolean; children: React.ReactNode }) {
  return (
    <section className={`tile tile-${ok ? "ok" : "todo"}`}>
      <h3 className="h4">
        {title} <span className={`tag tag-${ok ? "clean" : "pending"}`}>{ok ? "Satisfied" : "Outstanding"}</span>
      </h3>
      {children}
    </section>
  );
}

function usePreCas(applicationId: number | string) {
  const [summary, setSummary] = useState<PreCasSummary | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    api.get<PreCasSummary>(`/applications/${applicationId}/precas`)
      .then((s) => { setSummary(s); setFailed(false); })
      .catch(() => setFailed(true));
  }, [applicationId]);
  return { summary, failed };
}

// ---------------------------------------------------------------------------------------------
// Pre-CAS summary (FR-48, FR-49)
// ---------------------------------------------------------------------------------------------
function PreCasView({ app }: { app: ApplicationRecord }) {
  const { summary, failed } = usePreCas(app.id);
  const { user } = useAuth();
  const senior = user?.role === "senior" || user?.role === "admin";
  if (failed) return <LoadFailed what="The pre-CAS summary" />;
  if (!summary) return <Spinner />;

  return (
    <>
      {summary.ready_for_cas ? (
        <Alert kind="success" title="Ready for CAS assignment">
          <p>
            Every pre-CAS control is satisfied.
            {summary.assignment.can_assign
              ? " You can assign the CAS."
              : " It must be assigned by a senior user who did not complete either review."}
          </p>
        </Alert>
      ) : (
        <Alert kind="warning" title={`${summary.blockers.length} item(s) block CAS assignment`}>
          <ul>{summary.blockers.map((b, i) => <li key={i}>{b}</li>)}</ul>
        </Alert>
      )}

      <div className="tiles">
        <Tile title="Academic assessment" ok={summary.academic.complete}>
          <p>{summary.academic.outcome ? summary.academic.outcome.replace(/_/g, " ") : "Not started"}</p>
          {summary.academic.completed_by && (
            <p className="hint">{summary.academic.completed_by} · {formatDateTime(summary.academic.completed_at!)}</p>
          )}
        </Tile>
        <Tile title="Compliance review" ok={summary.compliance.complete}>
          <p>Risk: {RISK_LABELS[summary.compliance.risk_rating ?? ""] ?? "not rated"}
            {summary.compliance.atas_required ? " · ATAS required" : ""}</p>
          {summary.compliance.completed_by && (
            <p className="hint">{summary.compliance.completed_by} · {formatDateTime(summary.compliance.completed_at!)}</p>
          )}
          {summary.compliance.second_line_at && (
            <p className="hint">Second line: {formatDateTime(summary.compliance.second_line_at)}</p>
          )}
        </Tile>
        <Tile title="Documents" ok={summary.documents.complete}>
          {summary.documents.complete
            ? <p>All required documents held.</p>
            : (
              <>
                <ul>{summary.documents.outstanding.map((d) => <li key={d}>{CATEGORY_LABELS[d] ?? d.replace(/_/g, " ")}</li>)}</ul>
                <p className="hint">Document completeness is enforced through the compliance checklist, so this on its own does not block a CAS.</p>
              </>
            )}
        </Tile>
        <Tile title="Offer conditions" ok={summary.conditions.outstanding.length === 0 || summary.conditions.overridden}>
          {summary.conditions.overridden && <p>Overridden by a senior user.</p>}
          {summary.conditions.outstanding.length === 0
            ? <p>No outstanding conditions.</p>
            : <ul>{summary.conditions.outstanding.map((c, i) => <li key={i}>{c}</li>)}</ul>}
        </Tile>
        <Tile title="Fees" ok={summary.fees.deposit_met}>
          <dl className="kv compact">
            <div><dt>Agreed fee</dt><dd>{money(summary.fees.agreed_fee)}</dd></div>
            <div><dt>Minimum deposit</dt><dd>{money(summary.fees.minimum_deposit)}</dd></div>
            <div><dt>Paid</dt><dd>{money(summary.fees.paid)}</dd></div>
            <div><dt>Outstanding</dt><dd>{money(summary.fees.outstanding)}</dd></div>
          </dl>
        </Tile>
        <Tile title="Course" ok={Boolean(summary.course.programme && summary.course.start_date)}>
          <p>{summary.course.programme ?? "No programme"}</p>
          <p className="hint">{summary.course.intake ?? "No intake"} · starts {formatDate(summary.course.start_date)}</p>
        </Tile>
      </div>

      {app.cas ? (
        <Alert kind="success" title={`CAS ${app.cas.cas_number} assigned`}>
          <p>Assigned {formatDateTime(app.cas.assigned_at)} by {app.cas.authorised_by ?? "—"}. <Link to="/staff/cas">CAS register</Link></p>
        </Alert>
      ) : senior ? (
        summary.assignment.can_assign ? (
          <p className="actions"><Link className="button" to={`/staff/applications/${app.id}/cas`}>Assign CAS</Link></p>
        ) : (
          <BlockedAction label="Assign CAS" reasons={summary.assignment.reasons} />
        )
      ) : (
        <p className="hint">Only a senior user can assign a CAS.</p>
      )}
    </>
  );
}

export function PreCas() {
  return <RecordPage title="Pre-CAS summary" render={(app) => <PreCasView app={app} />} />;
}

// ---------------------------------------------------------------------------------------------
// CAS assignment (FR-53 to FR-58) — the most consequential action in the system
// ---------------------------------------------------------------------------------------------
function CasForm({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const { summary } = usePreCas(app.id);
  const nav = useNavigate();
  const { user } = useAuth();
  const [form, setForm] = useState({
    cas_number: "", course_title: "", start_date: "", end_date: "",
    tuition_fee: "", amount_paid: "", level1_user: "", notes: "",
  });
  const [confirming, setConfirming] = useState(false);
  const [depositReason, setDepositReason] = useState("");
  const { error, summary: errors, busy, run } = useAction();

  useEffect(() => {
    if (!summary) return;
    // FR-52: the ledger supplies the opening figures, but what is stored is what the Level 1 User
    // confirms was entered on the CAS. The two can differ legitimately — a part payment recorded
    // after the CAS was created, a fee agreed outside the schedule — and the CAS is what UKVI checks.
    setForm((f) => ({
      ...f,
      course_title: f.course_title || summary.course.programme || "",
      start_date: f.start_date || summary.course.start_date || "",
      tuition_fee: f.tuition_fee || summary.fees.agreed_fee || "",
      amount_paid: f.amount_paid || summary.fees.paid || "",
    }));
  }, [summary]);

  // Prototype screen 10: the CAS number is the whole point of the screen, so it is the largest
  // thing on it, with the dates it commits ESE to underneath.
  if (app.cas) {
    return (
      <div className="confirmation">
        <Tick />
        <h2 style={{ color: "var(--green)", fontSize: "20px", margin: "14px 0 6px" }}>CAS assigned</h2>
        <p className="hint">Assigned {formatDateTime(app.cas.assigned_at)} by {app.cas.authorised_by ?? "—"}.</p>
        <div className="detail-box">
          <dl className="kv compact">
            <div><dt>CAS number</dt><dd><strong className="ref-large">{app.cas.cas_number}</strong></dd></div>
            <div><dt>Applicant</dt><dd>{app.applicant_name ?? "—"}</dd></div>
            <div><dt>Course</dt><dd>{app.cas.course_title}</dd></div>
            <div><dt>Start date</dt><dd>{formatDate(app.cas.start_date)}</dd></div>
            <div><dt>End date</dt><dd>{formatDate(app.cas.end_date)}</dd></div>
            <div><dt>Tuition fee</dt><dd>{money(app.cas.tuition_fee)}</dd></div>
            <div><dt>Amount paid</dt><dd>{money(app.cas.amount_paid)}</dd></div>
            <div><dt>Level 1 user</dt><dd>{app.cas.level1_user}</dd></div>
          </dl>
        </div>
        <p>
          <Link to={`/staff/students?q=${encodeURIComponent(app.ref)}`}>View student record</Link>
          {" · "}
          <Link to="/staff/cas">Back to CAS management</Link>
        </p>
        <p className="hint">
          A CAS is assigned once. To correct it, withdraw the CAS with the Home Office and record the
          change here.
        </p>
      </div>
    );
  }

  const set = (k: keyof typeof form) => (v: string) => setForm({ ...form, [k]: v });
  const complete = form.cas_number && form.course_title && form.start_date && form.end_date
    && form.tuition_fee && form.level1_user;
  const feeNum = Number(form.tuition_fee);
  const paidNum = Number(form.amount_paid || 0);
  const feeOk = form.tuition_fee !== "" && Number.isFinite(feeNum) && feeNum > 0;
  const paidOk = Number.isFinite(paidNum) && paidNum >= 0 && (!feeOk || paidNum <= feeNum);
  const allocationBlocked = error?.code === "ERR_ALLOCATION_EXCEEDED";
  const depositBlocked = error?.code === "ERR_DEPOSIT_NOT_MET";
  const senior = user?.role === "senior" || user?.role === "admin";
  const casNumberOk = /^[A-Z0-9]{10,20}$/.test(form.cas_number);

  async function assign(allocationOverride = false) {
    await run(
      () => api.post(`/applications/${app.id}/cas`, {
        ...form,
        allocation_override_confirmed: allocationOverride,
        deposit_override_reason: depositReason.trim() || null,
      }),
      async () => { setConfirming(false); await reload(); nav(`/staff/applications/${app.id}/precas`); },
    );
  }

  return (
    <>
      {!confirming && <ActionErrors summary={errors} />}
      {summary && !summary.assignment.can_assign && (
        <Alert kind="warning" title="Pre-CAS controls are not all satisfied">
          <ul>{summary.assignment.reasons.map((b, i) => <li key={i}>{b}</li>)}</ul>
          <p>The assignment will be refused unless these are cleared. <Link to={`/staff/applications/${app.id}/precas`}>Open the pre-CAS summary</Link>.</p>
        </Alert>
      )}
      <section className="card">
        <h2 className="h3">CAS details</h2>
        <p className="hint">
          These details are recorded against the sponsor licence. They must match the CAS you create on the
          Sponsorship Management System exactly.
        </p>
        <TextField
          name="cas_number" label="CAS number" required width="m" value={form.cas_number}
          onChange={(v) => set("cas_number")(v.toUpperCase())}
          hint="10 to 20 characters, letters and numbers, as shown on the SMS."
          error={form.cas_number && !casNumberOk ? "Enter the CAS number exactly as issued (10–20 letters and numbers)." : undefined}
        />
        <TextField name="course_title" label="Course title" required value={form.course_title} onChange={set("course_title")} />
        <DateField name="start_date" label="Course start date" required value={form.start_date} onChange={set("start_date")} />
        <DateField name="end_date" label="Course end date" required value={form.end_date} onChange={set("end_date")} />
        <TextField
          name="tuition_fee" label="Tuition fee on the CAS" required width="s" inputMode="decimal"
          value={form.tuition_fee} onChange={set("tuition_fee")}
          hint="In pounds, exactly as entered on the SMS. The fee schedule is offered as a starting point."
          error={form.tuition_fee !== "" && !feeOk ? "Enter the tuition fee as a number greater than zero." : undefined}
        />
        <TextField
          name="amount_paid" label="Amount paid on the CAS" required width="s" inputMode="decimal"
          value={form.amount_paid} onChange={set("amount_paid")}
          hint="What the CAS declares has been paid, which may differ from the ledger."
          error={!paidOk ? "The amount paid cannot be more than the tuition fee." : undefined}
        />
        <TextField name="level1_user" label="Level 1 user who created the CAS" required value={form.level1_user} onChange={set("level1_user")} />
        <TextArea name="notes" label="Notes" rows={3} maxLength={2000} value={form.notes} onChange={set("notes")} />
        <div className="actions">
          <button
            type="button" className="button" disabled={busy || !complete || !casNumberOk || !feeOk || !paidOk}
            onClick={() => setConfirming(true)}
          >
            Assign CAS
          </button>
          <Link className="button button-secondary" to={`/staff/applications/${app.id}/precas`}>Back to pre-CAS summary</Link>
        </div>
      </section>

      {confirming && (
        <Confirm
          title={`Assign CAS ${form.cas_number} to ${app.ref}?`}
          consequence={`This records a Confirmation of Acceptance for Studies for ${app.applicant_name ?? "this applicant"} against the ESE sponsor licence, creates their student record, and cannot be undone from this system. Your name is recorded as the authorising officer.`}
          confirmLabel={allocationBlocked ? "Confirm and assign anyway" : "Assign CAS"}
          busy={busy}
          disabled={(depositBlocked || Boolean(depositReason)) && depositReason.trim().length < 10}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void assign(allocationBlocked)}
        >
          {allocationBlocked && (
            <Alert kind="warning" title="This exceeds the recorded CAS allocation">
              <p>{error?.message}</p>
              <p>Confirming records the over-allocation in the audit trail.</p>
            </Alert>
          )}
          {(depositBlocked || depositReason) && (
            senior ? (
              <>
                <Alert kind="warning" title="The tuition deposit gate is not satisfied">
                  <p>{error?.message}</p>
                </Alert>
                <TextArea
                  name="deposit_override_reason" label="Reason for overriding the deposit requirement" required rows={3}
                  maxLength={1000} value={depositReason} onChange={setDepositReason}
                  hint="Recorded in the audit trail against your name. Use only where the deposit is evidenced elsewhere."
                  error={depositReason.trim().length < 10 ? "Give a reason of at least 10 characters." : undefined}
                />
              </>
            ) : (
              <Alert kind="error" title="The tuition deposit gate is not satisfied">
                <p>{error?.message} Only a senior user can override this.</p>
              </Alert>
            )
          )}
          {errors.length > 0 && !allocationBlocked && !depositBlocked && <ActionErrors summary={errors} />}
          <dl className="kv compact">
            <div><dt>Course</dt><dd>{form.course_title}</dd></div>
            <div><dt>Dates</dt><dd>{formatDate(form.start_date)} to {formatDate(form.end_date)}</dd></div>
            <div><dt>Level 1 user</dt><dd>{form.level1_user}</dd></div>
          </dl>
        </Confirm>
      )}
    </>
  );
}

export function CasApproval() {
  return <RecordPage title="Assign CAS" render={(app, reload) => <CasForm app={app} reload={reload} />} />;
}

// ---------------------------------------------------------------------------------------------
// CAS register and allocation (FR-59, FR-60)
// ---------------------------------------------------------------------------------------------
function AllocationPanel({ allocation, onSaved }: { allocation: AllocationPosition; onSaved: () => void }) {
  const { user } = useAuth();
  const senior = user?.role === "senior" || user?.role === "admin";
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ period_label: "", period_start: "", period_end: "", allocated_count: "" });
  const { summary, busy, run } = useAction();
  const set = (k: keyof typeof form) => (v: string) => setForm({ ...form, [k]: v });

  return (
    <section className="card allocation">
      <h2 className="h3">CAS allocation</h2>
      {allocation.period_label ? (
        <>
          <p>
            <strong>{allocation.assigned} of {allocation.allocated}</strong> assigned for {allocation.period_label}
            {" "}({formatDate(allocation.period_start)} to {formatDate(allocation.period_end)}).
          </p>
          <div className="meter" role="img" aria-label={`${allocation.assigned} of ${allocation.allocated} CAS assigned`}>
            <span style={{ width: `${allocation.allocated ? Math.min(100, (allocation.assigned / allocation.allocated) * 100) : 0}%` }} />
          </div>
          <p className={allocation.remaining <= 0 ? "warning-text" : "hint"}>
            {allocation.remaining > 0 ? `${allocation.remaining} remaining.` : "The allocation is fully used. Further assignments need a second confirmation."}
          </p>
        </>
      ) : (
        <p className="hint">No allocation has been recorded for the current period, so over-allocation cannot be warned about.</p>
      )}
      {senior && (open ? (
        <>
          <ActionErrors summary={summary} />
          <TextField name="period_label" label="Period" required width="m" value={form.period_label} onChange={set("period_label")} hint="For example 2026/27" />
          <DateField name="period_start" label="Period start" required value={form.period_start} onChange={set("period_start")} />
          <DateField name="period_end" label="Period end" required value={form.period_end} onChange={set("period_end")} />
          <TextField name="allocated_count" label="CAS allocated" required width="s" inputMode="numeric" value={form.allocated_count} onChange={set("allocated_count")} />
          <div className="actions">
            <button type="button" className="button" disabled={busy}
              onClick={() => void run(
                () => api.post("/admin/allocations", { ...form, allocated_count: Number(form.allocated_count || 0) }),
                () => { setOpen(false); onSaved(); },
              )}>
              Save allocation
            </button>
            <button type="button" className="button button-secondary" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </>
      ) : (
        <button type="button" className="link-button" onClick={() => setOpen(true)}>Record or change the allocation</button>
      ))}
    </section>
  );
}

export function CasRegister() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [data, setData] = useState<{ items: CasRecord[]; total: number; allocation: AllocationPosition } | null>(null);
  const [failed, setFailed] = useState(false);
  const [exporting, setExporting] = useState(false);
  const page = Number(params.get("page") ?? 1);

  function load() {
    const qs = new URLSearchParams();
    if (params.get("q")) qs.set("q", params.get("q")!);
    qs.set("page", String(page));
    api.get<typeof data>(`/cas?${qs}`).then((d) => { setData(d); setFailed(false); }).catch(() => setFailed(true));
  }

  useEffect(load, [params, page]);

  function search(e: FormEvent) {
    e.preventDefault();
    const next = new URLSearchParams(params);
    if (q) next.set("q", q); else next.delete("q");
    next.delete("page");
    setParams(next);
  }

  return (
    <Layout>
      <div className="page-head">
        <h1>CAS register</h1>
        <button type="button" className="button button-secondary" disabled={exporting}
          onClick={() => { setExporting(true); void downloadCasRegister(params.get("q") ?? "").finally(() => setExporting(false)); }}>
          {exporting ? "Preparing…" : "Export as CSV"}
        </button>
      </div>
      <p className="hint">Every CAS assigned through this system, most recent first. Entries cannot be edited or deleted.</p>
      {failed ? <LoadFailed what="The CAS register" /> : !data ? <Spinner /> : (
        <>
          <AllocationPanel allocation={data.allocation} onSaved={load} />
          <form className="filters" onSubmit={search} role="search">
            <TextField name="q" label="Search" hint="CAS number, application reference or applicant name" value={q} onChange={setQ} width="m" />
            <button className="button" type="submit">Search</button>
          </form>
          {data.items.length === 0 ? <p className="empty">No CAS records yet.</p> : (
            <table className="table">
              <caption className="visually-hidden">CAS register</caption>
              <thead>
                <tr>
                  <th scope="col">CAS number</th><th scope="col">Applicant</th><th scope="col">Course</th>
                  <th scope="col">Dates</th><th scope="col">Fee / paid</th><th scope="col">Assigned</th><th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((c) => (
                  <tr key={c.id}>
                    <td><code>{c.cas_number}</code></td>
                    <td>{c.applicant_name ?? "—"}<span className="hint block">{c.application_ref}</span></td>
                    <td>{c.course_title}</td>
                    <td className="nowrap">{formatDate(c.start_date)}<span className="hint block">to {formatDate(c.end_date)}</span></td>
                    <td className="nowrap">{money(c.tuition_fee)}<span className="hint block">paid {money(c.amount_paid)}</span></td>
                    <td className="nowrap">{formatDateTime(c.assigned_at)}<span className="hint block">{c.authorised_by ?? "—"}</span></td>
                    <td><span className="tag tag-clean">{c.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <nav className="pager" aria-label="Pages">
            <span>{data.total} CAS record{data.total === 1 ? "" : "s"}</span>
            {page > 1 && <button type="button" className="link-button" onClick={() => { const n = new URLSearchParams(params); n.set("page", String(page - 1)); setParams(n); }}>Previous</button>}
            {page * 25 < data.total && <button type="button" className="link-button" onClick={() => { const n = new URLSearchParams(params); n.set("page", String(page + 1)); setParams(n); }}>Next</button>}
          </nav>
        </>
      )}
    </Layout>
  );
}
