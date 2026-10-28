import { useEffect, useState, type FormEvent } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type User } from "../api";
import { homeFor, useAuth } from "../auth";
import { Alert, ErrorSummary, Layout, TextField, detailsToErrors } from "../components/ui";

interface TokenOut { token: string; user: User }

function useFormErrors() {
  const [summary, setSummary] = useState<{ field: string | null; message: string }[]>([]);
  const [fields, setFields] = useState<Record<string, string>>({});
  function fromError(err: unknown) {
    if (err instanceof ApiError) {
      const byField = detailsToErrors(err.details);
      setFields(byField);
      const items = err.details.filter((d) => d.field).map((d) => ({ field: d.field, message: d.message ?? err.message }));
      setSummary(items.length ? items : [{ field: null, message: err.message }]);
    } else {
      setSummary([{ field: null, message: "Something went wrong. Please try again." }]);
    }
  }
  function clear() { setSummary([]); setFields({}); }
  return { summary, fields, fromError, clear };
}

export function SignIn() {
  const { user, signIn, notice, clearNotice } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const errors = useFormErrors();

  if (user) return <Navigate to={homeFor(user)} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    errors.clear();
    setBusy(true);
    try {
      const out = await api.post<TokenOut>("/auth/login", { email, password });
      clearNotice();
      signIn(out.token, out.user); // the signed-in render redirects to the right home
    } catch (err) {
      errors.fromError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout wide>
      {/* Applicant welcome — prototype screen 1. The photograph is ESE's own London image, taken
          from the approved prototype rather than a stand-in. */}
      <section className="win" style={{ marginBottom: "22px" }}>
        <div className="hero">
          <div className="hero-inner">
            <h1>A Global Education<br />A Brighter Future</h1>
            <p>Join a truly international community at the European School of Economics, London.</p>
            <Link to="/register" className="button">Apply now</Link>
          </div>
        </div>
        <div className="hero-points">
          <div><span className="icon" aria-hidden="true">🎓</span>World-class<br />programmes</div>
          <div><span className="icon" aria-hidden="true">🌍</span>International<br />community</div>
          <div><span className="icon" aria-hidden="true">💼</span>Career-focused<br />learning</div>
        </div>
        <p className="hero-strap">Business. Leadership. Creativity. Global Impact.</p>
      </section>

      <div className="two-col">
        <section>
          <h2>Apply to study with us</h2>
          <p className="lead">
            Create an account to start your application to the European School of Economics. You can save your
            progress and come back at any time.
          </p>
          <ul className="ticks">
            <li>Nine short sections — your answers are saved automatically</li>
            <li>Upload your passport, qualifications and supporting documents</li>
            <li>Follow your application from submission to decision</li>
          </ul>
          <Link to="/register" className="button button-secondary">Create an account</Link>
        </section>
        <section className="card">
          <h2>Sign in</h2>
          {notice && <Alert kind="warning">{notice}</Alert>}
          <ErrorSummary title="There is a problem" errors={errors.summary} />
          <form onSubmit={submit} noValidate>
            <TextField name="email" label="Email address" type="email" autoComplete="username" required
              value={email} onChange={setEmail} error={errors.fields.email} inputMode="email" />
            <TextField name="password" label="Password" type="password" autoComplete="current-password" required
              value={password} onChange={setPassword} error={errors.fields.password} />
            <button className="button" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
          </form>
          <p><Link to="/forgot-password">Forgotten your password?</Link></p>
        </section>
      </div>
    </Layout>
  );
}

export function Register() {
  const { user, signIn } = useAuth();
  const [form, setForm] = useState({ full_name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [justRegistered, setJustRegistered] = useState(false);
  const errors = useFormErrors();

  // A newly registered applicant goes straight into the form; anyone already signed in goes home.
  if (user) return <Navigate to={justRegistered ? "/apply?step=1" : homeFor(user)} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    errors.clear();
    setBusy(true);
    try {
      const out = await api.post<TokenOut>("/auth/register", form);
      setJustRegistered(true);
      signIn(out.token, out.user);
    } catch (err) {
      errors.fromError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div className="narrow">
        <h1>Create an account</h1>
        <p className="lead">We will send a link to confirm your email address. You must confirm it before you submit.</p>
        <ErrorSummary title="There is a problem" errors={errors.summary} />
        <form onSubmit={submit} noValidate className="card">
          <TextField name="full_name" label="Full name" hint="As shown in your passport." required autoComplete="name"
            value={form.full_name} onChange={(v) => setForm({ ...form, full_name: v })} error={errors.fields.full_name} />
          <TextField name="email" label="Email address" type="email" required autoComplete="email" inputMode="email"
            value={form.email} onChange={(v) => setForm({ ...form, email: v })} error={errors.fields.email} />
          <TextField name="password" label="Create a password" type="password" required autoComplete="new-password"
            hint="At least 10 characters. A short phrase of unrelated words works well."
            value={form.password} onChange={(v) => setForm({ ...form, password: v })} error={errors.fields.password} />
          <button className="button" type="submit" disabled={busy}>{busy ? "Creating account…" : "Create account"}</button>
        </form>
        <p>Already have an account? <Link to="/">Sign in</Link></p>
      </div>
    </Layout>
  );
}

export function VerifyEmail() {
  const [params] = useSearchParams();
  const { refresh, user } = useAuth();
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [message, setMessage] = useState("");
  useEffect(() => {
    const token = params.get("token") ?? "";
    api.post<{ message: string }>("/auth/verify-email/confirm", { token })
      .then((r) => { setState("done"); setMessage(r.message); void refresh(); })
      .catch((err: ApiError) => { setState("error"); setMessage(err.message); });
  }, [params, refresh]);
  return (
    <Layout>
      <div className="narrow">
        <h1>Confirm your email address</h1>
        {state === "working" && <p role="status">Confirming…</p>}
        {state === "done" && <Alert kind="success" title="Email address confirmed">{message}</Alert>}
        {state === "error" && <Alert kind="error" title="We could not confirm your email address">{message}</Alert>}
        <p><Link to={user ? homeFor(user) : "/"}>{user ? "Continue" : "Sign in"}</Link></p>
      </div>
    </Layout>
  );
}

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await api.post<{ message: string }>("/auth/password-reset/request", { email });
    setSent(r.message);
  }
  return (
    <Layout>
      <div className="narrow">
        <h1>Reset your password</h1>
        {sent ? <Alert kind="success">{sent}</Alert> : (
          <form onSubmit={submit} className="card" noValidate>
            <TextField name="email" label="Email address" type="email" required value={email} onChange={setEmail} inputMode="email" />
            <button className="button" type="submit">Send reset link</button>
          </form>
        )}
        <p><Link to="/">Back to sign in</Link></p>
      </div>
    </Layout>
  );
}

export function ResetPassword() {
  const [params] = useSearchParams();
  const activating = params.get("activate") === "1";
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const errors = useFormErrors();
  async function submit(e: FormEvent) {
    e.preventDefault();
    errors.clear();
    try {
      await api.post("/auth/password-reset/confirm", { token: params.get("token") ?? "", new_password: password });
      setDone(true);
    } catch (err) {
      if (err instanceof ApiError && err.code === "ERR_WEAK_PASSWORD") {
        errors.fromError(new ApiError(400, err.code, err.message, [{ field: "new_password", code: err.code, message: err.message }], null));
      } else errors.fromError(err);
    }
  }
  return (
    <Layout>
      <div className="narrow">
        <h1>{activating ? "Activate your account" : "Choose a new password"}</h1>
        {done ? (
          <Alert kind="success" title="Password set">You can now <Link to="/">sign in</Link>.</Alert>
        ) : (
          <>
            <ErrorSummary title="There is a problem" errors={errors.summary} />
            <form onSubmit={submit} className="card" noValidate>
              <TextField name="new_password" label="New password" type="password" required autoComplete="new-password"
                hint="At least 10 characters." value={password} onChange={setPassword} error={errors.fields.new_password} />
              <button className="button" type="submit">{activating ? "Activate account" : "Set password"}</button>
            </form>
          </>
        )}
      </div>
    </Layout>
  );
}
