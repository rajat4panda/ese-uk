import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, ROLE_LABELS, formatDateTime, type Role } from "../api";
import { useAuth } from "../auth";
import { Alert, ErrorSummary, Layout, SelectField, Spinner, TextField, detailsToErrors } from "../components/ui";

interface Staff { id: number; email: string; full_name: string; role: Role; status: string; last_sign_in_at: string | null; created_at: string }
interface Change { role?: Role; status?: string; full_name?: string; email?: string }
const STAFF_ROLES: Role[] = ["admissions", "compliance", "finance", "senior", "admin"];

export function AdminUsers() {
  const { user } = useAuth();
  const [rows, setRows] = useState<Staff[] | null>(null);
  const [form, setForm] = useState({ full_name: "", email: "", role: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [summary, setSummary] = useState<{ field: string | null; message: string }[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<{ user: Staff; change: Change } | null>(null);
  // FR-100: correcting a colleague's name or work address, in place, without a new account.
  const [editing, setEditing] = useState<Staff | null>(null);
  const [edit, setEdit] = useState({ full_name: "", email: "" });
  const [editError, setEditError] = useState<string | null>(null);

  const load = () => api.get<Staff[]>("/admin/users").then(setRows);
  useEffect(() => { void load(); }, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    setErrors({}); setSummary([]); setNotice(null);
    try {
      const created = await api.post<Staff>("/admin/users", form);
      setNotice(`Account created for ${created.full_name}. An activation link has been emailed to ${created.email}.`);
      setForm({ full_name: "", email: "", role: "" });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(detailsToErrors(err.details));
        setSummary(err.details.length ? err.details.map((d) => ({ field: d.field, message: d.message ?? err.message })) : [{ field: null, message: err.message }]);
      }
    }
  }

  async function confirmChange() {
    if (!pending) return;
    try {
      await api.patch(`/admin/users/${pending.user.id}`, pending.change);
      setNotice(`${pending.user.full_name} updated. Their current sessions have been signed out.`);
      setPending(null);
      await load();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Update failed.";
      if (pending.change.full_name || pending.change.email) {
        setEditing(pending.user);
        setEdit({ full_name: pending.user.full_name, email: pending.user.email });
        setEditError(message);
      } else {
        setNotice(message);
      }
      setPending(null);
    }
  }

  return (
    <Layout wide>
      <h1>Users</h1>
      {notice && <Alert kind="success">{notice}</Alert>}

      {pending && (
        <div className="panel panel-warning" role="alertdialog" aria-labelledby="confirm-h">
          <h2 id="confirm-h" className="h3">Confirm change</h2>
          <p>
            {pending.change.role && <>Change <strong>{pending.user.full_name}</strong> from {ROLE_LABELS[pending.user.role]} to <strong>{ROLE_LABELS[pending.change.role]}</strong>. </>}
            {pending.change.status && <>Set <strong>{pending.user.full_name}</strong> to <strong>{pending.change.status}</strong>. </>}
            {pending.change.full_name && <>Change the name on <strong>{pending.user.email}</strong> from <strong>{pending.user.full_name}</strong> to <strong>{pending.change.full_name}</strong>. </>}
            {pending.change.email && <>Change the address for <strong>{pending.user.full_name}</strong> from <strong>{pending.user.email}</strong> to <strong>{pending.change.email}</strong>. They sign in with it. </>}
            They will be signed out everywhere, and the change is recorded in the audit trail.
          </p>
          <div className="actions">
            <button type="button" className="button" onClick={() => void confirmChange()}>Confirm</button>
            <button type="button" className="button button-secondary" onClick={() => setPending(null)}>Cancel</button>
          </div>
        </div>
      )}

      {!rows ? <Spinner /> : (
        <table className="table">
          <caption className="visually-hidden">Staff accounts</caption>
          <thead><tr><th scope="col">Name</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col">Last sign-in</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const self = r.id === user?.id;
              return (
                <tr key={r.id}>
                  <td>{r.full_name}<span className="hint block">{r.email}</span></td>
                  <td>
                    {self ? ROLE_LABELS[r.role] : (
                      <select className="input select compact" aria-label={`Role for ${r.full_name}`} value={r.role}
                        onChange={(e) => setPending({ user: r, change: { role: e.target.value as Role } })}>
                        {STAFF_ROLES.map((role) => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}
                      </select>
                    )}
                  </td>
                  <td><span className={`tag tag-${r.status}`}>{r.status}</span></td>
                  <td>{r.last_sign_in_at ? formatDateTime(r.last_sign_in_at) : "Never"}</td>
                  <td>
                    <button type="button" className="link-button" onClick={() => {
                      setEditing(r); setEdit({ full_name: r.full_name, email: r.email }); setEditError(null);
                    }}>Correct details</button>
                    {" · "}
                    {self ? <span className="hint">You</span> : r.status === "active"
                      ? <button type="button" className="link-button danger" onClick={() => setPending({ user: r, change: { status: "deactivated" } })}>Deactivate</button>
                      : <button type="button" className="link-button" onClick={() => setPending({ user: r, change: { status: "active" } })}>Reactivate</button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {editing && (
        <section className="card narrow-card" aria-labelledby="edit-h">
          <h2 id="edit-h" className="h3">Correct details for {editing.full_name}</h2>
          <p className="hint">
            For a misspelling or a changed address. The account, its role and its history stay as they
            are, and the old value is recorded in the audit trail.
          </p>
          {editError && <Alert kind="error">{editError}</Alert>}
          <TextField name="edit_full_name" label="Full name" required value={edit.full_name}
            onChange={(v) => setEdit({ ...edit, full_name: v })} />
          <TextField name="edit_email" label="Work email" type="email" required value={edit.email}
            onChange={(v) => setEdit({ ...edit, email: v })} />
          <div className="actions">
            <button
              type="button" className="button"
              disabled={edit.full_name.trim().length < 2 || !edit.email.includes("@")
                || (edit.full_name.trim() === editing.full_name && edit.email.trim().toLowerCase() === editing.email)}
              onClick={() => {
                const change: Change = {};
                if (edit.full_name.trim() !== editing.full_name) change.full_name = edit.full_name.trim();
                if (edit.email.trim().toLowerCase() !== editing.email) change.email = edit.email.trim().toLowerCase();
                setPending({ user: editing, change });
                setEditing(null);
              }}
            >
              Save correction
            </button>
            <button type="button" className="button button-secondary" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </section>
      )}

      <section className="card narrow-card">
        <h2 className="h3">Add a staff member</h2>
        <p className="hint">They will receive an email with a link to set their password. Each person holds exactly one role.</p>
        <ErrorSummary title="There is a problem" errors={summary} />
        <form onSubmit={create} noValidate>
          <TextField name="full_name" label="Full name" required value={form.full_name} onChange={(v) => setForm({ ...form, full_name: v })} error={errors.full_name} />
          <TextField name="email" label="Work email" type="email" required value={form.email} onChange={(v) => setForm({ ...form, email: v })} error={errors.email} />
          <SelectField name="role" label="Role" required value={form.role} onChange={(v) => setForm({ ...form, role: v })} error={errors.role}
            options={STAFF_ROLES.map((r) => ({ value: r, label: ROLE_LABELS[r] }))} />
          <button className="button" type="submit">Create account</button>
        </form>
      </section>
    </Layout>
  );
}
