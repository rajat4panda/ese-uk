import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { RISK_LABELS, api, formatDate, type Assessment, type ApplicationRecord } from "../api";
import { useAuth } from "../auth";
import { Alert, DateField, RadioGroup, SelectField, TextArea, TextField } from "../components/ui";
import {
  ActionErrors, Checklist, checklistValue, Confirm, RecordPage, SignOff, useAction,
} from "../components/workflow";

const ACADEMIC_OUTCOMES = [
  { value: "recommend_offer", label: "Recommend an offer", hint: "Moves the application on to the offer stage." },
  { value: "request_info", label: "Request more information", hint: "Returns the application to the applicant to edit." },
  { value: "reject", label: "Reject the application", hint: "Ends the application. This cannot be undone." },
];

// ---------------------------------------------------------------------------------------------
// Academic assessment (FR-27 to FR-34)
// ---------------------------------------------------------------------------------------------
function AcademicForm({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const academic = app.academic as Assessment;
  const done = Boolean(academic.completed_at);
  const nav = useNavigate();
  const [checks, setChecks] = useState(() => checklistValue(academic.checklist));
  const [comments, setComments] = useState(academic.comments ?? "");
  const [outcome, setOutcome] = useState(academic.outcome ?? "");
  const [englishEvidence, setEnglishEvidence] = useState(academic.english_evidence ?? "");
  const [englishStandard, setEnglishStandard] = useState(academic.english_standard ?? "");
  const [interview, setInterview] = useState({
    interview_date: academic.interview_date ?? "", interviewer: academic.interviewer ?? "",
    interview_outcome: academic.interview_outcome ?? "", interview_notes: academic.interview_notes ?? "",
  });
  const [confirming, setConfirming] = useState<"complete" | "reject" | null>(null);
  const { summary, busy, run } = useAction();

  const needsReason = outcome === "request_info" || outcome === "reject";
  const reasonTooShort = needsReason && comments.trim().length < 10;

  async function save(complete: boolean) {
    const body = {
      checklist: checks, outcome: outcome || null, comments, complete,
      english_evidence: englishEvidence || null,
      english_standard: englishStandard.trim() || null,
      interview_date: interview.interview_date || null,
      interviewer: interview.interviewer.trim() || null,
      interview_outcome: interview.interview_outcome || null,
      interview_notes: interview.interview_notes.trim() || null,
    };
    await run(() => api.post<Assessment>(`/applications/${app.id}/academic`, body), async () => {
      await reload();
      if (complete && outcome === "recommend_offer") nav(`/staff/applications/${app.id}/offer`);
      if (complete && outcome !== "recommend_offer") nav(`/staff/applications/${app.id}`);
    });
    setConfirming(null);
  }

  return (
    <>
      <ActionErrors summary={summary} />
      {done && (
        <Alert kind="success" title="Assessment completed">
          <p>Outcome: {ACADEMIC_OUTCOMES.find((o) => o.value === academic.outcome)?.label ?? academic.outcome}.</p>
          <SignOff by={academic.completed_by} at={academic.completed_at} />
        </Alert>
      )}
      <section className="card">
        <h2 className="h3">Academic assessment</h2>
        <Checklist items={academic.checklist} value={checks} onChange={setChecks} readOnly={done} />
        {academic.outstanding_mandatory.length > 0 && !done && (
          <p className="hint">Outstanding: {academic.outstanding_mandatory.join("; ")}.</p>
        )}
        <TextArea
          name="comments" label="Assessment notes" rows={6} maxLength={4000} value={comments}
          hint={needsReason
            ? "These notes are the reason given to the applicant, so write them for them to read."
            : "Recorded against the application and visible in the audit trail."}
          error={reasonTooShort ? "Give a reason of at least 10 characters." : undefined}
          onChange={setComments} required={needsReason}
        />
        <EnglishEvidence
          app={app} done={done}
          evidence={englishEvidence} onEvidence={setEnglishEvidence}
          standard={englishStandard} onStandard={setEnglishStandard}
          required={outcome === "recommend_offer" && checks.english_evidence_verified === true}
        />
        <fieldset className="subsection" disabled={done}>
          <legend className="h4">Credibility interview</legend>
          <p className="hint">
            Record the interview if one was held. Ticking "Credibility interview completed" requires the
            date, the interviewer and the outcome; ticking "Interview notes uploaded" requires the notes
            on the application's documents
            {academic.interview_notes_documents ? ` (${academic.interview_notes_documents} on file)` : " (none on file yet — upload them from the application summary)"}.
          </p>
          <DateField name="interview_date" label="Interview date" value={interview.interview_date}
            required={checks.interview_completed === true}
            onChange={(v) => setInterview({ ...interview, interview_date: v })} />
          <TextField name="interviewer" label="Interviewer" width="m" value={interview.interviewer}
            required={checks.interview_completed === true}
            onChange={(v) => setInterview({ ...interview, interviewer: v })} />
          <RadioGroup name="interview_outcome" label="Interview outcome" inline value={interview.interview_outcome}
            required={checks.interview_completed === true}
            onChange={(v) => setInterview({ ...interview, interview_outcome: v })}
            options={[{ value: "satisfactory", label: "Satisfactory" }, { value: "concerns", label: "Concerns noted" },
              { value: "unsatisfactory", label: "Unsatisfactory" }]} />
          <TextArea name="interview_notes" label="Interview summary" rows={3} maxLength={4000} value={interview.interview_notes}
            onChange={(v) => setInterview({ ...interview, interview_notes: v })} />
        </fieldset>
        <RadioGroup
          name="outcome" label="Outcome" required value={outcome} onChange={setOutcome}
          options={ACADEMIC_OUTCOMES} error={undefined}
        />
        {!done && (
          <div className="actions">
            <button type="button" className="button button-secondary" onClick={() => void save(false)} disabled={busy}>
              Save without completing
            </button>
            <button
              type="button" className="button" disabled={busy || !outcome || reasonTooShort}
              onClick={() => setConfirming(outcome === "reject" ? "reject" : "complete")}
            >
              Complete assessment
            </button>
          </div>
        )}
      </section>
      {confirming === "reject" && (
        <Confirm
          title={`Reject application ${app.ref}?`}
          consequence={`${app.applicant_name ?? "The applicant"} will be told the application was unsuccessful. A rejected application cannot be reopened.`}
          confirmLabel="Reject application" busy={busy}
          onConfirm={() => void save(true)} onCancel={() => setConfirming(null)}
        />
      )}
      {confirming === "complete" && (
        <Confirm
          title={`Complete the academic assessment on ${app.ref}?`}
          consequence={outcome === "recommend_offer"
            ? "Your name and the time are recorded as the academic sign-off, the assessment can no longer be edited, and you will not be able to assign the CAS for this applicant yourself."
            : "The applicant will be asked for more information using the notes above, and the assessment can no longer be edited."}
          confirmLabel="Complete assessment" busy={busy}
          onConfirm={() => void save(true)} onCancel={() => setConfirming(null)}
        />
      )}
    </>
  );
}

const ENGLISH_EVIDENCE_OPTIONS = [
  { value: "slt_test", label: "Secure English Language Test (SELT)" },
  { value: "degree_taught_in_english", label: "Degree taught in English" },
  { value: "majority_english_country", label: "National of a majority English-speaking country" },
  { value: "provider_assessment", label: "ESE's own assessment" },
  { value: "not_required", label: "No evidence required for this course" },
];

/**
 * FR-36 — the English language check, with the applicant's own answers beside it.
 *
 * The checklist has always carried a tick for this. A tick records that somebody looked; it does
 * not record what they accepted, and a sponsor asked at a compliance visit which evidence it took
 * for a given student has to answer from the record. The applicant's test detail is shown here
 * because the assessor was previously expected to verify a test they could not see.
 */
function EnglishEvidence({ app, done, evidence, onEvidence, standard, onStandard, required }: {
  app: ApplicationRecord;
  done: boolean;
  evidence: string;
  onEvidence: (v: string) => void;
  standard: string;
  onStandard: (v: string) => void;
  required: boolean;
}) {
  const d = app.data as Record<string, unknown>;
  const scores = (d.test_scores ?? null) as Record<string, number> | null;
  const stated = [
    ["Route the applicant declared", String(d.english_route ?? "—").replace(/_/g, " ")],
    ["Test provider", (d.test_provider as string) ?? "—"],
    ["Test reference", (d.test_reference as string) ?? "—"],
    ["Test date", d.test_date ? formatDate(d.test_date as string) : "—"],
  ] as [string, string][];

  return (
    <section className="card nested">
      <h3 className="h4">English language evidence</h3>
      <dl className="kv compact">
        {stated.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        {scores && (
          <div>
            <dt>Reported scores</dt>
            <dd>{Object.entries(scores).map(([band, value]) => `${band} ${value}`).join(" · ")}</dd>
          </div>
        )}
      </dl>
      <p className="hint">
        As the applicant entered them. Check these against the certificate on file before recording
        what you accepted.
      </p>
      {done ? (
        <dl className="kv compact">
          <div>
            <dt>Evidence accepted</dt>
            <dd>{ENGLISH_EVIDENCE_OPTIONS.find((o) => o.value === evidence)?.label ?? "Not recorded"}</dd>
          </div>
          <div><dt>Standard</dt><dd>{standard || "Not recorded"}</dd></div>
        </dl>
      ) : (
        <>
          <SelectField
            name="english_evidence" label="Evidence accepted" required={required}
            value={evidence} onChange={onEvidence} options={ENGLISH_EVIDENCE_OPTIONS}
            placeholder="Select the evidence you accepted…"
            hint="Recorded against the assessment and repeated in the evidence pack."
            error={required && !evidence ? "Record which evidence you accepted." : undefined}
          />
          <TextField
            name="english_standard" label="Standard it was checked against"
            value={standard} onChange={onStandard} width="l"
            hint="For example: CEFR B2, or IELTS UKVI 6.0 with no band below 5.5."
          />
        </>
      )}
    </section>
  );
}

export function AcademicAssessment() {
  return <RecordPage title="Academic assessment" render={(app, reload) =>
    app.academic ? <AcademicForm app={app} reload={reload} /> : <Alert kind="error">You cannot see this assessment.</Alert>} />;
}

// ---------------------------------------------------------------------------------------------
// Compliance review (FR-44 to FR-52)
// ---------------------------------------------------------------------------------------------
function ComplianceForm({ app, reload }: { app: ApplicationRecord; reload: () => Promise<void> }) {
  const review = app.compliance as Assessment;
  const { user } = useAuth();
  const nav = useNavigate();
  const done = Boolean(review.completed_at);
  const [checks, setChecks] = useState(() => checklistValue(review.checklist));
  const [comments, setComments] = useState(review.comments ?? "");
  const [risk, setRisk] = useState(review.risk_rating ?? "");
  const [atas, setAtas] = useState(review.atas_required === null ? "" : String(review.atas_required));
  const [atasRef, setAtasRef] = useState(review.atas_reference ?? "");
  const [confirming, setConfirming] = useState(false);
  const { summary, busy, run } = useAction();
  const second = useAction();

  const secondLineDue = (risk === "medium" || risk === "high") && done && !review.second_line_at;
  const isFirstReviewer = review.completed_by_id !== null && review.completed_by_id === user?.id;

  async function save(complete: boolean) {
    const body = {
      checklist: checks, risk_rating: risk || null, comments, complete,
      atas_required: atas === "" ? null : atas === "true",
      atas_reference: atasRef || null,
    };
    await run(() => api.post<Assessment>(`/applications/${app.id}/compliance`, body), async () => {
      await reload();
      if (complete) nav(`/staff/applications/${app.id}/precas`);
    });
  }

  async function recordSecondLine(outcome: "supported" | "not_supported") {
    await second.run(
      () => api.post<Assessment>(`/applications/${app.id}/compliance/second-line`, { outcome }),
      () => reload(),
    );
  }

  return (
    <>
      <ActionErrors summary={summary} />
      {done && (
        <Alert kind="success" title="Compliance review completed">
          <p>Risk rating: {RISK_LABELS[review.risk_rating ?? ""] ?? "—"}.</p>
          <SignOff by={review.completed_by} at={review.completed_at} />
        </Alert>
      )}
      <section className="card">
        <h2 className="h3">Pre-CAS compliance checks</h2>
        <Checklist items={review.checklist} value={checks} onChange={setChecks} readOnly={done} />
        {review.outstanding_mandatory.length > 0 && !done && (
          <p className="hint">Outstanding: {review.outstanding_mandatory.join("; ")}.</p>
        )}
        <RadioGroup
          name="risk_rating" label="Credibility and compliance risk rating" required value={risk} onChange={setRisk}
          hint="Medium and High ratings need a second-line review by a different reviewer."
          options={[
            { value: "low", label: "Low" },
            { value: "medium", label: "Medium" },
            { value: "high", label: "High" },
          ]}
        />
        <RadioGroup
          name="atas_required" label="ATAS clearance required" inline value={atas} onChange={setAtas}
          options={[{ value: "true", label: "Yes" }, { value: "false", label: "No" }]}
        />
        {atas === "true" && (
          <TextField name="atas_reference" label="ATAS certificate reference" value={atasRef} onChange={setAtasRef} width="m" />
        )}
        <TextArea name="comments" label="Review notes" rows={6} maxLength={4000} value={comments} onChange={setComments} />
        {!done && (
          <div className="actions">
            <button type="button" className="button button-secondary" onClick={() => void save(false)} disabled={busy}>
              Save without completing
            </button>
            <button type="button" className="button" onClick={() => setConfirming(true)} disabled={busy || !risk}>
              Complete review
            </button>
          </div>
        )}
      </section>

      {confirming && (
        <Confirm
          title={`Complete the compliance review on ${app.ref}?`}
          consequence={"Your name and the time are recorded as the compliance sign-off, the review can no longer be "
            + "edited, and you will not be able to assign the CAS for this applicant yourself."}
          confirmLabel="Complete review" busy={busy}
          onConfirm={() => { setConfirming(false); void save(true); }} onCancel={() => setConfirming(false)}
        />
      )}

      {(secondLineDue || review.second_line_at) && (
        <section className="card">
          <h2 className="h3">Second-line review</h2>
          <ActionErrors summary={second.summary} />
          {review.second_line_at ? (
            <>
              <p>Outcome: {review.second_line_outcome === "supported" ? "Supported" : "Not supported"}.</p>
              <SignOff by={review.second_line_by} at={review.second_line_at} prefix="Recorded" />
            </>
          ) : isFirstReviewer ? (
            <Alert kind="info">
              This review must be recorded by a compliance colleague other than {review.completed_by}, who completed
              the first-line review.
            </Alert>
          ) : (
            <>
              <p>A {RISK_LABELS[risk] ?? risk} risk rating needs a second reviewer to confirm the decision (FR-52).</p>
              <div className="actions">
                <button type="button" className="button" disabled={second.busy} onClick={() => void recordSecondLine("supported")}>
                  Support the review
                </button>
                <button type="button" className="button button-secondary" disabled={second.busy} onClick={() => void recordSecondLine("not_supported")}>
                  Do not support
                </button>
              </div>
            </>
          )}
        </section>
      )}
    </>
  );
}

export function ComplianceReview() {
  return <RecordPage title="Compliance review" render={(app, reload) =>
    app.compliance ? <ComplianceForm app={app} reload={reload} /> : <Alert kind="error">You cannot see this review.</Alert>} />;
}
