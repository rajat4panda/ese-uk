import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, formatDate, type BcaMetrics, type ComplianceOverview, type ReportableEvent,
} from "../api";
import { Alert, Layout, Spinner, StatusBadge } from "../components/ui";
import { LoadFailed } from "../components/workflow";
import { DeadlineChip } from "./Student";

const RAG_LABEL: Record<string, string> = { green: "Green", amber: "Amber", red: "Red" };

const RAG_MEANING: Record<string, string> = {
  green: "Within the threshold.",
  amber: "Within the warning band of the threshold.",
  red: "Threshold breached.",
};

const STATUS_LABEL: Record<string, string> = {
  awaiting_arrival: "Awaiting arrival", enrolled: "Enrolled", deferred: "Deferred",
  interrupted: "Interrupted", withdrawn: "Withdrawn", completed: "Completed", non_enrolled: "Not enrolled",
};

/**
 * Compliance monitoring — prototype screen 13 (BRD FR-70, FR-71).
 *
 * The population at a glance, then the only list that matters day to day: the students who are not
 * clear, each with the reason. Classification is done on the server (services/monitoring.py) so this
 * screen and the Authorising Officer's dashboard can never disagree: an overdue report or an expired
 * permission is critical; an open report, attendance below the threshold or a visa expiring soon
 * needs attention.
 */
export function ComplianceMonitoring() {
  const [data, setData] = useState<ComplianceOverview | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.get<ComplianceOverview>("/compliance/overview").then(setData).catch(() => setFailed(true));
  }, []);

  if (failed) return <Layout><LoadFailed what="Compliance monitoring" /></Layout>;
  if (!data) return <Layout><Spinner /></Layout>;

  return (
    <Layout>
      <div className="page-head">
        <h1>Compliance overview</h1>
      </div>

      <div className="stats" style={{ maxWidth: "660px" }}>
        <div className="stat"><span className="n">{data.total}</span><span className="t">Total students</span></div>
        <div className="stat s-green"><span className="n">{data.compliant}</span><span className="t">Compliant</span></div>
        <div className="stat s-amber"><span className="n">{data.attention}</span><span className="t">Require attention</span></div>
        <div className="stat s-red"><span className="n">{data.critical}</span><span className="t">Critical</span></div>
      </div>

      <div className="page-head">
        <h2 className="h4" style={{ margin: 0 }}>Students requiring attention</h2>
        <Link to="/staff/students">View all students ›</Link>
      </div>

      {data.students.length === 0 ? (
        <p className="empty">Every sponsored student is clear: nothing to report, attendance above the threshold and no visa expiring soon.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Students with a compliance issue, most serious first</caption>
            <thead>
              <tr>
                <th scope="col">Student</th>
                <th scope="col">Issue</th>
                <th scope="col">Date</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.students.map((s) => (
                <tr key={s.student_id} className={s.severity === "critical" ? "row-overdue" : undefined}>
                  <td>
                    <Link to={`/staff/students/${s.student_id}`}>{s.student_name ?? "—"}</Link>
                    <span className="hint block">{s.application_ref}</span>
                  </td>
                  <td>
                    {s.issues.map((i, n) => (
                      <span key={n} className="block">
                        <span className={`tag tag-sentence ${i.severity === "critical" ? "tag-overdue" : "tag-pending"}`}>{i.label}</span>
                        <span className="hint"> {i.detail}</span>
                      </span>
                    ))}
                  </td>
                  <td className="nowrap">{s.issues[0].since ? formatDate(s.issues[0].since) : "—"}</td>
                  <td><StatusBadge status={s.enrolment_status} label={STATUS_LABEL[s.enrolment_status] ?? s.enrolment_status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="hint">
        Escalations follow the attendance and engagement policy: contact, then warning, then
        withdrawal, then a report to UKVI. Thresholds are set under Settings.
      </p>
    </Layout>
  );
}

/**
 * Reports — the three Basic Compliance Assessment rates and what is waiting to be reported.
 * Deliberately two things and not a wall of charts: these are the numbers a sponsor is judged on,
 * and the queue is the work that keeps them right.
 */
export function Reports() {
  const [metrics, setMetrics] = useState<BcaMetrics | null>(null);
  const [events, setEvents] = useState<ReportableEvent[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    Promise.all([
      api.get<BcaMetrics>("/metrics/bca"),
      api.get<ReportableEvent[]>("/events?status=pending"),
    ])
      .then(([m, e]) => { setMetrics(m); setEvents(e); })
      .catch(() => setFailed(true));
  }, []);

  if (failed) return <Layout><LoadFailed what="The compliance report" /></Layout>;
  if (!metrics || !events) return <Layout><Spinner /></Layout>;

  const overdue = events.filter((e) => e.overdue);
  const dueSoon = events.filter((e) => !e.overdue && e.days_remaining <= 3);

  return (
    <Layout>
      <div className="page-head">
        <h1>Basic Compliance Assessment</h1>
      </div>
      <p className="hint">
        Rates for {formatDate(metrics.period_start)} to {formatDate(metrics.period_end)}, measured
        against the CAS population — the same basis UKVI uses.
      </p>

      <div className={`rag-banner rag-${metrics.overall_rag}`}>
        <span className="caption">Overall rating</span>
        <p className="rag-value">{RAG_LABEL[metrics.overall_rag] ?? metrics.overall_rag}</p>
        <p>The worst of the three rates, never an average — one red metric puts a licence at risk.</p>
      </div>

      <div className="tiles">
        {metrics.metrics.map((m) => (
          <section key={m.key} className={`tile tile-rag-${m.rag}`}>
            <h2 className="h4">
              {m.label} <span className={`tag tag-rag-${m.rag}`}>{RAG_LABEL[m.rag] ?? m.rag}</span>
            </h2>
            <p className="metric-value">{m.percentage === null ? "—" : `${m.percentage}%`}</p>
            <p className="hint">
              {m.numerator} of {m.denominator} ·{" "}
              {m.direction === "max" ? `threshold ${m.threshold}% or below` : `threshold ${m.threshold}% or above`}
            </p>
            <p className="hint">{m.note || RAG_MEANING[m.rag]}</p>
          </section>
        ))}
      </div>

      <section className="card">
        <h2 className="h3">Reporting</h2>
        {events.length === 0 ? (
          <p className="empty">Nothing waiting to be reported.</p>
        ) : (
          <>
            {overdue.length > 0 && (
              <Alert kind="error" title={`${overdue.length} event(s) past the reporting deadline`}>
                <p>A late report is still required. Each breach is recorded in the audit trail.</p>
              </Alert>
            )}
            {overdue.length === 0 && dueSoon.length > 0 && (
              <Alert kind="warning" title={`${dueSoon.length} event(s) due within three working days`} />
            )}
            <div className="table-wrap">
              <table className="table">
                <caption className="visually-hidden">Open reportable events</caption>
                <thead>
                  <tr><th scope="col">Student</th><th scope="col">Event</th><th scope="col">Report by</th><th scope="col">Clock</th></tr>
                </thead>
                <tbody>
                  {events.slice(0, 10).map((e) => (
                    <tr key={e.id} className={e.overdue ? "row-overdue" : undefined}>
                      <td>
                        <Link to={`/staff/students/${e.student_id}`}>{e.student_name ?? "—"}</Link>
                        <span className="hint block">{e.application_ref}</span>
                      </td>
                      <td>{e.event_type.replace(/_/g, " ")}</td>
                      <td className="nowrap">{formatDate(e.deadline_date)}</td>
                      <td><DeadlineChip event={e} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p><Link to="/staff/events">Open the reporting queue</Link></p>
          </>
        )}
      </section>
    </Layout>
  );
}
