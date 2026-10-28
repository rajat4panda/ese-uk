import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useParams } from "react-router-dom";
import {
  ApiError, ROLE_LABELS, STATUS_LABELS, api, formatDateTime, nextActions,
  type ApplicationRecord, type ChecklistItem, type ErrorDetail,
} from "../api";
import { useAuth } from "../auth";
import { initials } from "../nav";
import { Alert, ErrorSummary, Layout, Spinner, StatusBadge } from "./ui";

// ---------------------------------------------------------------------------------------------
// Loading one application record, with a reload after every action
// ---------------------------------------------------------------------------------------------
export function useRecord() {
  const { id } = useParams();
  const [app, setApp] = useState<ApplicationRecord | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setApp(await api.get<ApplicationRecord>(`/applications/${id}`));
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.message : "The record could not be loaded.");
    }
  }, [id]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { id: id!, app, reload, loadError };
}

/** Turns an ApiError into an error summary plus per-field messages (LLD 15, 14.2). */
export function useAction() {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function run<T>(fn: () => Promise<T>, after?: (result: T) => void | Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      const result = await fn();
      await after?.(result);
      return result;
    } catch (e) {
      if (e instanceof ApiError) setError(e);
      else setError(new ApiError(0, "ERR_NETWORK", "The action could not be completed. Try again.", [], null));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  const summary = error
    ? [{ field: null, message: error.message }, ...error.details
        .filter((d: ErrorDetail) => d.message && d.message !== error.message)
        .map((d: ErrorDetail) => ({ field: d.field, message: d.message! }))]
    : [];

  return { error, summary, busy, run, clear: () => setError(null) };
}

export function ActionErrors({ summary }: { summary: { field: string | null; message: string }[] }) {
  if (!summary.length) return null;
  return <ErrorSummary title="This action was not completed" errors={summary} />;
}

// ---------------------------------------------------------------------------------------------
// Record shell: header, stage navigation, back link
// ---------------------------------------------------------------------------------------------
/**
 * Case sidebar — prototype screens 6–8. While a caseworker is inside one application the workspace
 * navigation is replaced by that application: who it is, and the stages of its assessment. Stages
 * are drawn from nextActions(), so the list is already shaped by role and by where the record has
 * reached; nothing is offered that the caller cannot open.
 */
function CaseSidebar({ app }: { app: ApplicationRecord }) {
  const { user, signOut } = useAuth();
  const actions = user ? nextActions(app.status, user.role) : [];
  const name = app.applicant_name ?? "Applicant";
  return (
    <aside className="case-sb">
      <div className="case-head">
        <span className="avatar" aria-hidden="true">{initials(name)}</span>
        <span className="who">
          <b>{app.ref}</b>
          {name}
          <span className="course">{app.programme ?? "No programme"} · {app.intake ?? "No intake"}</span>
        </span>
      </div>
      <nav aria-label="Application stages">
        <NavLink end to={`/staff/applications/${app.id}`}>Application summary</NavLink>
        {actions.map((a) => (
          <NavLink key={a.to} to={`/staff/applications/${app.id}/${a.to}`}>{a.label}</NavLink>
        ))}
      </nav>
      <div className="sidebar-foot">
        <Link to="/staff">← All applications</Link>
        {user && (
          <>
            <br />
            <span>{user.name}</span>
            <br />
            <span className="role">{ROLE_LABELS[user.role]}</span>
            <br />
            <button type="button" className="link-button" onClick={() => void signOut()}>Sign out</button>
          </>
        )}
      </div>
    </aside>
  );
}

export function RecordShell({ app, title, children }: { app: ApplicationRecord; title: string; children: ReactNode }) {
  return (
    <Layout sidebar={<CaseSidebar app={app} />}>
      <div className="page-head">
        <div>
          <h1>{title}</h1>
          <span className="sub">{app.ref} · {app.applicant_name ?? "—"}</span>
        </div>
        <StatusBadge status={app.status} label={STATUS_LABELS[app.status] ?? app.status} />
      </div>
      {children}
    </Layout>
  );
}

export function RecordPage({ title, render }: { title: string; render: (app: ApplicationRecord, reload: () => Promise<void>) => ReactNode }) {
  const { app, reload, loadError } = useRecord();
  if (loadError) return <Layout><Alert kind="error">{loadError}</Alert></Layout>;
  if (!app) return <Layout><Spinner /></Layout>;
  return <RecordShell app={app} title={title}>{render(app, reload)}</RecordShell>;
}

// ---------------------------------------------------------------------------------------------
// Checklist editor — one row per definition, mandatory rows marked
// ---------------------------------------------------------------------------------------------
export function Checklist({ items, value, onChange, readOnly }: {
  items: ChecklistItem[];
  value: Record<string, boolean>;
  onChange: (next: Record<string, boolean>) => void;
  readOnly?: boolean;
}) {
  return (
    <fieldset className="checklist">
      <legend className="label">Checks</legend>
      <p className="hint">Items marked required must be satisfied before this stage can be completed.</p>
      <ul>
        {items.map((item) => (
          <li key={item.item_key}>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={Boolean(value[item.item_key])}
                disabled={readOnly}
                onChange={(e) => onChange({ ...value, [item.item_key]: e.target.checked })}
              />
              <span>
                {item.label}
                {item.mandatory && <span className="req"> required</span>}
              </span>
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}

export function checklistValue(items: ChecklistItem[]): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const i of items) out[i.item_key] = i.satisfied === true;
  return out;
}

// ---------------------------------------------------------------------------------------------
// Confirmation for irreversible actions (LLD 14.2)
// ---------------------------------------------------------------------------------------------
export function Confirm({ title, consequence, confirmLabel, busy, disabled, onConfirm, onCancel, children }: {
  title: string;
  consequence: string;
  confirmLabel: string;
  busy?: boolean;
  disabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  return (
    <div className="confirm" role="alertdialog" aria-labelledby="confirm-h" tabIndex={-1} ref={ref}>
      <h2 id="confirm-h" className="h3">{title}</h2>
      <p>{consequence}</p>
      {children}
      <div className="actions">
        <button type="button" className="button button-warning" onClick={onConfirm} disabled={busy || disabled}>
          {busy ? "Working…" : confirmLabel}
        </button>
        <button type="button" className="button button-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </div>
  );
}

/** An action the caller cannot take yet: shown, disabled, with the reason beside it (LLD 14.2). */
export function BlockedAction({ label, reasons }: { label: string; reasons: string[] }) {
  return (
    <div className="blocked">
      <button type="button" className="button" disabled aria-describedby="blocked-why">{label}</button>
      <div id="blocked-why" className="blocked-why">
        <p className="label">Blocked because:</p>
        <ul>{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      </div>
    </div>
  );
}

export function SignOff({ by, at, prefix = "Completed" }: { by: string | null; at: string | null; prefix?: string }) {
  if (!by || !at) return null;
  return <p className="signoff">{prefix} by {by} on {formatDateTime(at)}.</p>;
}

/** A failed load says so: an empty state must never stand in for an error (LLD 14.2). */
export function LoadFailed({ what }: { what: string }) {
  return (
    <Alert kind="error" title={`${what} could not be loaded`}>
      <p>Refresh the page to try again. If it keeps happening, report it with the time and the record reference.</p>
    </Alert>
  );
}

export function money(value: string | null | undefined, currency = "GBP") {
  if (value === null || value === undefined) return "—";
  return `${currency} ${Number(value).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
