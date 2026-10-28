/**
 * Role-scoped workspace navigation.
 *
 * The prototype does not have one sidebar — it has seven, one per workspace, and a role only ever
 * sees the items its own work needs (prototype screens 5, 9, 11, 12, 13, 14, 15). This file is the
 * single definition of that, so a sidebar item and the route guard behind it cannot drift apart.
 *
 * Rule followed throughout: a nav item is shown only where the caller's role can actually open the
 * page AND the page's data endpoint will answer. A *record-level* block (segregation of duties on a
 * particular application, FR-55) is never expressed by hiding a link — LLD 14.2 requires the action
 * to be visible and disabled with its reason, which is what the screens do.
 *
 * Source of truth for the role column: LLD 6 Authorisation Matrix, reconciled against the role
 * guards actually shipped in app/api/*.py.
 */
import type { Role } from "./api";

export interface NavItem {
  label: string;
  to: string;
  /** Matches when the current path starts with this, for the "you are here" marker. */
  match?: string;
  roles: Role[];
  /** Requirement(s) the screen serves — kept beside the route so the traceability stays honest. */
  req: string;
}

const ADMISSIONS_UP: Role[] = ["admissions", "senior", "admin"];
const COMPLIANCE_UP: Role[] = ["compliance", "senior", "admin"];
const CASEWORK: Role[] = ["admissions", "compliance", "senior", "admin"];
const ALL_STAFF: Role[] = ["admissions", "compliance", "finance", "senior", "admin"];

export const NAV: NavItem[] = [
  { label: "Dashboard", to: "/staff/dashboard", roles: CASEWORK, req: "FR-109–FR-113" },
  { label: "Applications", to: "/staff", match: "/staff/applications", roles: ALL_STAFF, req: "FR-25, FR-26" },
  { label: "Assessments", to: "/staff?stage=assessment", match: "\u0000", roles: ADMISSIONS_UP, req: "FR-27–FR-30" },
  { label: "Offers", to: "/staff?stage=offers", match: "\u0000", roles: ADMISSIONS_UP, req: "FR-35–FR-39" },
  { label: "CAS management", to: "/staff/cas", roles: ALL_STAFF, req: "FR-59, FR-60" },
  { label: "Students", to: "/staff/students", roles: CASEWORK, req: "FR-71–FR-81" },
  { label: "Compliance", to: "/staff/compliance", roles: COMPLIANCE_UP, req: "FR-91, FR-92" },
  { label: "UKVI reporting", to: "/staff/events", roles: CASEWORK, req: "FR-93–FR-101" },
  { label: "Agents", to: "/staff/agents", roles: CASEWORK, req: "FR-102–FR-107" },
  { label: "Reports", to: "/staff/reports", roles: COMPLIANCE_UP, req: "FR-87–FR-92, FR-127" },
  { label: "Administration", to: "/admin/users", match: "/admin", roles: ["admin"], req: "FR-121, FR-132" },
  // Prototype screen 15. Finance sees the fees section only (LLD 6: fees are its side of the file).
  { label: "Settings", to: "/settings", roles: ["finance", "senior", "admin"], req: "BRD FR-105" },
];

/**
 * The sidebar for a role. Finance is deliberately short: it may read the pipeline and the CAS
 * register and record payments from within an application, and nothing else (LLD 6).
 */
export function navFor(role: Role): NavItem[] {
  if (role === "applicant") return [];
  return NAV.filter((item) => item.roles.includes(role));
}

/** True when `path` is the page this nav item leads to. */
export function isCurrent(item: NavItem, path: string, search: string): boolean {
  if (item.to.includes("?")) {
    const [base, query] = item.to.split("?");
    return path === base && search.replace(/^\?/, "") === query;
  }
  if (item.to === "/staff") return (path === "/staff" && !search) || path.startsWith("/staff/applications");
  if (item.match && item.match !== "\u0000") return path.startsWith(item.match);
  return path === item.to || path.startsWith(`${item.to}/`);
}

/** Where a role lands after signing in — its first sidebar item. */
export function homePath(role: Role): string {
  if (role === "applicant") return "/my";
  return navFor(role)[0]?.to ?? "/staff";
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  const first = parts[0][0] ?? "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] ?? "" : "";
  return (first + last).toUpperCase();
}
