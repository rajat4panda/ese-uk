import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAuth } from "../auth";
import { ROLE_LABELS, type ErrorDetail } from "../api";
import { initials, isCurrent, navFor } from "../nav";

// ---------------------------------------------------------------------------------------------
// Chrome, shell and sidebar — the prototype's frame (design/prototype/ese-prototype.html)
// ---------------------------------------------------------------------------------------------

/** The ESE logo lifted from the approved prototype. Falls back to the wordmark if it is missing. */
export function Logo({ mark = false, height, alt = "European School of Economics" }: { mark?: boolean; height?: number; alt?: string }) {
  const [ok, setOk] = useState(true);
  if (!ok) return <span className="brand-text">European School of Economics</span>;
  return (
    <img
      src={mark ? "/brand/ese-mark.png" : "/brand/ese-logo.png"}
      alt={alt}
      style={height ? { height } : undefined}
      onError={() => setOk(false)}
    />
  );
}

/** Top chrome: white, four-pixel ESE red rule, real logo, system name, Finteam credit. */
export function Chrome() {
  return (
    <header className="chrome">
      <Link to="/" className="chrome-wordmark" aria-label="European School of Economics — home">
        <Logo />
      </Link>
      <div className="chrome-title">
        <div className="chrome-system-name">International Student Recruitment &amp; Compliance System</div>
        <div className="chrome-tag">Recruit&nbsp; ·&nbsp; Assess&nbsp; ·&nbsp; Sponsor&nbsp; ·&nbsp; Support&nbsp; ·&nbsp; Succeed</div>
      </div>
      <div className="chrome-right">
        Powered by eMigrant<sup>®</sup> &nbsp;|&nbsp; Designed for a compliant global future
        <div className="chrome-finteam">FINTEAM</div>
      </div>
    </header>
  );
}

/** The maroon workspace sidebar. Its items come from nav.ts, so they match the caller's role. */
function RoleSidebar() {
  const { user, signOut } = useAuth();
  const { pathname, search } = useLocation();
  if (!user) return null;
  const items = navFor(user.role);
  return (
    <aside className="sidebar">
      <div className="sidebar-mark">
        <span><Logo height={22} alt="" /></span>
      </div>
      {/* Plain links, not NavLink: several items share the /staff path and differ only by query,
          so "you are here" is decided by nav.ts, not by prefix matching. */}
      <nav aria-label="Sections">
        {items.map((item) => (
          <Link key={item.label} to={item.to} aria-current={isCurrent(item, pathname, search) ? "page" : undefined}>
            {item.label}
          </Link>
        ))}
      </nav>
      <div className="sidebar-foot">
        <span>{user.name}</span>
        <br />
        <span className="role">{ROLE_LABELS[user.role]}</span>
        <br />
        <button type="button" className="link-button" onClick={() => void signOut()}>Sign out</button>
      </div>
    </aside>
  );
}

function AccountBar() {
  const { user, signOut } = useAuth();
  if (!user) return null;
  return (
    <div className="account-bar">
      <span className="avatar sm" aria-hidden="true">{initials(user.name)}</span>
      <span>{user.name}</span>
      {user.role !== "applicant" && <span className="role-chip">{ROLE_LABELS[user.role]}</span>}
      <button type="button" className="link-button" onClick={() => void signOut()}>Sign out</button>
    </div>
  );
}

function Footer() {
  return (
    <footer className="site-footer">
      <div className="footer-inner">
        <span>ESE Insight Limited · Student sponsor licence 0MXJ7G5D6</span>
        <span>·</span>
        <span>Powered by eMigrant<sup>®</sup> · Finteam Ltd</span>
      </div>
    </footer>
  );
}

/**
 * Page frame. Signed-in staff get the maroon workspace sidebar; applicants and signed-out visitors
 * get the centred portal column, as in prototype screens 1–4.
 *
 * `wide` keeps its old meaning — full-width content rather than a reading-width column — and
 * `bare` is for screens that draw their own frame (the applicant welcome hero, the wizard).
 */
export function Layout({ children, wide = false, bare = false, sidebar }: {
  children: ReactNode;
  wide?: boolean;
  bare?: boolean;
  /** Replaces the role sidebar — used by case screens, which show the record instead (prototype 6–8). */
  sidebar?: ReactNode;
}) {
  const { user } = useAuth();
  const staff = Boolean(user && user.role !== "applicant");
  return (
    <>
      <a href="#main" className="skip-link">Skip to main content</a>
      <Chrome />
      {staff ? (
        <div className="shell">
          {sidebar ?? <RoleSidebar />}
          <main id="main" className="workspace" tabIndex={-1}>{children}</main>
        </div>
      ) : (
        <>
          {user && <AccountBar />}
          <main id="main" className={bare ? "bare" : wide ? "container" : "container narrow-page"} tabIndex={-1}>
            {children}
          </main>
        </>
      )}
      <Footer />
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------------------------
export function Alert({ kind = "info", title, children }: { kind?: "info" | "success" | "warning" | "error"; title?: string; children?: ReactNode }) {
  return (
    <div className={`alert alert-${kind}`} role={kind === "error" ? "alert" : "status"}>
      {title && <p className="alert-title">{title}</p>}
      {children}
    </div>
  );
}

/** GOV.UK-style error summary: focused on appearance, each item links to its field. */
export function ErrorSummary({ title, errors, onSelect }: {
  title: string;
  errors: { field: string | null; message: string; step?: number | null }[];
  onSelect?: (field: string | null, step?: number | null) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (errors.length) ref.current?.focus();
  }, [errors]);
  if (!errors.length) return null;
  return (
    <div className="error-summary" role="alert" tabIndex={-1} ref={ref} aria-labelledby="error-summary-title">
      <h2 id="error-summary-title">{title}</h2>
      <ul>
        {errors.map((e, i) => (
          <li key={`${e.field}-${i}`}>
            {onSelect ? (
              <a href={e.field ? `#f-${e.field}` : "#main"} onClick={(ev) => { ev.preventDefault(); onSelect(e.field, e.step); }}>
                {e.message}
              </a>
            ) : e.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function detailsToErrors(details: ErrorDetail[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const d of details) if (d.field && !out[d.field]) out[d.field] = d.message ?? "Check this answer.";
  return out;
}

// ---------------------------------------------------------------------------------------------
// Form fields — labelled, with hint and error wired through aria-describedby (NFR-11)
// ---------------------------------------------------------------------------------------------
interface FieldProps {
  name: string;
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
  /** Filters and search boxes are optional by nature; the suffix is noise on those. */
  hideOptional?: boolean;
}

function FieldShell({ name, label, hint, error, required, hideOptional, children, id }: FieldProps & { children: ReactNode; id: string }) {
  return (
    <div className={`field${error ? " field-error" : ""}`} id={`f-${name}`}>
      {/* The prototype marks required fields with a red asterisk. It is drawn in CSS so that it
          stays out of the label's text: the requirement is carried to assistive technology by
          aria-required on the control itself. */}
      <label htmlFor={id} className="label" data-required={required ? "" : undefined}>
        {label}
        {!required && !hideOptional && <span className="optional"> (optional)</span>}
      </label>
      {hint && <p className="hint" id={`${id}-hint`}>{hint}</p>}
      {error && <p className="error-message" id={`${id}-error`}><span className="visually-hidden">Error: </span>{error}</p>}
      {children}
    </div>
  );
}

function describedBy(id: string, hint?: string, error?: string) {
  return [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
}

export function TextField(props: FieldProps & {
  value: string | number | null | undefined;
  onChange: (v: string) => void;
  type?: string;
  autoComplete?: string;
  width?: "s" | "m" | "l";
  readOnly?: boolean;
  inputMode?: "text" | "email" | "tel" | "numeric" | "decimal";
}) {
  const id = useId();
  return (
    <FieldShell {...props} id={id}>
      <input
        id={id}
        name={props.name}
        className={`input w-${props.width ?? "l"}`}
        type={props.type ?? "text"}
        value={props.value ?? ""}
        onChange={(e) => props.onChange(e.target.value)}
        autoComplete={props.autoComplete ?? "off"}
        aria-invalid={props.error ? true : undefined}
        aria-describedby={describedBy(id, props.hint, props.error)}
        readOnly={props.readOnly}
        inputMode={props.inputMode}
        aria-required={props.required || undefined}
      />
    </FieldShell>
  );
}

export function TextArea(props: FieldProps & { value: string | null | undefined; onChange: (v: string) => void; rows?: number; maxLength?: number; minLength?: number }) {
  const id = useId();
  const len = (props.value ?? "").length;
  return (
    <FieldShell {...props} id={id}>
      <textarea
        id={id}
        name={props.name}
        className="input textarea"
        rows={props.rows ?? 6}
        value={props.value ?? ""}
        onChange={(e) => props.onChange(e.target.value)}
        aria-invalid={props.error ? true : undefined}
        aria-describedby={describedBy(id, props.hint, props.error)}
        maxLength={props.maxLength}
        aria-required={props.required || undefined}
      />
      {props.maxLength && (
        <p className="counter" aria-live="polite">
          {len} of {props.maxLength} characters{props.minLength && len < props.minLength ? ` — at least ${props.minLength} needed` : ""}
        </p>
      )}
    </FieldShell>
  );
}

export function SelectField(props: FieldProps & {
  value: string | number | null | undefined;
  onChange: (v: string) => void;
  options: { value: string | number; label: string }[];
  placeholder?: string;
}) {
  const id = useId();
  return (
    <FieldShell {...props} id={id}>
      <select
        id={id}
        name={props.name}
        className="input select"
        value={props.value ?? ""}
        onChange={(e) => props.onChange(e.target.value)}
        aria-invalid={props.error ? true : undefined}
        aria-describedby={describedBy(id, props.hint, props.error)}
        aria-required={props.required || undefined}
      >
        <option value="">{props.placeholder ?? "Select…"}</option>
        {props.options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </FieldShell>
  );
}

export function DateField(props: FieldProps & { value: string | null | undefined; onChange: (v: string) => void }) {
  return <TextField {...props} type="date" width="m" />;
}

export function RadioGroup(props: FieldProps & {
  value: string | boolean | null | undefined;
  onChange: (v: string) => void;
  options: { value: string; label: string; hint?: string }[];
  inline?: boolean;
}) {
  const id = useId();
  const current = props.value === true ? "true" : props.value === false ? "false" : (props.value ?? "");
  return (
    <div className={`field${props.error ? " field-error" : ""}`} id={`f-${props.name}`}>
      <fieldset aria-describedby={describedBy(id, props.hint, props.error)}>
        <legend className="label" data-required={props.required ? "" : undefined}>
          {props.label}
          {!props.required && !props.hideOptional && <span className="optional"> (optional)</span>}
        </legend>
        {props.hint && <p className="hint" id={`${id}-hint`}>{props.hint}</p>}
        {props.error && <p className="error-message" id={`${id}-error`}><span className="visually-hidden">Error: </span>{props.error}</p>}
        <div className={props.inline ? "radios inline" : "radios"}>
          {props.options.map((o) => (
            <label key={o.value} className="radio">
              <input type="radio" name={`${id}-${props.name}`} value={o.value} checked={current === o.value} onChange={() => props.onChange(o.value)} />
              <span>
                {o.label}
                {o.hint && <span className="hint block">{o.hint}</span>}
              </span>
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  );
}

export function Checkbox(props: FieldProps & { checked: boolean; onChange: (v: boolean) => void; children?: ReactNode }) {
  const id = useId();
  return (
    <div className={`field${props.error ? " field-error" : ""}`} id={`f-${props.name}`}>
      {props.error && <p className="error-message" id={`${id}-error`}><span className="visually-hidden">Error: </span>{props.error}</p>}
      <label className="checkbox" htmlFor={id}>
        <input id={id} type="checkbox" checked={props.checked} onChange={(e) => props.onChange(e.target.checked)}
          aria-invalid={props.error ? true : undefined} aria-describedby={props.error ? `${id}-error` : undefined} />
        <span>{props.label}{props.children}</span>
      </label>
    </div>
  );
}

export function StatusBadge({ status, label }: { status: string; label: string }) {
  return <span className={`badge badge-${status}`}>{label}</span>;
}

/** The confirmation tick from prototype screens 4 and 10. Decorative — the heading carries the news. */
export function Tick({ size = 80 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 86 86" aria-hidden="true" focusable="false">
      <circle cx="43" cy="43" r="39" fill="none" stroke="#166534" strokeWidth="5" />
      <path d="M27 44 l11 11 21-24" fill="none" stroke="#166534" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <p className="loading" role="status">{label}</p>;
}
