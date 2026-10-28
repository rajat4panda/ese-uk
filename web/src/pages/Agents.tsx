import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AGENT_EDITORS, api, formatDate, type AgentRow } from "../api";
import { useAuth } from "../auth";
import { Alert, Layout, Spinner } from "../components/ui";
import { LoadFailed } from "../components/workflow";

/**
 * Recruitment agent register — prototype screen 11.
 *
 * Compliance, the Authorising Officer and administrators add and maintain agents from here (BRD
 * FR-90 to FR-94); admissions reads the register. The columns are the ones the prototype shows, with one addition the
 * sponsor duty actually turns on: the visa refusal rate per agent, and whether it has passed the
 * threshold recorded against that agent (FR-106, FR-107). Rates below five visa decisions are not
 * shown at all rather than shown as a large percentage of a tiny sample.
 */
export function Agents() {
  const [rows, setRows] = useState<AgentRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const { user } = useAuth();
  const canEdit = !!user && AGENT_EDITORS.includes(user.role);

  useEffect(() => {
    api.get<AgentRow[]>("/agents").then(setRows).catch(() => setFailed(true));
  }, []);

  if (failed) return <Layout><LoadFailed what="The agent register" /></Layout>;
  if (!rows) return <Layout><Spinner /></Layout>;

  const flagged = rows.filter((a) => a.refusal_rate_over_threshold);

  return (
    <Layout>
      <div className="page-head">
        <h1>Recruitment agents</h1>
        {canEdit && <Link className="button" to="/staff/agents/new">Add an agent</Link>}
      </div>

      {flagged.length > 0 && (
        <Alert kind="error" title={`${flagged.length} agent${flagged.length === 1 ? "" : "s"} above the refusal-rate threshold`}>
          <p>
            {flagged.map((a) => `${a.name} (${a.refusal_rate_pct}% of ${a.visa_decisions})`).join(", ")}.
            Review the agreement and due-diligence record before accepting further applications.
          </p>
        </Alert>
      )}

      {rows.length === 0 ? (
        <p className="empty">No agents are on the register.</p>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Recruitment agents and their conversion</caption>
              <thead>
                <tr>
                  <th scope="col">Agent</th>
                  <th scope="col">Country</th>
                  <th scope="col">Applications</th>
                  <th scope="col">Offers</th>
                  <th scope="col">CAS</th>
                  <th scope="col">Enrolled</th>
                  <th scope="col">Refusal rate</th>
                  <th scope="col">Due diligence</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} className={a.refusal_rate_over_threshold ? "row-overdue" : undefined}>
                    <td>
                      <Link to={`/staff/agents/${a.id}`}><b>{a.name}</b></Link>
                      {a.contact_name && <span className="hint block">{a.contact_name}</span>}
                    </td>
                    <td>{a.country}</td>
                    <td>{a.applications}</td>
                    <td>{a.offers}</td>
                    <td>{a.cas}</td>
                    <td>{a.enrolled}</td>
                    <td>
                      {a.refusal_rate_pct === null ? (
                        <span className="hint">
                          {a.visa_decisions === 0 ? "No decisions yet" : `${a.refused} of ${a.visa_decisions}`}
                        </span>
                      ) : (
                        <>
                          <span className={`tag ${a.refusal_rate_over_threshold ? "tag-overdue" : "tag-clean"}`}>
                            {a.refusal_rate_pct}%
                          </span>
                          <span className="hint block">
                            {a.refused} of {a.visa_decisions}
                            {a.refusal_rate_threshold_pct !== null && ` · threshold ${a.refusal_rate_threshold_pct}%`}
                          </span>
                        </>
                      )}
                    </td>
                    <td className="nowrap">
                      {a.due_diligence_date ? formatDate(a.due_diligence_date) : <span className="hint">Not recorded</span>}
                      {a.next_review_date && <span className="hint block">Review {formatDate(a.next_review_date)}</span>}
                    </td>
                    <td>
                      <span className={`tag tag-${a.status}`}>{a.status.replace(/_/g, " ")}</span>
                      {a.status_reason && <span className="hint block">{a.status_reason}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">
            Open an agent for its contact details, agreement and due-diligence record.
            Suspended and deactivated agents stay on the register with their history, and cannot be selected on new applications.
          </p>
        </>
      )}
    </Layout>
  );
}
