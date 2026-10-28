import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/**
 * The applicant's wizard frame — prototype screens 2 and 3.
 *
 * Both of those screens are the same frame: the tinted section rail on the left, the form on the
 * right. Supporting documents is one of the sections, not a page the applicant is sent away to,
 * so the rail is defined here once and used by the form and the upload screen alike.
 */
export const STEPS = [
  "Personal details", "Identity and nationality", "Address and contact", "Programme and intake",
  "Academic background", "English language", "UK immigration history", "Supporting information", "Declaration",
];

export const DOCUMENTS_STEP = "documents" as const;

export function WizardFrame({ reference, current, onStep, furthest = 0, status, children }: {
  reference: string;
  /** 1–9 for a form section, or "documents" for the upload screen. */
  current: number | typeof DOCUMENTS_STEP;
  /** Supplied by the form, which moves between sections in place; the upload screen navigates. */
  onStep?: (step: number) => void;
  /** Sections up to and including this number are marked done. */
  furthest?: number;
  status?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="win">
      <div className="wizard">
        <aside className="wizard-nav">
          <p className="ref">Application <strong>{reference}</strong></p>
          <nav aria-label="Application sections">
            <ol className="stepper">
              {STEPS.map((title, i) => {
                const n = i + 1;
                const here = current === n;
                const done = !here && n <= furthest;
                const body = <><span className="step-num">{done ? "✓" : n}</span>{title}</>;
                return (
                  <li key={title}>
                    {onStep ? (
                      <a
                        href={`?step=${n}`}
                        className={done ? "done" : undefined}
                        aria-current={here ? "step" : undefined}
                        onClick={(ev) => { ev.preventDefault(); onStep(n); }}
                      >
                        {body}
                      </a>
                    ) : (
                      <Link to={`/apply?step=${n}`} className={done ? "done" : undefined} aria-current={here ? "step" : undefined}>
                        {body}
                      </Link>
                    )}
                  </li>
                );
              })}
              <li>
                <Link
                  to="/apply/documents"
                  aria-current={current === DOCUMENTS_STEP ? "step" : undefined}
                  className="step-documents"
                >
                  <span className="step-num" aria-hidden="true">▤</span>Supporting documents
                </Link>
              </li>
            </ol>
          </nav>
          {status}
        </aside>
        <section className="wizard-body">{children}</section>
      </div>
    </div>
  );
}
