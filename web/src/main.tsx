import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import type { Role } from "./api";
import { AuthProvider, homeFor, useAuth } from "./auth";
import { Layout, Spinner } from "./components/ui";
import { AdminUsers } from "./pages/AdminUsers";
import { Documents, MyApplication, Submitted } from "./pages/Applicant";
import { Apply } from "./pages/Apply";
import { ForgotPassword, Register, ResetPassword, SignIn, VerifyEmail } from "./pages/Auth";
import { AcademicAssessment, ComplianceReview } from "./pages/Assessment";
import { CasApproval, CasRegister, PreCas } from "./pages/Cas";
import { AgentDetail } from "./pages/AgentDetail";
import { Settings } from "./pages/Settings";
import { Agents } from "./pages/Agents";
import { Dashboard } from "./pages/Dashboard";
import { Fees } from "./pages/Fees";
import { ComplianceMonitoring, Reports } from "./pages/Metrics";
import { MyOffer, OfferBuilder } from "./pages/Offer";
import { EventDetail, ReportingQueue, Student, Students } from "./pages/Student";
import { StaffApplication, StaffApplications } from "./pages/Staff";
import "./styles.css";

const STAFF: Role[] = ["admissions", "compliance", "finance", "senior", "admin"];
const ADMISSIONS: Role[] = ["admissions", "senior", "admin"];
const COMPLIANCE: Role[] = ["compliance", "senior", "admin"];
const PRECAS: Role[] = ["admissions", "compliance", "senior", "admin"];
const SENIOR: Role[] = ["senior", "admin"];

function Guard({ roles, children }: { roles: Role[]; children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <Layout><Spinner /></Layout>;
  if (!user) return <Navigate to="/" replace />;
  if (!roles.includes(user.role)) return <Navigate to={homeFor(user)} replace />;
  return <>{children}</>;
}

function NotFound() {
  return (
    <Layout>
      <h1>Page not found</h1>
      <p>Check the address, or go back to the <a href="/">start page</a>.</p>
    </Layout>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<SignIn />} />
          <Route path="/register" element={<Register />} />
          <Route path="/verify-email" element={<VerifyEmail />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/apply" element={<Guard roles={["applicant"]}><Apply /></Guard>} />
          <Route path="/apply/documents" element={<Guard roles={["applicant"]}><Documents /></Guard>} />
          <Route path="/apply/submitted" element={<Guard roles={["applicant"]}><Submitted /></Guard>} />
          <Route path="/my" element={<Guard roles={["applicant"]}><MyApplication /></Guard>} />
          <Route path="/my/offer" element={<Guard roles={["applicant"]}><MyOffer /></Guard>} />
          <Route path="/staff" element={<Guard roles={STAFF}><StaffApplications /></Guard>} />
          <Route path="/staff/applications/:id" element={<Guard roles={STAFF}><StaffApplication /></Guard>} />
          <Route path="/staff/applications/:id/academic" element={<Guard roles={ADMISSIONS}><AcademicAssessment /></Guard>} />
          <Route path="/staff/applications/:id/offer" element={<Guard roles={ADMISSIONS}><OfferBuilder /></Guard>} />
          <Route path="/staff/applications/:id/compliance" element={<Guard roles={COMPLIANCE}><ComplianceReview /></Guard>} />
          <Route path="/staff/applications/:id/precas" element={<Guard roles={PRECAS}><PreCas /></Guard>} />
          <Route path="/staff/applications/:id/cas" element={<Guard roles={SENIOR}><CasApproval /></Guard>} />
          <Route path="/staff/applications/:id/fees" element={<Guard roles={STAFF}><Fees /></Guard>} />
          <Route path="/staff/cas" element={<Guard roles={STAFF}><CasRegister /></Guard>} />
          <Route path="/staff/students" element={<Guard roles={PRECAS}><Students /></Guard>} />
          <Route path="/staff/students/:id" element={<Guard roles={PRECAS}><Student /></Guard>} />
          <Route path="/staff/events" element={<Guard roles={PRECAS}><ReportingQueue /></Guard>} />
          <Route path="/staff/events/:id" element={<Guard roles={PRECAS}><EventDetail /></Guard>} />
          <Route path="/staff/dashboard" element={<Guard roles={PRECAS}><Dashboard /></Guard>} />
          <Route path="/staff/agents" element={<Guard roles={PRECAS}><Agents /></Guard>} />
          <Route path="/staff/agents/:id" element={<Guard roles={PRECAS}><AgentDetail /></Guard>} />
          <Route path="/staff/compliance" element={<Guard roles={COMPLIANCE}><ComplianceMonitoring /></Guard>} />
          <Route path="/staff/reports" element={<Guard roles={COMPLIANCE}><Reports /></Guard>} />
          <Route path="/admin/users" element={<Guard roles={["admin"]}><AdminUsers /></Guard>} />
          <Route path="/settings" element={<Guard roles={["finance", "senior", "admin"]}><Settings /></Guard>} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
