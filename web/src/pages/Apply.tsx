import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, CATEGORY_LABELS, type AppData, type Application, type ErrorDetail } from "../api";
import { useAuth } from "../auth";
import {
  Alert, Checkbox, DateField, ErrorSummary, Layout, RadioGroup, SelectField, Spinner, TextArea, TextField,
} from "../components/ui";
import { STEPS, WizardFrame } from "../components/wizard";

// Appendix A — the nine steps live with the frame that draws them (components/wizard.tsx).
export { STEPS };
const AUTOSAVE_MS = 30_000; // LLD 14.2

type Option = { value: string | number; label: string };
interface Refs { countries: Option[]; programmes: Option[]; intakes: Option[]; agents: Option[] }
interface Problem extends ErrorDetail { message: string }

const YES_NO = [{ value: "true", label: "Yes" }, { value: "false", label: "No" }];
const LEVELS: Option[] = [
  { value: "secondary_school", label: "Secondary school / high school" }, { value: "foundation", label: "Foundation" },
  { value: "diploma", label: "Diploma" }, { value: "bachelors", label: "Bachelor's degree" },
  { value: "masters", label: "Master's degree" }, { value: "doctorate", label: "Doctorate" }, { value: "other", label: "Other" },
];

/** Order-insensitive JSON: PostgreSQL jsonb reorders object keys, which must not count as a change. */
function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v as object).sort().map((k) => `${JSON.stringify(k)}:${stable((v as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v ?? null);
}
const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));
const boolOrNull = (v: string) => (v === "true" ? true : v === "false" ? false : null);
const numOrNull = (v: string) => (v === "" ? null : Number(v));

export function Apply() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const step = Math.min(Math.max(Number(params.get("step") ?? 1) || 1, 1), 9);

  const [app, setApp] = useState<Application | null>(null);
  const [data, setData] = useState<AppData>({});
  const savedRef = useRef<AppData>({});
  const dataRef = useRef<AppData>({});
  const etagRef = useRef<string | undefined>();
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [summary, setSummary] = useState<Problem[]>([]);
  const [banner, setBanner] = useState<string | null>(null);
  const [refs, setRefs] = useState<Refs>({ countries: [], programmes: [], intakes: [], agents: [] });
  const [check, setCheck] = useState<{ ready: boolean; problems: Problem[]; outstanding_documents: string[] } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [attempted, setAttempted] = useState(false);

  dataRef.current = data;

  // Load or create the draft, plus reference data
  useEffect(() => {
    (async () => {
      const a = await api.post<Application & { __etag?: string }>("/applications");
      if (!a.editable) { navigate("/my", { replace: true }); return; }
      etagRef.current = a.__etag;
      savedRef.current = a.data;
      setApp(a);
      setData(a.data);
      const [countries, programmes, intakes, agents] = await Promise.all([
        api.get<{ code: string; name: string }[]>("/reference/countries"),
        api.get<{ id: number; title: string }[]>("/reference/programmes"),
        api.get<{ id: number; label: string; start_date: string }[]>("/reference/intakes"),
        api.get<{ id: number; name: string; country: string }[]>("/reference/agents"),
      ]);
      setRefs({
        countries: countries.map((c) => ({ value: c.code, label: c.name })),
        programmes: programmes.map((p) => ({ value: p.id, label: p.title })),
        intakes: intakes.map((i) => ({ value: i.id, label: `${i.label} (starts ${new Date(i.start_date).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })})` })),
        agents: agents.map((a) => ({ value: a.id, label: `${a.name} (${a.country})` })),
      });
    })().catch((err) => setBanner(err instanceof ApiError ? err.message : "We could not load your application."));
  }, [navigate]);

  const changedKeys = useCallback(() => {
    const cur = dataRef.current;
    const prev = savedRef.current;
    const keys = new Set([...Object.keys(cur), ...Object.keys(prev)]);
    return [...keys].filter((k) => stable(cur[k]) !== stable(prev[k]));
  }, []);

  /** Save changed top-level keys. Returns false when the server rejected a value. */
  const save = useCallback(async (): Promise<boolean> => {
    if (!app) return false;
    const keys = changedKeys();
    if (!keys.length) return true;
    const payload: AppData = {};
    for (const k of keys) payload[k] = dataRef.current[k] ?? null;
    setSaveState("saving");
    try {
      const out = await api.put<Application & { __etag?: string }>(`/applications/${app.id}`, { data: payload },
        etagRef.current ? { "If-Match": etagRef.current } : undefined);
      etagRef.current = out.__etag;
      savedRef.current = out.data;
      setApp(out);
      // Adopt the server's normalised value for each key we sent, unless it was edited again in flight.
      setData((curr) => {
        const next: AppData = { ...curr };
        for (const k of keys) {
          if (stable(curr[k]) !== stable(payload[k])) continue;
          if (k in out.data) next[k] = out.data[k];
          else delete next[k];
        }
        return next;
      });
      setFieldErrors((fe) => Object.fromEntries(Object.entries(fe).filter(([f]) => !keys.includes(f.split(".")[0]))));
      setSummary([]);
      setSaveState("saved");
      setSavedAt(new Date());
      return true;
    } catch (err) {
      setSaveState("error");
      if (err instanceof ApiError && err.code === "ERR_VALIDATION") {
        const fe: Record<string, string> = {};
        for (const d of err.details) if (d.field) fe[d.field] = d.message ?? "Check this answer.";
        setFieldErrors(fe);
        setSummary(err.details.map((d) => ({ ...d, message: d.message ?? "Check this answer." })));
        return false;
      }
      if (err instanceof ApiError && err.code === "ERR_STALE_RECORD") {
        setBanner("This application was changed in another window. Reload the page to continue with the latest version.");
      } else if (err instanceof ApiError && err.code === "ERR_NOT_EDITABLE") {
        navigate("/my");
      } else if (err instanceof ApiError && err.status !== 401) {
        setBanner(err.message);
      }
      return false;
    }
  }, [app, changedKeys, navigate]);

  // Autosave every 30 seconds when there are unsaved changes (FR-04)
  useEffect(() => {
    const t = window.setInterval(() => { if (changedKeys().length) void save(); }, AUTOSAVE_MS);
    return () => window.clearInterval(t);
  }, [changedKeys, save]);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (changedKeys().length) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [changedKeys]);

  const goTo = useCallback(async (n: number, focusField?: string | null) => {
    const ok = await save();
    if (!ok && n > step) return; // stay to correct malformed answers before moving forward
    setParams({ step: String(n) });
    window.scrollTo(0, 0);
    window.setTimeout(() => {
      const target = focusField ? document.querySelector<HTMLElement>(`#f-${CSS.escape(focusField)} input, #f-${CSS.escape(focusField)} select, #f-${CSS.escape(focusField)} textarea`) : null;
      (target ?? document.getElementById("step-heading"))?.focus();
    }, 50);
  }, [save, setParams, step]);

  // Step 9: evaluate readiness
  useEffect(() => {
    if (step !== 9 || !app) return;
    (async () => {
      await save();
      setCheck(await api.get(`/applications/${app.id}/submission-check`));
    })().catch(() => undefined);
  }, [step, app?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function refreshCheck() {
    if (!app) return;
    await save();
    setCheck(await api.get(`/applications/${app.id}/submission-check`));
  }

  async function submit() {
    if (!app) return;
    setSubmitting(true);
    setAttempted(true);
    try {
      if (!(await save())) return;
      await api.post(`/applications/${app.id}/submit`);
      navigate("/apply/submitted");
    } catch (err) {
      if (err instanceof ApiError && err.code === "ERR_MANDATORY_FIELDS") {
        setCheck({ ready: false, problems: err.details.map((d) => ({ ...d, message: d.message ?? "" })), outstanding_documents: check?.outstanding_documents ?? [] });
      } else if (err instanceof ApiError) setBanner(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  const set = (key: string, value: unknown) => setData((d) => ({ ...d, [key]: value }));
  const setIn = (key: string, sub: string, value: unknown) =>
    setData((d) => ({ ...d, [key]: { ...((d[key] as Record<string, unknown>) ?? {}), [sub]: value } }));
  const e = (path: string) => fieldErrors[path];

  const field = useMemo(() => ({ data, set, setIn, e, refs }), [data, fieldErrors, refs]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!app) return <Layout>{banner ? <Alert kind="error">{banner}</Alert> : <Spinner label="Loading your application…" />}</Layout>;

  return (
    <Layout wide>
      <WizardFrame
        reference={app.ref}
        current={step}
        furthest={step - 1}
        onStep={(n: number) => void goTo(n)}
        status={
          <p className="save-status" aria-live="polite">
            {saveState === "saving" && "Saving…"}
            {saveState === "saved" && savedAt && `Saved at ${savedAt.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`}
            {saveState === "error" && "Not saved — check the answers marked in red"}
            {saveState === "idle" && "Your answers save automatically"}
          </p>
        }
      >
        <>
          {banner && <Alert kind="error">{banner}</Alert>}
          {!user?.email_verified && (
            <Alert kind="warning" title="Confirm your email address">
              Use the link we emailed to you. You can complete the form now, but you cannot submit until your email is confirmed.
            </Alert>
          )}
          <span className="caption">Section {step} of 9</span>
          <h1 id="step-heading" tabIndex={-1}>{STEPS[step - 1]}</h1>
          <ErrorSummary title="Some answers need correcting" errors={summary}
            onSelect={(f, s) => (s && s !== step ? void goTo(s, f) : document.querySelector<HTMLElement>(`#f-${CSS.escape(f ?? "")} input`)?.focus())} />

          {step === 1 && <Step1 {...field} accountEmail={user?.email ?? ""} />}
          {step === 2 && <Step2 {...field} />}
          {step === 3 && <Step3 {...field} />}
          {step === 4 && <Step4 {...field} />}
          {step === 5 && <Step5 {...field} />}
          {step === 6 && <Step6 {...field} />}
          {step === 7 && <Step7 {...field} />}
          {step === 8 && <Step8 {...field} />}
          {step === 9 && (
            <Step9 {...field} check={check} onRefresh={refreshCheck} onSubmit={submit} submitting={submitting}
              goTo={(s, f) => void goTo(s, f)} emailVerified={Boolean(user?.email_verified)} attempted={attempted} />
          )}

          {step < 9 && (
            <div className="actions">
              <button type="button" className="button" onClick={() => void goTo(step + 1)}>Save and continue</button>
              {step > 1 && <button type="button" className="button button-secondary" onClick={() => void goTo(step - 1)}>Back</button>}
              <button type="button" className="link-button" onClick={() => void save().then((ok) => ok && navigate("/my"))}>Save and come back later</button>
            </div>
          )}
        </>
      </WizardFrame>
    </Layout>
  );
}

// ---------------------------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------------------------
interface StepProps {
  data: AppData;
  set: (k: string, v: unknown) => void;
  setIn: (k: string, sub: string, v: unknown) => void;
  e: (path: string) => string | undefined;
  refs: Refs;
}

function Step1({ data, set, e, refs, accountEmail }: StepProps & { accountEmail: string }) {
  return (
    <>
      <TextField name="full_name" label="Full name" hint="Exactly as shown in your passport." required
        value={str(data.full_name)} onChange={(v) => set("full_name", v)} error={e("full_name")} autoComplete="name" />
      <TextField name="other_names" label="Other names you have used" value={str(data.other_names)} onChange={(v) => set("other_names", v)} error={e("other_names")} />
      <DateField name="date_of_birth" label="Date of birth" required value={str(data.date_of_birth)} onChange={(v) => set("date_of_birth", v)} error={e("date_of_birth")} />
      <RadioGroup name="sex" label="Sex" inline value={str(data.sex)} onChange={(v) => set("sex", v)} error={e("sex")}
        options={[{ value: "female", label: "Female" }, { value: "male", label: "Male" }, { value: "prefer_not_to_say", label: "Prefer not to say" }]} />
      <SelectField name="country_of_birth" label="Country of birth" value={str(data.country_of_birth)} onChange={(v) => set("country_of_birth", v)} options={refs.countries} error={e("country_of_birth")} />
      <TextField name="email" label="Email address" hint="The address you registered with. We use it for all correspondence." required readOnly
        value={accountEmail || str(data.email)} onChange={() => undefined} error={e("email")} />
      <TextField name="mobile" label="Mobile number" hint="Include the country code, for example +44 7700 900123." required type="tel" inputMode="tel" autoComplete="tel"
        value={str(data.mobile)} onChange={(v) => set("mobile", v)} error={e("mobile")} width="m" />
    </>
  );
}

function Step2({ data, set, e, refs }: StepProps) {
  return (
    <>
      <SelectField name="nationality" label="Nationality" required value={str(data.nationality)} onChange={(v) => set("nationality", v)} options={refs.countries} error={e("nationality")} />
      <SelectField name="second_nationality" label="Second nationality" value={str(data.second_nationality)} onChange={(v) => set("second_nationality", v)} options={refs.countries} error={e("second_nationality")} />
      <TextField name="passport_number" label="Passport number" required width="m" value={str(data.passport_number)} onChange={(v) => set("passport_number", v)} error={e("passport_number")} />
      <DateField name="passport_issue_date" label="Passport issue date" required value={str(data.passport_issue_date)} onChange={(v) => set("passport_issue_date", v)} error={e("passport_issue_date")} />
      <DateField name="passport_expiry_date" label="Passport expiry date" hint="Your passport must be valid beyond the start of your course." required value={str(data.passport_expiry_date)} onChange={(v) => set("passport_expiry_date", v)} error={e("passport_expiry_date")} />
      <SelectField name="passport_issuing_country" label="Issuing country" required value={str(data.passport_issuing_country)} onChange={(v) => set("passport_issuing_country", v)} options={refs.countries} error={e("passport_issuing_country")} />
    </>
  );
}

function AddressFields({ prefix, legend, required, data, setIn, e, refs }: StepProps & { prefix: string; legend: string; required?: boolean }) {
  const a = (data[prefix] as Record<string, unknown>) ?? {};
  return (
    <fieldset className="group">
      <legend>{legend}{!required && <span className="optional"> (optional)</span>}</legend>
      <TextField name={`${prefix}.line1`} label="Address line 1" required={required} value={str(a.line1)} onChange={(v) => setIn(prefix, "line1", v)} error={e(`${prefix}.line1`)} />
      <TextField name={`${prefix}.line2`} label="Address line 2" value={str(a.line2)} onChange={(v) => setIn(prefix, "line2", v)} error={e(`${prefix}.line2`)} />
      <TextField name={`${prefix}.city`} label="Town or city" required={required} width="m" value={str(a.city)} onChange={(v) => setIn(prefix, "city", v)} error={e(`${prefix}.city`)} />
      <TextField name={`${prefix}.region`} label="State, province or region" width="m" value={str(a.region)} onChange={(v) => setIn(prefix, "region", v)} error={e(`${prefix}.region`)} />
      <TextField name={`${prefix}.postcode`} label="Postcode" width="s" value={str(a.postcode)} onChange={(v) => setIn(prefix, "postcode", v)} error={e(`${prefix}.postcode`)} />
      <SelectField name={`${prefix}.country`} label="Country" required={required} value={str(a.country)} onChange={(v) => setIn(prefix, "country", v)} options={refs.countries} error={e(`${prefix}.country`)} />
    </fieldset>
  );
}

function Step3(props: StepProps) {
  const { data, setIn, e } = props;
  const c = (data.emergency_contact as Record<string, unknown>) ?? {};
  return (
    <>
      <AddressFields {...props} prefix="home_address" legend="Home address" required />
      <AddressFields {...props} prefix="uk_address" legend="UK address, if you already have one" />
      <fieldset className="group">
        <legend>Emergency contact<span className="optional"> (optional)</span></legend>
        <TextField name="emergency_contact.name" label="Name" value={str(c.name)} onChange={(v) => setIn("emergency_contact", "name", v)} error={e("emergency_contact.name")} />
        <TextField name="emergency_contact.relationship" label="Relationship to you" width="m" value={str(c.relationship)} onChange={(v) => setIn("emergency_contact", "relationship", v)} error={e("emergency_contact.relationship")} />
        <TextField name="emergency_contact.phone" label="Phone" type="tel" width="m" hint="Include the country code." value={str(c.phone)} onChange={(v) => setIn("emergency_contact", "phone", v)} error={e("emergency_contact.phone")} />
        <TextField name="emergency_contact.email" label="Email" type="email" value={str(c.email)} onChange={(v) => setIn("emergency_contact", "email", v)} error={e("emergency_contact.email")} />
      </fieldset>
    </>
  );
}

function Step4({ data, set, e, refs }: StepProps) {
  return (
    <>
      <SelectField name="programme_id" label="Programme" required value={str(data.programme_id)} onChange={(v) => set("programme_id", numOrNull(v))} options={refs.programmes} error={e("programme_id")} />
      <SelectField name="intake_id" label="Intake" required value={str(data.intake_id)} onChange={(v) => set("intake_id", numOrNull(v))} options={refs.intakes} error={e("intake_id")}
        hint={refs.intakes.length ? undefined : "No intakes are open for applications at the moment."} />
      <RadioGroup name="study_mode" label="Study mode" required inline value={str(data.study_mode)} onChange={(v) => set("study_mode", v)} error={e("study_mode")}
        options={[{ value: "full_time", label: "Full-time" }, { value: "part_time", label: "Part-time" }]} />
      <SelectField name="agent_id" label="Recruitment agent" hint="Only if an agent is helping you with this application." value={str(data.agent_id)} onChange={(v) => set("agent_id", numOrNull(v))} options={refs.agents} placeholder="No agent" error={e("agent_id")} />
      <SelectField name="how_heard" label="How did you hear about us?" value={str(data.how_heard)} onChange={(v) => set("how_heard", v)} error={e("how_heard")}
        options={[{ value: "ese_website", label: "ESE website" }, { value: "search_engine", label: "Search engine" }, { value: "social_media", label: "Social media" },
          { value: "agent", label: "Recruitment agent" }, { value: "education_fair", label: "Education fair" }, { value: "friend_or_family", label: "Friend or family" }, { value: "other", label: "Other" }]} />
    </>
  );
}

type Row = Record<string, unknown>;
function Repeater({ name, rows, onChange, blank, render, addLabel, max = 10 }: {
  name: string; rows: Row[]; onChange: (rows: Row[]) => void; blank: Row; render: (row: Row, i: number, update: (k: string, v: unknown) => void) => React.ReactNode; addLabel: string; max?: number;
}) {
  return (
    <div className="repeater" id={`f-${name}`}>
      {rows.map((row, i) => (
        <fieldset key={i} className="group">
          <legend>{addLabel.replace("Add ", "").replace("another ", "")} {i + 1}</legend>
          {render(row, i, (k, v) => onChange(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r))))}
          <button type="button" className="link-button danger" onClick={() => onChange(rows.filter((_, j) => j !== i))}>Remove</button>
        </fieldset>
      ))}
      {rows.length < max && (
        <button type="button" className="button button-secondary" onClick={() => onChange([...rows, { ...blank }])}>
          {rows.length ? addLabel.replace("Add ", "Add another ") : addLabel}
        </button>
      )}
    </div>
  );
}

function Step5({ data, set, e, refs }: StepProps) {
  const quals = (data.qualifications as Row[]) ?? [];
  const jobs = (data.employment_history as Row[]) ?? [];
  return (
    <>
      <h2 className="h3">Qualifications</h2>
      <p className="hint">Add your most recent qualifications first. You will upload certificates on the documents page.</p>
      {e("qualifications") && <p className="error-message">{e("qualifications")}</p>}
      <Repeater name="qualifications" rows={quals} onChange={(r) => set("qualifications", r)} addLabel="Add qualification"
        blank={{ institution: "", title: "", level: "", country: "", start_date: "", end_date: "", grade: "" }}
        render={(q, i, up) => (
          <>
            <TextField name={`qualifications.${i}.institution`} label="Institution" required value={str(q.institution)} onChange={(v) => up("institution", v)} error={e(`qualifications.${i}.institution`)} />
            <TextField name={`qualifications.${i}.title`} label="Qualification title" required value={str(q.title)} onChange={(v) => up("title", v)} error={e(`qualifications.${i}.title`)} />
            <SelectField name={`qualifications.${i}.level`} label="Level" required value={str(q.level)} onChange={(v) => up("level", v)} options={LEVELS} error={e(`qualifications.${i}.level`)} />
            <SelectField name={`qualifications.${i}.country`} label="Country" required value={str(q.country)} onChange={(v) => up("country", v)} options={refs.countries} error={e(`qualifications.${i}.country`)} />
            <div className="row">
              <DateField name={`qualifications.${i}.start_date`} label="Start date" required value={str(q.start_date)} onChange={(v) => up("start_date", v)} error={e(`qualifications.${i}.start_date`)} />
              <DateField name={`qualifications.${i}.end_date`} label="End date" required value={str(q.end_date)} onChange={(v) => up("end_date", v)} error={e(`qualifications.${i}.end_date`)} />
            </div>
            <TextField name={`qualifications.${i}.grade`} label="Grade or result" required width="m" value={str(q.grade)} onChange={(v) => up("grade", v)} error={e(`qualifications.${i}.grade`)} />
          </>
        )} />
      <SelectField name="highest_level" label="Highest level of study completed" required value={str(data.highest_level)} onChange={(v) => set("highest_level", v)} options={LEVELS} error={e("highest_level")} />
      <h2 className="h3">Employment history <span className="optional">(optional)</span></h2>
      <Repeater name="employment_history" rows={jobs} onChange={(r) => set("employment_history", r)} addLabel="Add employment"
        blank={{ employer: "", role: "", start_date: "", end_date: "" }}
        render={(j, i, up) => (
          <>
            <TextField name={`employment_history.${i}.employer`} label="Employer" value={str(j.employer)} onChange={(v) => up("employer", v)} error={e(`employment_history.${i}.employer`)} />
            <TextField name={`employment_history.${i}.role`} label="Role" value={str(j.role)} onChange={(v) => up("role", v)} error={e(`employment_history.${i}.role`)} />
            <div className="row">
              <DateField name={`employment_history.${i}.start_date`} label="Start date" value={str(j.start_date)} onChange={(v) => up("start_date", v)} />
              <DateField name={`employment_history.${i}.end_date`} label="End date" value={str(j.end_date)} onChange={(v) => up("end_date", v)} />
            </div>
          </>
        )} />
    </>
  );
}

function Step6({ data, set, setIn, e }: StepProps) {
  const scores = (data.test_scores as Record<string, unknown>) ?? {};
  const slt = data.english_route === "slt_test";
  return (
    <>
      <RadioGroup name="english_route" label="How will you show your English language ability?" required value={str(data.english_route)} onChange={(v) => set("english_route", v)} error={e("english_route")}
        options={[
          { value: "slt_test", label: "A Secure English Language Test (SELT)", hint: "For example IELTS for UKVI." },
          { value: "majority_english_country", label: "I am a national of a majority English-speaking country" },
          { value: "degree_taught_in_english", label: "I hold a degree taught in English" },
          { value: "provider_assessment", label: "Assessment by ESE" },
        ]} />
      {slt && (
        <fieldset className="group">
          <legend>Test details</legend>
          <TextField name="test_provider" label="Test provider and test name" required value={str(data.test_provider)} onChange={(v) => set("test_provider", v)} error={e("test_provider")} />
          <TextField name="test_reference" label="Test reference number" hint="As shown on your test report, so we can verify it." required width="m" value={str(data.test_reference)} onChange={(v) => set("test_reference", v)} error={e("test_reference")} />
          <DateField name="test_date" label="Test date" hint="The test must still be valid at the start of your course." required value={str(data.test_date)} onChange={(v) => set("test_date", v)} error={e("test_date")} />
          <div className="scores">
            {(["overall", "reading", "writing", "listening", "speaking"] as const).map((k) => (
              <TextField key={k} name={`test_scores.${k}`} label={k[0].toUpperCase() + k.slice(1)} required width="s" inputMode="decimal"
                value={str(scores[k])} onChange={(v) => setIn("test_scores", k, v === "" ? null : Number(v))} error={e(`test_scores.${k}`)} />
            ))}
          </div>
        </fieldset>
      )}
    </>
  );
}

function Step7({ data, set, setIn, e }: StepProps) {
  const studies = data.previous_uk_study as Row[] | undefined;
  const permission = (data.current_uk_permission as Record<string, unknown>) ?? {};
  const studied = studies === undefined ? "" : studies.length ? "true" : "false";
  return (
    <>
      <RadioGroup name="been_to_uk" label="Have you been to the UK before?" required inline value={data.been_to_uk as boolean | undefined} onChange={(v) => set("been_to_uk", boolOrNull(v))} options={YES_NO} error={e("been_to_uk")} />
      {data.been_to_uk === true && (
        <>
          <RadioGroup name="previous_uk_study" label="Have you studied in the UK before?" required inline value={studied}
            onChange={(v) => set("previous_uk_study", v === "true" ? [{ institution: "", sponsor_licence_number: "", qualification_level: "", start_date: "", end_date: "", completed: null }] : [])}
            options={YES_NO} error={e("previous_uk_study")} />
          {Boolean(studies?.length) && (
            <Repeater name="previous_uk_study_rows" rows={studies ?? []} onChange={(r) => set("previous_uk_study", r)} addLabel="Add UK course"
              blank={{ institution: "", sponsor_licence_number: "", qualification_level: "", start_date: "", end_date: "", completed: null }}
              render={(s, i, up) => (
                <>
                  <TextField name={`previous_uk_study.${i}.institution`} label="Institution" required value={str(s.institution)} onChange={(v) => up("institution", v)} error={e(`previous_uk_study.${i}.institution`)} />
                  <TextField name={`previous_uk_study.${i}.sponsor_licence_number`} label="Sponsor licence number, if known" width="m" value={str(s.sponsor_licence_number)} onChange={(v) => up("sponsor_licence_number", v)} />
                  <SelectField name={`previous_uk_study.${i}.qualification_level`} label="Qualification level" required value={str(s.qualification_level)} onChange={(v) => up("qualification_level", v)} options={LEVELS} error={e(`previous_uk_study.${i}.qualification_level`)} />
                  <div className="row">
                    <DateField name={`previous_uk_study.${i}.start_date`} label="Start date" required value={str(s.start_date)} onChange={(v) => up("start_date", v)} error={e(`previous_uk_study.${i}.start_date`)} />
                    <DateField name={`previous_uk_study.${i}.end_date`} label="End date" required value={str(s.end_date)} onChange={(v) => up("end_date", v)} error={e(`previous_uk_study.${i}.end_date`)} />
                  </div>
                  <RadioGroup name={`previous_uk_study.${i}.completed`} label="Did you complete the course?" required inline value={s.completed as boolean | undefined} onChange={(v) => up("completed", boolOrNull(v))} options={YES_NO} error={e(`previous_uk_study.${i}.completed`)} />
                </>
              )} />
          )}
        </>
      )}
      <RadioGroup name="previous_visa_refusal" label="Have you ever been refused a visa for any country?" required inline value={data.previous_visa_refusal as boolean | undefined} onChange={(v) => set("previous_visa_refusal", boolOrNull(v))} options={YES_NO} error={e("previous_visa_refusal")} />
      {data.previous_visa_refusal === true && (
        <TextArea name="refusal_details" label="Give details of each refusal" hint="Country, date, visa type and the reason given." required value={str(data.refusal_details)} onChange={(v) => set("refusal_details", v)} error={e("refusal_details")} maxLength={4000} rows={4} />
      )}
      <fieldset className="group">
        <legend>If you are in the UK now<span className="optional"> (optional)</span></legend>
        <TextField name="current_uk_permission.type" label="Current immigration permission" width="m" value={str(permission.type)} onChange={(v) => setIn("current_uk_permission", "type", v)} error={e("current_uk_permission.type")} />
        <DateField name="current_uk_permission.expiry_date" label="Permission expiry date" value={str(permission.expiry_date)} onChange={(v) => setIn("current_uk_permission", "expiry_date", v)} />
        <TextField name="brp_number" label="Biometric residence permit number" width="m" value={str(data.brp_number)} onChange={(v) => set("brp_number", v)} error={e("brp_number")} />
      </fieldset>
    </>
  );
}

function Step8({ data, set, e }: StepProps) {
  return (
    <>
      <TextArea name="personal_statement" label="Personal statement" hint="Why this programme, why ESE, and what you will bring. Between 200 and 4,000 characters." required
        value={str(data.personal_statement)} onChange={(v) => set("personal_statement", v)} error={e("personal_statement")} maxLength={4000} minLength={200} rows={10} />
      <TextArea name="study_purpose" label="Your plans during and after your studies" required value={str(data.study_purpose)} onChange={(v) => set("study_purpose", v)} error={e("study_purpose")} maxLength={4000} rows={5} />
      <RadioGroup name="funding_source" label="How will your studies be funded?" required value={str(data.funding_source)} onChange={(v) => set("funding_source", v)} error={e("funding_source")}
        options={[{ value: "self", label: "Self-funded" }, { value: "family", label: "Family" }, { value: "sponsor", label: "Sponsor or employer" }, { value: "scholarship", label: "Scholarship" }, { value: "loan", label: "Loan" }]} />
      {data.funding_source && data.funding_source !== "self" && (
        <TextArea name="funder_details" label="Who is funding your studies?" required value={str(data.funder_details)} onChange={(v) => set("funder_details", v)} error={e("funder_details")} maxLength={2000} rows={3} />
      )}
      <RadioGroup name="disability_support_required" label="Would you like to tell us about support needs related to a disability?" inline
        hint="This is recorded separately and is not used to assess your application." value={data.disability_support_required as boolean | undefined}
        onChange={(v) => set("disability_support_required", boolOrNull(v))} options={YES_NO} error={e("disability_support_required")} />
    </>
  );
}

function Step9({ data, set, e, check, onRefresh, onSubmit, submitting, goTo, emailVerified, attempted }: StepProps & {
  check: { ready: boolean; problems: Problem[]; outstanding_documents: string[] } | null;
  onRefresh: () => Promise<void>; onSubmit: () => Promise<void>; submitting: boolean;
  goTo: (step: number, field?: string | null) => void; emailVerified: boolean; attempted: boolean;
}) {
  const others = check?.problems.filter((p) => p.step !== 9) ?? [];
  const byStep = others.reduce<Record<number, Problem[]>>((acc, p) => { const s = p.step ?? 0; (acc[s] ??= []).push(p); return acc; }, {});
  const satisfied = (f: string) => (f === "declaration_signature_name" ? str(data[f]).trim() !== "" : data[f] === true);
  const declErr = (f: string) => e(f) ?? (attempted && !satisfied(f) ? check?.problems.find((p) => p.field === f)?.message : undefined);
  // Declarations are ticked on this page, so only earlier sections block the button; the server re-checks everything.
  const blocked = !check || others.length > 0 || !emailVerified;
  return (
    <>
      {check && others.length > 0 && (
        <div className="panel panel-warning">
          <h2 className="h3">Still to complete</h2>
          <p>You can submit once these answers are complete.</p>
          {Object.entries(byStep).map(([s, items]) => (
            <div key={s}>
              <h3 className="h4">{Number(s) ? `${s}. ${STEPS[Number(s) - 1]}` : "Your account"}</h3>
              <ul>
                {items.map((p, i) => (
                  <li key={i}>
                    {p.step ? <a href={`?step=${p.step}`} onClick={(ev) => { ev.preventDefault(); goTo(p.step!, p.field); }}>{p.message}</a> : p.message}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      {check && check.outstanding_documents.length > 0 && (
        <Alert kind="info" title="Documents still to upload">
          <p>{check.outstanding_documents.map((c) => CATEGORY_LABELS[c] ?? c).join(", ")}.</p>
          <p>Upload these before you submit: once your application is submitted, you can only add documents if ESE asks you for more information. <Link to="/apply/documents">Upload documents</Link></p>
        </Alert>
      )}
      <h2 className="h3">Your declaration</h2>
      <Checkbox name="declaration_truthful" label="I confirm that the information I have given is true and complete. I understand that giving false information may lead to my application being refused or any offer being withdrawn."
        checked={data.declaration_truthful === true} onChange={(v) => set("declaration_truthful", v)} error={declErr("declaration_truthful")} />
      <Checkbox name="declaration_data_consent" label="I have read the privacy notice and agree to ESE processing my personal data to assess my application and meet its duties as a UK visa sponsor."
        checked={data.declaration_data_consent === true} onChange={(v) => set("declaration_data_consent", v)} error={declErr("declaration_data_consent")} />
      <TextField name="declaration_signature_name" label="Type your full name to sign" hint={`Must match the name in section 1${data.full_name ? `: ${String(data.full_name)}` : ""}.`} required
        value={str(data.declaration_signature_name)} onChange={(v) => set("declaration_signature_name", v)} error={declErr("declaration_signature_name")} />
      <div className="actions">
        <button type="button" className="button" disabled={submitting || blocked} aria-describedby="submit-reason" onClick={() => void onSubmit()}>
          {submitting ? "Submitting…" : "Submit application"}
        </button>
        <button type="button" className="button button-secondary" onClick={() => void onRefresh()}>Check my application</button>
      </div>
      {blocked && (
        <p id="submit-reason" className="hint">
          {!check ? "Checking your application…" : !emailVerified ? "Confirm your email address to submit." : `Submit becomes available when the ${others.length} item(s) above are complete.`}
        </p>
      )}
    </>
  );
}
