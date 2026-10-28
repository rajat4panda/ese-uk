import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, formatDate, formatDateTime, openOfferLetter,
  type ApplicationRecord, type Offer, type Summary,
} from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, Layout, RadioGroup, SelectField, Spinner, TextArea } from "../components/ui";
import { ActionErrors, Confirm, LoadFailed, RecordPage, useAction } from "../components/workflow";

interface LibraryCondition { id: number; description: string; category: string }

const TYPE_LABELS: Record<string, string> = { conditional: "Conditional offer", unconditional: "Unconditional offer" };

// ---------------------------------------------------------------------------------------------
// Staff: build, issue and manage the offer (FR-35 to FR-42)
// ---------------------------------------------------------------------------------------------
function OfferBuilderForm({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const [library, setLibrary] = useState<LibraryCondition[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [type, setType] = useState("conditional");
  const [picked, setPicked] = useState<string[]>([]);
  const [custom, setCustom] = useState("");
  const [expiry, setExpiry] = useState("");
  const { summary, busy, run } = useAction();

  useEffect(() => {
    api.get<LibraryCondition[]>("/reference/offer-conditions").then(setLibrary).catch(() => setFailed(true));
  }, []);

  const conditions = [...picked, ...custom.split("\n").map((s) => s.trim()).filter(Boolean)];

  async function create() {
    await run(
      () => api.post<Offer>(`/applications/${app.id}/offer`, {
        offer_type: type,
        conditions: type === "conditional" ? conditions : [],
        expiry_date: expiry || null,
      }),
      () => reload(),
    );
  }

  return (
    <>
      <ActionErrors summary={summary} />
      <section className="card">
        <h2 className="h3">Build the offer</h2>
        <RadioGroup
          name="offer_type" label="Offer type" required value={type} onChange={setType}
          options={[
            { value: "conditional", label: "Conditional", hint: "The applicant must satisfy the conditions below before a CAS can be assigned." },
            { value: "unconditional", label: "Unconditional", hint: "No outstanding conditions." },
          ]}
        />
        {type === "conditional" && (
          <>
            <fieldset className="checklist">
              <legend className="label">Standard conditions</legend>
              {failed ? <LoadFailed what="The standard conditions" /> : !library ? <Spinner /> : (
                <ul>
                  {library.map((c) => (
                    <li key={c.id}>
                      <label className="checkbox">
                        <input
                          type="checkbox" checked={picked.includes(c.description)}
                          onChange={(e) => setPicked(e.target.checked
                            ? [...picked, c.description]
                            : picked.filter((d) => d !== c.description))}
                        />
                        <span>{c.description}<span className="hint block">{c.category.replace(/_/g, " ")}</span></span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </fieldset>
            <TextArea
              name="custom_conditions" label="Additional conditions" rows={3} value={custom} onChange={setCustom}
              hint="One condition per line."
            />
          </>
        )}
        <DateField name="expiry_date" label="Offer expires" value={expiry} onChange={setExpiry}
          hint="The date by which the applicant must respond." />
        <div className="actions">
          <button
            type="button" className="button" disabled={busy || (type === "conditional" && conditions.length === 0)}
            onClick={() => void create()}
          >
            Create offer
          </button>
        </div>
        {type === "conditional" && conditions.length === 0 && (
          <p className="hint">A conditional offer needs at least one condition.</p>
        )}
      </section>
    </>
  );
}

function OfferDetail({ app, offer, reload }: { app: ApplicationRecord; offer: Offer; reload: () => Promise<void> }) {
  const { user } = useAuth();
  const senior = user?.role === "senior" || user?.role === "admin";
  const [method, setMethod] = useState("email");
  const [overrideReason, setOverrideReason] = useState("");
  const [overriding, setOverriding] = useState(false);
  const { summary, busy, run } = useAction();
  const outstanding = offer.conditions.filter((c) => c.status !== "met");

  return (
    <>
      <ActionErrors summary={summary} />
      <section className="card">
        <h2 className="h3">{TYPE_LABELS[offer.offer_type] ?? offer.offer_type}</h2>
        <dl className="kv">
          <div><dt>Issued</dt><dd>{offer.issued_at ? `${formatDateTime(offer.issued_at)} by ${offer.issue_method}` : "Not yet issued"}</dd></div>
          <div><dt>Expires</dt><dd>{formatDate(offer.expiry_date)}</dd></div>
          <div><dt>Applicant response</dt><dd>{offer.response ? `${offer.response === "accepted" ? "Accepted" : "Declined"} on ${formatDateTime(offer.response_at!)}` : "Awaiting response"}</dd></div>
          <div><dt>Conditions</dt><dd>{offer.conditions.length === 0 ? "None" : `${offer.conditions.length - outstanding.length} of ${offer.conditions.length} met`}</dd></div>
        </dl>
        {offer.has_letter && (
          <p><button type="button" className="link-button" onClick={() => void openOfferLetter(offer.id)}>Open the offer letter</button></p>
        )}
        {!offer.issued_at && (
          <div className="inline-form">
            <SelectField
              name="issue_method" label="Issue the offer by" value={method} onChange={setMethod} required
              options={[{ value: "email", label: "Email" }, { value: "portal", label: "Applicant portal" }, { value: "post", label: "Post" }]}
            />
            <button type="button" className="button" disabled={busy}
              onClick={() => void run(() => api.post(`/offers/${offer.id}/issue`, { issue_method: method }), () => reload())}>
              Issue offer
            </button>
          </div>
        )}
      </section>

      {offer.conditions.length > 0 && (
        <section className="card">
          <h2 className="h3">Conditions</h2>
          <p className="hint">A CAS cannot be assigned while a condition is outstanding, unless a senior user overrides it.</p>
          <table className="table">
            <caption className="visually-hidden">Offer conditions</caption>
            <thead><tr><th scope="col">Condition</th><th scope="col">Status</th><th scope="col">Verified</th><th scope="col"><span className="visually-hidden">Action</span></th></tr></thead>
            <tbody>
              {offer.conditions.map((c) => (
                <tr key={c.id}>
                  <td>{c.description}</td>
                  <td><span className={`tag tag-${c.status === "met" ? "clean" : "pending"}`}>{c.status === "met" ? "Met" : "Outstanding"}</span></td>
                  <td>{c.verified_at ? formatDateTime(c.verified_at) : "—"}</td>
                  <td>
                    <button
                      type="button" className="link-button" disabled={busy}
                      onClick={() => void run(
                        () => api.patch(`/offers/${offer.id}/conditions/${c.id}`, { status: c.status === "met" ? "outstanding" : "met" }),
                        () => reload(),
                      )}
                    >
                      {c.status === "met" ? "Mark outstanding" : "Mark met"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {offer.override_reason && (
            <Alert kind="warning" title="Conditions overridden">
              <p>{offer.override_reason}</p>
            </Alert>
          )}
          {outstanding.length > 0 && !offer.override_reason && senior && (
            overriding ? (
              <Confirm
                title={`Override ${outstanding.length} outstanding condition(s) on ${app.ref}?`}
                consequence="The outstanding conditions will no longer block a CAS for this application. The tuition deposit gate is separate and still applies. The override, your name and the reason are written to the audit trail and cannot be removed."
                confirmLabel="Record override" busy={busy} disabled={overrideReason.trim().length < 10}
                onConfirm={() => void run(() => api.post(`/offers/${offer.id}/override`, { reason: overrideReason }), async () => { setOverriding(false); await reload(); })}
                onCancel={() => setOverriding(false)}
              >
                <TextArea name="override_reason" label="Reason for the override" required rows={3} maxLength={2000}
                  value={overrideReason} onChange={setOverrideReason}
                  error={overrideReason.trim().length < 10 ? "Give a reason of at least 10 characters." : undefined} />
              </Confirm>
            ) : (
              <div className="actions">
                <button type="button" className="button button-secondary" onClick={() => setOverriding(true)}>
                  Override outstanding conditions
                </button>
              </div>
            )
          )}
          {outstanding.length > 0 && !offer.override_reason && !senior && (
            <p className="hint">Only a senior user can override outstanding conditions.</p>
          )}
        </section>
      )}

      {offer.issued_at && !offer.response && (
        <section className="card">
          <h2 className="h3">Record a response received outside the portal</h2>
          <p className="hint">Use this only when {app.applicant_name ?? "the applicant"} has replied by email or post (FR-39).</p>
          <div className="actions">
            <button type="button" className="button" disabled={busy}
              onClick={() => void run(() => api.post(`/offers/${offer.id}/response`, { response: "accepted" }), () => reload())}>
              Record acceptance
            </button>
            <button type="button" className="button button-secondary" disabled={busy}
              onClick={() => void run(() => api.post(`/offers/${offer.id}/response`, { response: "declined" }), () => reload())}>
              Record decline
            </button>
          </div>
        </section>
      )}
    </>
  );
}

export function OfferBuilder() {
  return <RecordPage title="Offer" render={(app, reload) => (
    app.offer
      ? <OfferDetail app={app} offer={app.offer} reload={reload} />
      : app.status === "offer_pending"
        ? <OfferBuilderForm app={app} reload={reload} />
        : <Alert kind="info">An offer can only be built once the academic assessment has been completed with a recommendation to offer.</Alert>
  )} />;
}

// ---------------------------------------------------------------------------------------------
// Applicant: respond to the offer (FR-39)
// ---------------------------------------------------------------------------------------------
export function MyOffer() {
  const [app, setApp] = useState<ApplicationRecord | null>(null);
  const [none, setNone] = useState(false);
  const [confirming, setConfirming] = useState<"accepted" | "declined" | null>(null);
  const { summary, busy, run } = useAction();

  async function load() {
    const mine = await api.get<Summary[]>("/applications/mine");
    if (!mine.length) { setNone(true); return; }
    setApp(await api.get<ApplicationRecord>(`/applications/${mine[0].id}`));
  }

  useEffect(() => { void load().catch(() => setNone(true)); }, []);

  if (none) return <Layout><h1>Your offer</h1><p>You do not have an offer yet. <Link to="/my">Back to your application</Link></p></Layout>;
  if (!app) return <Layout><Spinner /></Layout>;
  const offer = app.offer;
  if (!offer || !offer.issued_at) {
    return (
      <Layout>
        <h1>Your offer</h1>
        <p>No offer has been issued yet. We will email you as soon as there is news. <Link to="/my">Back to your application</Link></p>
      </Layout>
    );
  }

  return (
    <Layout>
      <p><Link to="/my">← Your application</Link></p>
      <h1>Your offer</h1>
      <ActionErrors summary={summary} />
      {offer.response ? (
        <Alert kind={offer.response === "accepted" ? "success" : "info"} title={offer.response === "accepted" ? "You accepted this offer" : "You declined this offer"}>
          <p>Recorded on {formatDateTime(offer.response_at!)}.</p>
        </Alert>
      ) : null}
      <section className="card">
        <h2 className="h3">{TYPE_LABELS[offer.offer_type] ?? offer.offer_type} — {app.programme ?? "your programme"}</h2>
        <dl className="kv">
          <div><dt>Reference</dt><dd>{app.ref}</dd></div>
          <div><dt>Intake</dt><dd>{app.intake ?? "—"}</dd></div>
          <div><dt>Respond by</dt><dd>{formatDate(offer.expiry_date)}</dd></div>
        </dl>
        {offer.conditions.length > 0 && (
          <>
            <h3 className="h4">Conditions you need to meet</h3>
            <ul className="condition-list">
              {offer.conditions.map((c) => (
                <li key={c.id}>
                  {c.description} <span className={`tag tag-${c.status === "met" ? "clean" : "pending"}`}>{c.status === "met" ? "Met" : "Outstanding"}</span>
                </li>
              ))}
            </ul>
          </>
        )}
        {offer.has_letter && (
          <p><button type="button" className="link-button" onClick={() => void openOfferLetter(offer.id)}>Read your offer letter</button></p>
        )}
        {!offer.response && !confirming && (
          <div className="actions">
            <button type="button" className="button" onClick={() => setConfirming("accepted")}>Accept this offer</button>
            <button type="button" className="button button-secondary" onClick={() => setConfirming("declined")}>Decline this offer</button>
          </div>
        )}
      </section>
      {confirming && (
        <Confirm
          title={confirming === "accepted" ? `Accept your offer for ${app.programme ?? "this programme"}?` : "Decline this offer?"}
          consequence={confirming === "accepted"
            ? "We will start your pre-CAS compliance checks. You can still withdraw later, but the offer cannot be un-accepted."
            : "Declining ends this application. You would need to apply again for a future intake."}
          confirmLabel={confirming === "accepted" ? "Accept offer" : "Decline offer"} busy={busy}
          onCancel={() => setConfirming(null)}
          onConfirm={() => void run(
            () => api.post(`/offers/${offer.id}/response`, { response: confirming }),
            async () => { setConfirming(null); await load(); },
          )}
        />
      )}
    </Layout>
  );
}
