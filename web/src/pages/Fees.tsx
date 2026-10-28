import { useState } from "react";
import { api, formatDate, formatDateTime, type ApplicationRecord, type PaymentRow } from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, SelectField, TextArea, TextField } from "../components/ui";
import { ActionErrors, RecordPage, money, useAction } from "../components/workflow";

const TYPES = [
  { value: "deposit", label: "Tuition deposit" },
  { value: "tuition", label: "Tuition instalment" },
  { value: "scholarship", label: "Scholarship or discount" },
  { value: "refund", label: "Refund" },
];

const METHODS = [
  { value: "bank_transfer", label: "Bank transfer" },
  { value: "card", label: "Card" },
  { value: "cash", label: "Cash" },
  { value: "waiver", label: "Waiver" },
];

function FeesView({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const { user } = useAuth();
  const canRecord = user ? ["finance", "senior", "admin"].includes(user.role) : false;
  const position = app.fee_position;
  const payments = app.payments ?? [];
  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({ type: "deposit", amount: "", payment_date: today, method: "bank_transfer", reference: "", reason: "" });
  const { summary, busy, run } = useAction();
  const set = (k: keyof typeof form) => (v: string) => setForm({ ...form, [k]: v });
  const needsReason = form.type === "refund" || form.type === "scholarship";

  if (!position) return <Alert kind="error">You cannot see the fee position for this application.</Alert>;

  return (
    <>
      <ActionErrors summary={summary} />
      <section className="card">
        <h2 className="h3">Fee position</h2>
        {position.agreed_fee === null && (
          <Alert kind="warning" title="No fee schedule applies">
            <p>Until a fee schedule is recorded for this programme and intake, the deposit gate cannot be evidenced and a CAS will be refused.</p>
          </Alert>
        )}
        <dl className="kv">
          <div><dt>Agreed tuition fee</dt><dd>{money(position.agreed_fee, position.currency)}</dd></div>
          <div><dt>Minimum deposit</dt><dd>{money(position.minimum_deposit, position.currency)}</dd></div>
          <div><dt>Paid</dt><dd>{money(position.paid, position.currency)}</dd></div>
          <div><dt>Scholarship</dt><dd>{money(position.scholarship, position.currency)}</dd></div>
          <div><dt>Refunded</dt><dd>{money(position.refunded, position.currency)}</dd></div>
          <div><dt>Outstanding</dt><dd>{money(position.outstanding, position.currency)}</dd></div>
          <div>
            <dt>Deposit gate</dt>
            <dd><span className={`tag tag-${position.deposit_met ? "clean" : "pending"}`}>{position.deposit_met ? "Satisfied" : "Not met"}</span></dd>
          </div>
        </dl>
      </section>

      <section className="card">
        <h2 className="h3">Payments</h2>
        {payments.length === 0 ? <p className="empty">Nothing recorded yet.</p> : (
          <table className="table">
            <caption className="visually-hidden">Payments recorded against this application</caption>
            <thead><tr><th scope="col">Date</th><th scope="col">Type</th><th scope="col">Amount</th><th scope="col">Method</th><th scope="col">Reference</th><th scope="col">Recorded</th></tr></thead>
            <tbody>
              {payments.map((p: PaymentRow) => (
                <tr key={p.id}>
                  <td className="nowrap">{formatDate(p.payment_date)}</td>
                  <td>{TYPES.find((t) => t.value === p.type)?.label ?? p.type}{p.reason && <span className="hint block">{p.reason}</span>}</td>
                  <td className="nowrap">{money(p.amount, position.currency)}</td>
                  <td>{METHODS.find((m) => m.value === p.method)?.label ?? "—"}</td>
                  <td>{p.reference ?? "—"}</td>
                  <td className="nowrap">{formatDateTime(p.recorded_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {canRecord && (
        <section className="card">
          <h2 className="h3">Record a payment</h2>
          <p className="hint">Payments are recorded here, never edited. Correct a mistake with a refund entry and a reason.</p>
          <SelectField name="type" label="Type" required value={form.type} onChange={set("type")} options={TYPES} />
          <TextField name="amount" label={`Amount (${position.currency})`} required width="s" inputMode="decimal" value={form.amount} onChange={set("amount")} />
          <DateField name="payment_date" label="Date received" required value={form.payment_date} onChange={set("payment_date")} />
          <SelectField name="method" label="Method" value={form.method} onChange={set("method")} options={METHODS} />
          <TextField name="reference" label="Bank or receipt reference" value={form.reference} onChange={set("reference")} width="m" />
          {needsReason && (
            <TextArea name="reason" label="Reason" required rows={2} maxLength={1000} value={form.reason} onChange={set("reason")} />
          )}
          <div className="actions">
            <button
              type="button" className="button"
              disabled={busy || !form.amount || Number(form.amount) <= 0 || (needsReason && form.reason.trim().length === 0)}
              onClick={() => void run(
                () => api.post(`/applications/${app.id}/payments`, {
                  type: form.type, amount: form.amount, payment_date: form.payment_date,
                  method: form.method || null, reference: form.reference || null, reason: form.reason || null,
                }),
                async () => { setForm({ ...form, amount: "", reference: "", reason: "" }); await reload(); },
              )}
            >
              Record payment
            </button>
          </div>
        </section>
      )}
    </>
  );
}

export function Fees() {
  return <RecordPage title="Fees" render={(app, reload) => <FeesView app={app} reload={reload} />} />;
}
