import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api, ApiError, CATEGORY_LABELS, STATUS_LABELS, downloadDocument, formatBytes, formatDate, formatDateTime,
  type Application, type ApplicationRecord, type DocumentRow, type Summary,
} from "../api";
import { useAuth } from "../auth";
import { DOCUMENTS_STEP, WizardFrame } from "../components/wizard";
import { Alert, Layout, SelectField, Spinner, StatusBadge, Tick } from "../components/ui";

const UPLOAD_CATEGORIES = ["passport", "qualification", "english", "financial", "statement", "other"];
/** The categories ESE asks for before submission, in the order the checklist shows them. */
const REQUIRED_CATEGORIES = ["passport", "qualification", "english", "financial"];
const WITH_EXPIRY = new Set(["passport", "english"]);
const MAX_BYTES = 5 * 1024 * 1024;

/**
 * The applicant's own application, read rather than created.
 *
 * FR-08: this used to POST /applications, which returns the *open* application and makes a new
 * draft when there is none. An applicant whose application had been rejected, withdrawn or whose
 * offer they had declined therefore opened "My application" and was handed a blank new form —
 * their outcome nowhere to be seen, and a fresh reference silently consumed. Reading the most
 * recent application, whatever its status, is the whole fix: starting a new one is now something
 * the applicant asks for.
 */
function useMyApplication() {
  const [app, setApp] = useState<Application | null>(null);
  const [none, setNone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const mine = await api.get<Summary[]>("/applications/mine");
      if (mine.length === 0) {
        setNone(true);
        setApp(null);
        return;
      }
      setNone(false);
      setApp(await api.get<Application>(`/applications/${mine[0].id}`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We could not load your application.");
    }
  }

  useEffect(() => { void load(); }, []);
  return { app, none, error, reload: load };
}

/** Statuses after which nothing more happens on the application (LLD 7.1). */
const CLOSED_STATUSES = new Set(["rejected", "withdrawn", "offer_declined"]);

function scanLabel(d: DocumentRow) {
  if (d.scan_status === "clean") return d.verified_at ? "Verified by ESE" : "Received";
  if (d.scan_status === "quarantined") return "Rejected — failed security scan";
  return "Checking…";
}

export function Documents() {
  const { app, none, error, reload } = useMyApplication();
  const [category, setCategory] = useState("");
  const [expiry, setExpiry] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  if (error) return <Layout><Alert kind="error">{error}</Alert></Layout>;
  if (none) {
    return (
      <Layout>
        <h1>Supporting documents</h1>
        <p className="empty">
          Documents are uploaded against an application. <Link to="/my">Start your application</Link> first.
        </p>
      </Layout>
    );
  }
  if (!app) return <Layout><Spinner /></Layout>;

  async function upload(e: FormEvent) {
    e.preventDefault();
    setMessage(null);
    if (!category) return setMessage({ kind: "error", text: "Choose what kind of document you are uploading." });
    if (!file) return setMessage({ kind: "error", text: "Choose a file to upload." });
    if (file.size > MAX_BYTES) return setMessage({ kind: "error", text: "Files must be 5MB or smaller." });
    const form = new FormData();
    form.append("category", category);
    form.append("file", file);
    if (expiry && WITH_EXPIRY.has(category)) form.append("expiry_date", expiry);
    setBusy(true);
    try {
      await api.upload(`/applications/${app!.id}/documents`, form);
      setMessage({ kind: "success", text: `${file.name} uploaded.` });
      setFile(null);
      setExpiry("");
      if (fileInput.current) fileInput.current.value = "";
      await reload();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof ApiError ? err.message : "Upload failed. Please try again." });
    } finally {
      setBusy(false);
    }
  }

  const current = app.documents.filter((d) => d.superseded_by === null);
  const held = new Set(current.map((d) => d.category));
  const required = [...new Set([...app.outstanding_documents, ...REQUIRED_CATEGORIES.filter((c) => held.has(c))])];

  return (
    // Prototype screen 3: the same wizard frame as the form, not a page the applicant is sent
    // away to — uploading documents is part of applying, so the section rail stays beside it.
    <Layout wide>
      {/* Nothing is ticked here: this screen does not know how far the form has been filled in. */}
      <WizardFrame reference={app.ref} current={DOCUMENTS_STEP}>
        <h1>Supporting documents</h1>
        <p className="lead">Upload clear scans or photos as PDF, JPG or PNG, up to 5MB each.</p>
        {required.length > 0 && (
          <div className="checklist">
            {required.map((c) => (
              <div className="chk" key={c}>
                <span className={held.has(c) ? "tick" : "tick open"} aria-hidden="true">{held.has(c) ? "✓" : ""}</span>
                {CATEGORY_LABELS[c] ?? c}
                <span className="visually-hidden">{held.has(c) ? " — received" : " — still needed"}</span>
              </div>
            ))}
          </div>
        )}
        {app.outstanding_documents.length === 0 && (
          <Alert kind="success">All the documents we ask for at this stage have been received.</Alert>
        )}
        {!app.editable && <Alert kind="warning">Your application has been submitted. If ESE needs anything else they will contact you.</Alert>}

        {app.editable && (
        <form className="card" onSubmit={upload} noValidate>
          <h2 className="h3">Upload a document</h2>
          {message && <Alert kind={message.kind}>{message.text}</Alert>}
          <SelectField name="category" label="Document type" required value={category} onChange={setCategory}
            options={UPLOAD_CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABELS[c] }))} />
          {WITH_EXPIRY.has(category) && (
            <div className="field">
              <label className="label" htmlFor="expiry">Expiry date<span className="optional"> (if shown on the document)</span></label>
              <input id="expiry" className="input w-m" type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} />
            </div>
          )}
          <div className="field">
            <label className="label" htmlFor="file">File</label>
            <p className="hint" id="file-hint">PDF, JPG or PNG. Maximum 5MB.</p>
            <input id="file" ref={fileInput} type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
              aria-describedby="file-hint" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </div>
          <button className="button" type="submit" disabled={busy}>{busy ? "Uploading and scanning…" : "Upload"}</button>
        </form>
      )}

        <h2 className="h3">Your documents</h2>
        {current.length === 0 ? (
          <p className="empty">Nothing uploaded yet. Documents you upload will be listed here with their status.</p>
        ) : (
          <div className="table-wrap"><table className="table">
          <caption className="visually-hidden">Documents you have uploaded</caption>
          <thead><tr><th scope="col">Type</th><th scope="col">File</th><th scope="col">Uploaded</th><th scope="col">Status</th></tr></thead>
          <tbody>
            {current.map((d) => (
              <tr key={d.id}>
                <td>{CATEGORY_LABELS[d.category] ?? d.category}{d.expiry_date && <span className="hint block">Expires {formatDate(d.expiry_date)}</span>}</td>
                <td>
                  {d.scan_status === "clean"
                    ? <button type="button" className="link-button" onClick={() => void downloadDocument(d)}>{d.filename}</button>
                    : d.filename}
                  <span className="hint block">{formatBytes(d.size_bytes)}</span>
                </td>
                <td>{formatDateTime(d.uploaded_at)}</td>
                <td><span className={`tag tag-${d.scan_status}`}>{scanLabel(d)}</span></td>
              </tr>
            ))}
          </tbody>
          </table></div>
        )}
        <p><Link to="/apply?step=9">← Back to the declaration</Link></p>
      </WizardFrame>
    </Layout>
  );
}

export function Submitted() {
  const { app } = useMyApplication();
  return (
    // Prototype screen 4: one thing on the page — the reference — then what happens next.
    <Layout wide>
      <div className="win">
        <div className="confirmation">
          <Tick />
          <h1>Your application has been submitted</h1>
          {app && (
            <>
              <span className="caption">Application reference</span>
              <p className="ref-box">{app.ref}</p>
            </>
          )}
          <p>
            We have sent a confirmation to your email address. Keep your reference — quote it whenever
            you contact us.
          </p>
          <div style={{ textAlign: "left", maxWidth: "420px", margin: "22px auto 0" }}>
            <h2 className="h4">What happens next</h2>
            <ol className="next-steps">
              <li>Our admissions team reviews your academic background and English language evidence.</li>
              <li>We may contact you for more information or to arrange an interview.</li>
              <li>You will be told the outcome by email, and you can follow progress here at any time.</li>
            </ol>
          </div>
          <Link to="/my" className="button" style={{ marginTop: "18px" }}>View my application</Link>
          <p className="hero-strap">A Global Education. A Brighter Future.</p>
        </div>
      </div>
    </Layout>
  );
}

const JOURNEY = ["submitted", "academic_review", "offer_issued", "offer_accepted", "compliance_review", "cas_assigned"];
/** Offer Pending is academic review finished, which is where the applicant sees themselves. */
const JOURNEY_ALIASES: Record<string, string> = { precas_ready: "compliance_review", offer_pending: "academic_review" };
const JOURNEY_LABELS = ["Submitted", "Academic review", "Offer", "Offer accepted", "Pre-CAS checks", "CAS issued"];

export function MyApplication() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { app, none, error } = useMyApplication();
  const [resent, setResent] = useState(false);
  const [starting, setStarting] = useState(false);
  const [detail, setDetail] = useState<ApplicationRecord | null>(null);

  useEffect(() => {
    if (app) api.get<ApplicationRecord>(`/applications/${app.id}`).then(setDetail).catch(() => setDetail(null));
  }, [app]);

  async function start() {
    setStarting(true);
    try {
      await api.post<Application>("/applications");
      navigate("/apply?step=1");
    } finally {
      setStarting(false);
    }
  }

  if (error) return <Layout><Alert kind="error">{error}</Alert></Layout>;
  if (none) {
    return (
      <Layout>
        <h1>My application</h1>
        <div className="panel">
          <h2 className="h3">You have not started an application yet</h2>
          <p>It takes nine short sections and your answers are saved as you go.</p>
          <div className="actions">
            <button type="button" className="button" onClick={() => void start()} disabled={starting}>
              {starting ? "Starting…" : "Start my application"}
            </button>
          </div>
        </div>
      </Layout>
    );
  }
  if (!app) return <Layout><Spinner /></Layout>;

  const closed = CLOSED_STATUSES.has(app.status);
  const pos = JOURNEY.indexOf(JOURNEY_ALIASES[app.status] ?? app.status);
  const d = app.data as Record<string, string | undefined>;
  return (
    <Layout>
      <h1>My application</h1>
      {!user?.email_verified && (
        <Alert kind="warning" title="Confirm your email address">
          <p>We sent you a link when you registered. You need to confirm your email address before you can submit.</p>
          {resent ? <p>We have sent a new link.</p> : (
            <button type="button" className="link-button" onClick={() => void api.post("/auth/verify-email/resend").then(() => setResent(true))}>Send the link again</button>
          )}
        </Alert>
      )}
      <div className="card status-card">
        <div>
          <span className="caption">Reference</span>
          <p className="ref-large">{app.ref}</p>
        </div>
        <div>
          <span className="caption">Status</span>
          <p><StatusBadge status={app.status} label={STATUS_LABELS[app.status] ?? app.status} /></p>
        </div>
        {d.submitted_at && (
          <div>
            <span className="caption">Submitted</span>
            <p>{formatDateTime(d.submitted_at)}</p>
          </div>
        )}
      </div>

      {closed && (
        <div className="panel panel-warning">
          <h2 className="h3">
            {app.status === "rejected" ? "This application was not successful"
              : app.status === "withdrawn" ? "This application was withdrawn"
              : "You declined this offer"}
          </h2>
          <p>
            {app.status === "rejected"
              ? "We wrote to you with the decision. This application is closed and cannot be reopened."
              : app.status === "withdrawn"
              ? "This application is closed. If that is not what you expected, contact admissions quoting your reference."
              : "This application is closed. If you have changed your mind, contact admissions quoting your reference."}
          </p>
          <div className="actions">
            <button type="button" className="button button-secondary" onClick={() => void start()} disabled={starting}>
              {starting ? "Starting…" : "Start a new application"}
            </button>
          </div>
        </div>
      )}

      {app.status === "draft" && (
        <div className="panel">
          <h2 className="h3">Continue your application</h2>
          <p>Your answers are saved. Pick up where you left off.</p>
          <div className="actions">
            <Link className="button" to="/apply?step=1">Continue application</Link>
            <Link className="button button-secondary" to="/apply/documents">Upload documents</Link>
          </div>
        </div>
      )}

      {app.status === "info_requested" && (
        <div className="panel">
          <h2 className="h3">We need more information</h2>
          <p>Your application has been returned to you so you can add what we asked for. We emailed you the details.</p>
          <div className="actions">
            <Link className="button" to="/apply?step=1">Update your application</Link>
            <Link className="button button-secondary" to="/apply/documents">Upload documents</Link>
          </div>
        </div>
      )}

      {detail?.offer?.issued_at && (
        <div className="panel">
          <h2 className="h3">{detail.offer.response === "accepted" ? "You have accepted your offer" : detail.offer.response === "declined" ? "You declined your offer" : "You have an offer"}</h2>
          <p>
            {detail.offer.offer_type === "conditional" ? "A conditional offer" : "An unconditional offer"} for
            {" "}{detail.programme ?? "your programme"}
            {detail.offer.expiry_date && !detail.offer.response ? `. Respond by ${formatDate(detail.offer.expiry_date)}.` : "."}
          </p>
          <div className="actions">
            <Link className="button" to="/my/offer">{detail.offer.response ? "View your offer" : "View and respond to your offer"}</Link>
          </div>
        </div>
      )}

      {detail?.cas && (
        <div className="panel">
          <h2 className="h3">Your CAS has been issued</h2>
          <p>CAS number <strong>{detail.cas.cas_number}</strong> for {detail.cas.course_title}, starting {formatDate(detail.cas.start_date)}.</p>
          <p>Use this number in your Student visa application. Keep it safe — we cannot issue a second CAS.</p>
        </div>
      )}

      {pos >= 0 && (
        <section aria-labelledby="progress-h">
          <h2 id="progress-h" className="h3">Progress</h2>
          <ol className="journey">
            {JOURNEY_LABELS.map((label, i) => (
              <li key={label} className={i < pos ? "done" : i === pos ? "current" : ""} aria-current={i === pos ? "step" : undefined}>
                <span className="dot" aria-hidden="true" />
                {label}
                <span className="visually-hidden">{i < pos ? " (complete)" : i === pos ? " (current stage)" : " (not started)"}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section aria-labelledby="summary-h" className="summary-list">
        <h2 id="summary-h" className="h3">Summary</h2>
        <dl>
          <div><dt>Name</dt><dd>{d.full_name ?? "—"}</dd></div>
          <div><dt>Email</dt><dd>{d.email ?? "—"}</dd></div>
          <div><dt>Documents received</dt><dd>{app.documents.filter((x) => x.scan_status === "clean").length}</dd></div>
          <div><dt>Documents still needed</dt><dd>{app.outstanding_documents.length ? app.outstanding_documents.map((c) => CATEGORY_LABELS[c]).join(", ") : "None"}</dd></div>
        </dl>
      </section>
    </Layout>
  );
}
