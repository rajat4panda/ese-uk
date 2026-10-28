import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, formatDate, formatDateTime } from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, Layout, SelectField, Spinner, TextField } from "../components/ui";
import { LoadFailed } from "../components/workflow";

/**
 * Settings — prototype screen 15's "Settings" item (BRD FR-105).
 *
 * Compliance thresholds, CAS allocation, programmes, intakes and fees, changed here rather than with
 * SQL. Every save is written to the audit trail with the old and new value, and says so. Finance
 * sees the fees section only; the Authorising Officer and administrators see everything.
 */

interface Setting {
  key: string; group: string; label: string; hint: string; type: "int" | "decimal" | "bool" | "text" | "email";
  unit: string; minimum: string | null; maximum: string | null; value: string; is_default: boolean;
  updated_by: string | null; updated_at: string | null; ao_only: boolean;
}
interface Programme { id: number; title: string; qualification_level: string; duration_months: number; atas_required: boolean; status: "active" | "inactive" }
interface Intake { id: number; label: string; start_date: string; application_deadline: string; status: "open" | "closed" }
interface Fee { programme_id: number; programme: string; intake_id: number; intake: string; tuition_fee: string | null; minimum_deposit: string | null; currency: string; effective_from: string | null }
interface Allocation { id: number; period_label: string; period_start: string; period_end: string; allocated: number; assigned: number; remaining: number }

const SECTIONS = [
  { key: "thresholds", label: "Compliance settings", roles: ["senior", "admin"] },
  { key: "allocation", label: "CAS allocation", roles: ["senior", "admin"] },
  { key: "programmes", label: "Programmes and intakes", roles: ["senior", "admin"] },
  { key: "fees", label: "Fees", roles: ["finance", "senior", "admin"] },
] as const;

function message(err: unknown): string {
  if (err instanceof ApiError) return err.details[0]?.message ?? err.message;
  return "The change could not be saved.";
}

export function Settings() {
  const { user } = useAuth();
  const visible = SECTIONS.filter((s) => user && (s.roles as readonly string[]).includes(user.role));
  const [section, setSection] = useState<string>(visible[0]?.key ?? "fees");
  const [notice, setNotice] = useState<string | null>(null);
  const saved = (text: string) => setNotice(`${text} Recorded in the audit trail.`);

  return (
    <Layout wide>
      <h1>Settings</h1>
      <p className="hint">Changes take effect immediately and are recorded in the audit trail with the previous value.</p>
      {visible.length > 1 && (
        <nav className="tabs" aria-label="Settings sections">
          {visible.map((s) => (
            <a key={s.key} href={`#${s.key}`} aria-current={section === s.key ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); setSection(s.key); setNotice(null); }}>{s.label}</a>
          ))}
        </nav>
      )}
      {notice && <Alert kind="success">{notice}</Alert>}
      {section === "thresholds" && <Thresholds onSaved={saved} />}
      {section === "allocation" && <Allocations onSaved={saved} />}
      {section === "programmes" && <ProgrammesAndIntakes onSaved={saved} />}
      {section === "fees" && <Fees onSaved={saved} />}
    </Layout>
  );
}

// --- Compliance settings ---------------------------------------------------------------------------
function Thresholds({ onSaved }: { onSaved: (t: string) => void }) {
  const { user } = useAuth();
  const [rows, setRows] = useState<Setting[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.get<Setting[]>("/admin/settings").then(setRows).catch(() => setFailed(true)); }, []);
  if (failed) return <LoadFailed what="Settings" />;
  if (!rows) return <Spinner />;

  async function save(s: Setting) {
    setBusy(true); setError(null);
    try {
      const updated = await api.put<Setting>(`/admin/settings/${s.key}`, { value });
      setRows(rows!.map((r) => (r.key === s.key ? updated : r)));
      setEditing(null);
      onSaved(`${s.label} changed from ${show(s, s.value)} to ${show(s, updated.value)}.`);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const groups = [...new Set(rows.map((r) => r.group))];
  return (
    <>
      {groups.map((g) => (
        <section className="card" key={g} aria-labelledby={`g-${g}`}>
          <h2 className="h3" id={`g-${g}`}>{g}</h2>
          <dl className="kv settings-list">
            {rows.filter((r) => r.group === g).map((s) => (
              <div key={s.key}>
                <dt>
                  {s.label}
                  <span className="hint block">{s.hint}</span>
                </dt>
                <dd>
                  {editing === s.key ? (
                    <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); void save(s); }}>
                      {error && <Alert kind="error">{error}</Alert>}
                      {s.type === "bool" ? (
                        <SelectField name={s.key} label={s.label} hideOptional value={value} onChange={setValue}
                          options={[{ value: "true", label: "On" }, { value: "false", label: "Off" }]} />
                      ) : (
                        <TextField name={s.key} label={s.label} hideOptional value={value} onChange={setValue}
                          width={s.type === "email" || s.type === "text" ? "m" : "s"}
                          inputMode={s.type === "int" ? "numeric" : s.type === "decimal" ? "decimal" : undefined}
                          hint={s.minimum !== null ? `From ${s.minimum} to ${s.maximum}${s.unit ? ` ${s.unit}` : ""}.` : undefined} />
                      )}
                      <div className="actions">
                        <button className="button" type="submit" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
                        <button className="button button-secondary" type="button" onClick={() => { setEditing(null); setError(null); }}>Cancel</button>
                      </div>
                    </form>
                  ) : (
                    <>
                      <b>{show(s, s.value)}</b>{" "}
                      {s.ao_only && user?.role !== "senior" ? (
                        <span className="hint">Only the Authorising Officer can change this.</span>
                      ) : (
                        <button type="button" className="link-button" onClick={() => { setEditing(s.key); setValue(s.value); setError(null); }}>
                          Change<span className="visually-hidden"> {s.label}</span>
                        </button>
                      )}
                      <span className="hint block">
                        {s.updated_by ? `Changed by ${s.updated_by}, ${formatDateTime(s.updated_at!)}` : s.is_default ? "Default value" : "As installed"}
                      </span>
                    </>
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </>
  );
}

function show(s: Setting, v: string): string {
  if (s.type === "bool") return v === "true" ? "On" : "Off";
  return s.unit ? `${v} ${s.unit}`.replace(" %", "%") : v;
}

// --- CAS allocation -------------------------------------------------------------------------------
function Allocations({ onSaved }: { onSaved: (t: string) => void }) {
  const [rows, setRows] = useState<Allocation[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [form, setForm] = useState({ period_label: "", period_start: "", period_end: "", allocated_count: "" });
  const [error, setError] = useState<string | null>(null);
  const load = () => api.get<Allocation[]>("/admin/allocations").then(setRows).catch(() => setFailed(true));
  useEffect(() => { void load(); }, []);
  if (failed) return <LoadFailed what="CAS allocations" />;
  if (!rows) return <Spinner />;

  async function save(e: FormEvent) {
    e.preventDefault(); setError(null);
    try {
      await api.post("/admin/allocations", { ...form, allocated_count: Number(form.allocated_count) });
      onSaved(`CAS allocation for ${form.period_label} saved.`);
      setForm({ period_label: "", period_start: "", period_end: "", allocated_count: "" });
      await load();
    } catch (err) { setError(message(err)); }
  }

  return (
    <>
      <section className="card">
        <h2 className="h3">Allocation periods</h2>
        {rows.length === 0 ? <p className="empty">No allocation is recorded. CAS cannot be counted against one until it is.</p> : (
          <table className="table">
            <thead><tr><th scope="col">Period</th><th scope="col">Dates</th><th scope="col">Allocated</th><th scope="col">Assigned</th><th scope="col">Remaining</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><b>{r.period_label}</b></td>
                  <td className="nowrap">{formatDate(r.period_start)} – {formatDate(r.period_end)}</td>
                  <td>{r.allocated}</td><td>{r.assigned}</td>
                  <td><span className={`tag ${r.remaining <= 0 ? "tag-overdue" : "tag-clean"}`}>{r.remaining}</span></td>
                  <td><button type="button" className="link-button" onClick={() => setForm({ period_label: r.period_label, period_start: r.period_start, period_end: r.period_end, allocated_count: String(r.allocated) })}>Change<span className="visually-hidden"> {r.period_label}</span></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card narrow-card">
        <h2 className="h3">Record an allocation</h2>
        <p className="hint">Enter the figure UKVI granted for the period. Saving an existing period's label updates it.</p>
        {error && <Alert kind="error">{error}</Alert>}
        <form onSubmit={save} noValidate>
          <TextField name="period_label" label="Period" required hint="For example 2026/27" width="s" value={form.period_label} onChange={(v) => setForm({ ...form, period_label: v })} />
          <DateField name="period_start" label="Start" required value={form.period_start} onChange={(v) => setForm({ ...form, period_start: v })} />
          <DateField name="period_end" label="End" required value={form.period_end} onChange={(v) => setForm({ ...form, period_end: v })} />
          <TextField name="allocated_count" label="CAS allocated" required width="s" inputMode="numeric" value={form.allocated_count} onChange={(v) => setForm({ ...form, allocated_count: v })} />
          <button className="button" type="submit">Save allocation</button>
        </form>
      </section>
    </>
  );
}

// --- Programmes and intakes ----------------------------------------------------------------------------
function ProgrammesAndIntakes({ onSaved }: { onSaved: (t: string) => void }) {
  const [programmes, setProgrammes] = useState<Programme[] | null>(null);
  const [intakes, setIntakes] = useState<Intake[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emptyP = { id: 0, title: "", qualification_level: "", duration_months: "", atas_required: "false" };
  const emptyI = { id: 0, label: "", start_date: "", application_deadline: "" };
  const [p, setP] = useState(emptyP);
  const [i, setI] = useState(emptyI);

  const load = () => Promise.all([api.get<Programme[]>("/admin/programmes"), api.get<Intake[]>("/admin/intakes")])
    .then(([a, b]) => { setProgrammes(a); setIntakes(b); }).catch(() => setFailed(true));
  useEffect(() => { void load(); }, []);
  if (failed) return <LoadFailed what="Programmes and intakes" />;
  if (!programmes || !intakes) return <Spinner />;

  async function run(action: () => Promise<unknown>, done: string) {
    setError(null);
    try { await action(); onSaved(done); await load(); return true; } catch (err) { setError(message(err)); return false; }
  }
  async function saveProgramme(e: FormEvent) {
    e.preventDefault();
    const body = { title: p.title, qualification_level: p.qualification_level, duration_months: Number(p.duration_months), atas_required: p.atas_required === "true" };
    if (await run(() => (p.id ? api.patch(`/admin/programmes/${p.id}`, body) : api.post("/admin/programmes", body)), `Programme ${p.title} saved.`)) setP(emptyP);
  }
  async function saveIntake(e: FormEvent) {
    e.preventDefault();
    const body = { label: i.label, start_date: i.start_date, application_deadline: i.application_deadline };
    if (await run(() => (i.id ? api.patch(`/admin/intakes/${i.id}`, body) : api.post("/admin/intakes", body)), `Intake ${i.label} saved.`)) setI(emptyI);
  }

  return (
    <>
      {error && <Alert kind="error">{error}</Alert>}
      <section className="card">
        <h2 className="h3">Programmes</h2>
        <p className="hint">Only active programmes are offered on the application form. Withdrawing one does not affect applications already made.</p>
        <table className="table">
          <thead><tr><th scope="col">Programme</th><th scope="col">Level</th><th scope="col">Duration</th><th scope="col">ATAS</th><th scope="col">Status</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            {programmes.map((r) => (
              <tr key={r.id}>
                <td><b>{r.title}</b></td><td>{r.qualification_level}</td><td>{r.duration_months} months</td><td>{r.atas_required ? "Required" : "—"}</td>
                <td><span className={`tag tag-${r.status === "active" ? "active" : "deactivated"}`}>{r.status}</span></td>
                <td className="nowrap">
                  <button type="button" className="link-button" onClick={() => setP({ id: r.id, title: r.title, qualification_level: r.qualification_level, duration_months: String(r.duration_months), atas_required: String(r.atas_required) })}>Edit<span className="visually-hidden"> {r.title}</span></button>
                  {" · "}
                  <button type="button" className="link-button" onClick={() => void run(() => api.patch(`/admin/programmes/${r.id}`, { status: r.status === "active" ? "inactive" : "active" }), `${r.title} ${r.status === "active" ? "withdrawn" : "offered again"}.`)}>
                    {r.status === "active" ? "Withdraw" : "Offer again"}<span className="visually-hidden"> {r.title}</span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form onSubmit={saveProgramme} noValidate className="subform">
          <h3 className="h4">{p.id ? `Edit ${p.title}` : "Add a programme"}</h3>
          <TextField name="p_title" label="Title" required value={p.title} onChange={(v) => setP({ ...p, title: v })} />
          <TextField name="p_level" label="Qualification level" required width="m" hint="For example RQF Level 7" value={p.qualification_level} onChange={(v) => setP({ ...p, qualification_level: v })} />
          <TextField name="p_duration" label="Duration (months)" required width="s" inputMode="numeric" value={p.duration_months} onChange={(v) => setP({ ...p, duration_months: v })} />
          <SelectField name="p_atas" label="ATAS certificate required" required value={p.atas_required} onChange={(v) => setP({ ...p, atas_required: v })}
            options={[{ value: "false", label: "No" }, { value: "true", label: "Yes" }]} />
          <div className="actions">
            <button className="button" type="submit">{p.id ? "Save programme" : "Add programme"}</button>
            {p.id !== 0 && <button className="button button-secondary" type="button" onClick={() => setP(emptyP)}>Cancel</button>}
          </div>
        </form>
      </section>

      <section className="card">
        <h2 className="h3">Intakes</h2>
        <table className="table">
          <thead><tr><th scope="col">Intake</th><th scope="col">Starts</th><th scope="col">Application deadline</th><th scope="col">Status</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            {intakes.map((r) => (
              <tr key={r.id}>
                <td><b>{r.label}</b></td><td>{formatDate(r.start_date)}</td><td>{formatDate(r.application_deadline)}</td>
                <td><span className={`tag tag-${r.status === "open" ? "active" : "deactivated"}`}>{r.status}</span></td>
                <td className="nowrap">
                  <button type="button" className="link-button" onClick={() => setI({ id: r.id, label: r.label, start_date: r.start_date, application_deadline: r.application_deadline })}>Edit<span className="visually-hidden"> {r.label}</span></button>
                  {" · "}
                  <button type="button" className="link-button" onClick={() => void run(() => api.patch(`/admin/intakes/${r.id}`, { status: r.status === "open" ? "closed" : "open" }), `${r.label} ${r.status === "open" ? "closed" : "reopened"}.`)}>
                    {r.status === "open" ? "Close" : "Reopen"}<span className="visually-hidden"> {r.label}</span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form onSubmit={saveIntake} noValidate className="subform">
          <h3 className="h4">{i.id ? `Edit ${i.label}` : "Add an intake"}</h3>
          <TextField name="i_label" label="Label" required width="m" hint="For example January 2027" value={i.label} onChange={(v) => setI({ ...i, label: v })} />
          <DateField name="i_start" label="Course start" required value={i.start_date} onChange={(v) => setI({ ...i, start_date: v })} />
          <DateField name="i_deadline" label="Application deadline" required value={i.application_deadline} onChange={(v) => setI({ ...i, application_deadline: v })} />
          <div className="actions">
            <button className="button" type="submit">{i.id ? "Save intake" : "Add intake"}</button>
            {i.id !== 0 && <button className="button button-secondary" type="button" onClick={() => setI(emptyI)}>Cancel</button>}
          </div>
        </form>
      </section>
    </>
  );
}

// --- Fees ----------------------------------------------------------------------------------------------
function Fees({ onSaved }: { onSaved: (t: string) => void }) {
  const [rows, setRows] = useState<Fee[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState({ tuition_fee: "", minimum_deposit: "" });
  const [error, setError] = useState<string | null>(null);
  const load = () => api.get<Fee[]>("/admin/fees").then(setRows).catch(() => setFailed(true));
  useEffect(() => { void load(); }, []);
  if (failed) return <LoadFailed what="Fees" />;
  if (!rows) return <Spinner />;

  async function save(f: Fee) {
    setError(null);
    try {
      await api.post("/admin/fees", { programme_id: f.programme_id, intake_id: f.intake_id, ...form });
      onSaved(`Fees for ${f.programme}, ${f.intake} saved. Applications already priced keep their original fees.`);
      setEditing(null);
      await load();
    } catch (err) { setError(message(err)); }
  }

  const money = (v: string, c: string) => `${c} ${Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2 })}`;
  return (
    <section className="card">
      <h2 className="h3">Fees by programme and intake</h2>
      <p className="hint">A new fee applies from today. Applications already priced keep the fees they were given.</p>
      {error && <Alert kind="error">{error}</Alert>}
      {rows.length === 0 ? <p className="empty">Add an active programme and an open intake first.</p> : (
        <table className="table">
          <thead><tr><th scope="col">Programme</th><th scope="col">Intake</th><th scope="col">Tuition fee</th><th scope="col">Minimum deposit</th><th scope="col">Since</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            {rows.map((f) => {
              const k = `${f.programme_id}-${f.intake_id}`;
              return (
                <tr key={k}>
                  <td>{f.programme}</td><td>{f.intake}</td>
                  {editing === k ? (
                    <>
                      <td><TextField name={`fee-${k}`} label={`Tuition fee for ${f.programme}, ${f.intake}`} hideOptional width="s" inputMode="decimal" value={form.tuition_fee} onChange={(v) => setForm({ ...form, tuition_fee: v })} /></td>
                      <td><TextField name={`dep-${k}`} label="Minimum deposit" hideOptional width="s" inputMode="decimal" value={form.minimum_deposit} onChange={(v) => setForm({ ...form, minimum_deposit: v })} /></td>
                      <td />
                      <td className="nowrap">
                        <button type="button" className="button" onClick={() => void save(f)}>Save</button>{" "}
                        <button type="button" className="button button-secondary" onClick={() => { setEditing(null); setError(null); }}>Cancel</button>
                      </td>
                    </>
                  ) : (
                    <>
                      <td>{f.tuition_fee ? money(f.tuition_fee, f.currency) : <span className="tag tag-pending">Not set</span>}</td>
                      <td>{f.minimum_deposit ? money(f.minimum_deposit, f.currency) : "—"}</td>
                      <td>{f.effective_from ? formatDate(f.effective_from) : "—"}</td>
                      <td><button type="button" className="link-button" onClick={() => { setEditing(k); setForm({ tuition_fee: f.tuition_fee ?? "", minimum_deposit: f.minimum_deposit ?? "" }); setError(null); }}>{f.tuition_fee ? "Change" : "Set fees"}<span className="visually-hidden"> for {f.programme}, {f.intake}</span></button></td>
                    </>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
