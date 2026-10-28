import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, formatDate, type AgentRow, type AllocationPosition, type ComplianceOverview, type ReportableEvent,
} from "../api";
import { useAuth } from "../auth";
import { Layout, Spinner } from "../components/ui";
import { LoadFailed } from "../components/workflow";
import { initials } from "../nav";

/**
 * Workspace dashboard — prototype screen 15 (Authorising Officer).
 *
 * Deliberately built from records rather than from a stored summary: every number here is a live
 * count of the same rows the linked screen shows, so the dashboard and the queue can never disagree.
 * It draws only on endpoints every caseworking role may call, which is why the Basic Compliance
 * Assessment rates are not on it — those are compliance-and-above and live on Reports (LLD 6).
 */

interface Health {
  total: number;
  compliant: number;
  attention: number;
  critical: number;
}

/** The same classification the Compliance screen shows (services/monitoring.py), never recomputed here. */
function health(o: ComplianceOverview): Health {
  return { total: o.total, compliant: o.compliant, attention: o.attention, critical: o.critical };
}

/** Compliance ring — the same three-part donut as the prototype, drawn from the counts above. */
function Donut({ h }: { h: Health }) {
  const pct = h.total === 0 ? 100 : Math.round((h.compliant / h.total) * 100);
  const seg = (n: number) => (h.total === 0 ? 0 : (n / h.total) * 100);
  const green = seg(h.compliant);
  const amber = seg(h.attention);
  const red = seg(h.critical);
  return (
    <div className="donut">
      <svg viewBox="0 0 42 42" width="110" height="110" role="img"
        aria-label={`${pct}% compliant: ${h.compliant} compliant, ${h.attention} require attention, ${h.critical} critical`}>
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="#e6ecf5" strokeWidth="5" />
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="#166534" strokeWidth="5"
          strokeDasharray={`${green} ${100 - green}`} strokeDashoffset="25" />
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="#b45309" strokeWidth="5"
          strokeDasharray={`${amber} ${100 - amber}`} strokeDashoffset={String(25 - green)} />
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="#b91c1c" strokeWidth="5"
          strokeDasharray={`${red} ${100 - red}`} strokeDashoffset={String(25 - green - amber)} />
      </svg>
      <div className="donut-centre" aria-hidden="true">
        <b>{pct}%</b>
        <span>Compliant</span>
      </div>
    </div>
  );
}

export function Dashboard() {
  const { user } = useAuth();
  const [overview, setOverview] = useState<ComplianceOverview | null>(null);
  const [allocation, setAllocation] = useState<AllocationPosition | null>(null);
  const [events, setEvents] = useState<ReportableEvent[] | null>(null);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    Promise.all([
      api.get<ComplianceOverview>("/compliance/overview"),
      api.get<{ allocation: AllocationPosition }>("/cas?page_size=1"),
      api.get<ReportableEvent[]>("/events?status=pending"),
    ])
      .then(([o, c, e]) => { setOverview(o); setAllocation(c.allocation); setEvents(e); })
      .catch(() => setFailed(true));
    // Agent performance is a nice-to-have on this screen: a failure here must not blank the page.
    api.get<AgentRow[]>("/agents").then(setAgents).catch(() => setAgents([]));
  }, []);

  if (failed) return <Layout><LoadFailed what="The dashboard" /></Layout>;
  if (!overview || !allocation || !events || !user) return <Layout><Spinner /></Layout>;

  const h = health(overview);
  const expiring = overview.issue_counts.visa_expiring ?? 0;
  const expired = overview.issue_counts.visa_expired ?? 0;
  const engagement = overview.issue_counts.attendance ?? 0;
  const failedChecks = overview.issue_counts.status_check_failed ?? 0;
  const missingChecks = overview.issue_counts.status_check_missing ?? 0;
  const noEvidence = overview.issue_counts.enrolment_evidence_missing ?? 0;
  const noAddress = overview.issue_counts.address_update ?? 0;
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  const overdue = events.filter((e) => e.overdue);
  const dueSoon = events.filter((e) => !e.overdue && e.days_remaining <= 3);
  const nonEnrolment = events.filter((e) => e.event_type === "non_enrolment");
  const overThreshold = agents.filter((a) => a.refusal_rate_over_threshold);

  const alerts: { tone: "amber" | "red"; text: string; to: string }[] = [];
  if (overdue.length) alerts.push({ tone: "red", text: `${overdue.length} report${overdue.length === 1 ? "" : "s"} past the UKVI deadline`, to: "/staff/events" });
  if (h.attention) alerts.push({ tone: "amber", text: `${h.attention} student${h.attention === 1 ? "" : "s"} require attention`, to: "/staff/compliance" });
  if (expired) alerts.push({ tone: "red", text: `${expired} student${expired === 1 ? "'s" : "s'"} permission to stay has expired`, to: "/staff/compliance" });
  if (expiring) alerts.push({ tone: "amber", text: `${expiring} visa expir${expiring === 1 ? "y" : "ies"} approaching`, to: "/staff/compliance" });
  if (engagement) alerts.push({ tone: "amber", text: `${engagement} student${engagement === 1 ? "" : "s"} below the attendance threshold`, to: "/staff/compliance" });
  if (failedChecks) alerts.push({ tone: "red", text: `${plural(failedChecks, "right-to-study check", "right-to-study checks")} failed`, to: "/staff/compliance" });
  if (missingChecks) alerts.push({ tone: "amber", text: `${plural(missingChecks, "student has", "students have")} no right-to-study check on record`, to: "/staff/compliance" });
  if (noEvidence) alerts.push({ tone: "amber", text: `${plural(noEvidence, "enrolment", "enrolments")} without evidence on file`, to: "/staff/compliance" });
  if (noAddress) alerts.push({ tone: "amber", text: `${plural(noAddress, "address update", "address updates")} required`, to: "/staff/compliance" });
  if (nonEnrolment.length) alerts.push({ tone: "red", text: `${nonEnrolment.length} non-enrolment to report`, to: "/staff/events" });
  if (dueSoon.length) alerts.push({ tone: "amber", text: `${dueSoon.length} report${dueSoon.length === 1 ? "" : "s"} due within three working days`, to: "/staff/events" });
  for (const a of overThreshold) {
    alerts.push({ tone: "red", text: `Agent ${a.name} — refusal rate ${a.refusal_rate_pct}% above threshold`, to: "/staff/agents" });
  }

  const today = formatDate(new Date().toISOString().slice(0, 10));

  return (
    <Layout>
      <div className="page-head">
        <div>
          <h1>Welcome, {user.name.split(" ")[0]}</h1>
          <span className="sub">Student sponsorship compliance dashboard · {today}</span>
        </div>
        <div className="whoami">
          <span>
            <b>{user.name}</b>
            <span className="role">{user.role === "senior" ? "Authorising Officer" : user.role.replace(/^\w/, (c) => c.toUpperCase())}</span>
          </span>
          <span className="avatar sm" aria-hidden="true">{initials(user.name)}</span>
        </div>
      </div>

      <div className="stats">
        <div className="stat s-blue"><span className="n">{allocation.allocated}</span><span className="t">CAS allocated</span></div>
        <div className="stat s-green"><span className="n">{allocation.assigned}</span><span className="t">CAS assigned</span></div>
        <div className="stat s-amber"><span className="n">{allocation.remaining}</span><span className="t">CAS remaining</span></div>
        <div className="stat s-navy">
          <Link to="/staff/students"><span className="n">{h.total}</span><span className="t">International students</span></Link>
        </div>
      </div>
      <p className="hint">
        {allocation.period_label
          ? `Allocation period ${allocation.period_label}.`
          : "No allocation period is recorded — the remaining figure cannot be relied on until one is."}
      </p>

      <div className="row" style={{ gap: "32px", alignItems: "flex-start" }}>
        <section>
          <h2 className="h4">Compliance status</h2>
          <div className="row" style={{ gap: "16px", alignItems: "center" }}>
            <Donut h={h} />
            <p className="legend">
              <span className="dot" style={{ background: "#166534" }} />{h.compliant} compliant<br />
              <span className="dot" style={{ background: "#b45309" }} />{h.attention} require attention<br />
              <span className="dot" style={{ background: "#b91c1c" }} />{h.critical} critical
            </p>
          </div>
        </section>

        <section style={{ flex: 1, minWidth: "260px" }}>
          <h2 className="h4">Key alerts</h2>
          {alerts.length === 0 ? (
            <p className="empty">Nothing needs attention. Every reportable event is inside its deadline.</p>
          ) : (
            <ul className="alert-list">
              {alerts.map((a, i) => (
                <li key={i}>
                  <span className={`alert-flag flag-${a.tone}`} aria-hidden="true">▲</span>
                  <Link to={a.to}>{a.text}</Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {overdue.length > 0 && (
        <section className="card">
          <h2 className="h3">Past the reporting deadline</h2>
          <p className="hint">A late report is still required. Each breach is recorded in the audit trail.</p>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Reportable events past their deadline</caption>
              <thead>
                <tr><th scope="col">Student</th><th scope="col">Event</th><th scope="col">Deadline</th></tr>
              </thead>
              <tbody>
                {overdue.slice(0, 5).map((e) => (
                  <tr key={e.id} className="row-overdue">
                    <td><Link to={`/staff/students/${e.student_id}`}>{e.student_name ?? "—"}</Link></td>
                    <td>{e.event_type.replace(/_/g, " ")}</td>
                    <td className="nowrap">{formatDate(e.deadline_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p><Link to="/staff/events">Open the reporting queue</Link></p>
        </section>
      )}

      <p className="quote">
        “Quality education. Responsible sponsorship. A global future.”<br />
        <span>— European School of Economics</span>
      </p>
    </Layout>
  );
}
