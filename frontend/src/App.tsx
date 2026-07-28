import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import { AuthProvider } from "./context/AuthContext";
import AuroraBackground from "./components/AuroraBackground";
import ScrollProgress from "./components/ScrollProgress";
import NavBar from "./components/NavBar";
import Footer from "./components/Footer";
import ProtectedRoute from "./components/ProtectedRoute";
import ErrorBoundary from "./components/ErrorBoundary";
import DashboardLayout from "./components/DashboardLayout";
import AdminLayout from "./components/AdminLayout";
import Home from "./pages/Home";
import HowItWorks from "./pages/HowItWorks";
import Dashboard from "./pages/Dashboard";
import Generate from "./pages/Generate";
import Publish from "./pages/Publish";
import Scan from "./pages/Scan";
import Status from "./pages/Status";
import Settings from "./pages/Settings";
import CliAuth from "./pages/CliAuth";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import AuthCallback from "./pages/AuthCallback";
import VerifyEmail from "./pages/VerifyEmail";
import ForgotPassword from "./pages/ForgotPassword";
import ResetPassword from "./pages/ResetPassword";
import Profile from "./pages/Profile";
import AdminOverview from "./pages/AdminOverview";
import AdminUsers from "./pages/AdminUsers";
import AdminSystem from "./pages/AdminSystem";
import NotFound from "./pages/NotFound";

function AnimatedRoutes() {
  const location = useLocation();
  return (
    <AnimatePresence mode="wait">
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<Home />} />
        {/* Public on purpose: this is the page that explains what we do with a
            repo before you hand us one, so it has to answer to someone who
            hasn't signed up yet. */}
        <Route path="/how-it-works" element={<HowItWorks />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="/auth/callback" element={<AuthCallback />} />
        {/* Reached from an email link, by definition while logged out — these
            must stay outside the ProtectedRoute guard or the link bounces to
            /login and the token in the URL is lost. */}
        <Route path="/verify-email" element={<VerifyEmail />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
        <Route path="/reset-password" element={<ResetPassword />} />

        {/* Everything signed-in renders inside the dashboard shell, so the
            sidebar is present on every one of these pages. The guard wraps the
            layout rather than each page: one check, and no way to add a route
            here that quietly forgets it. */}
        <Route element={<ProtectedRoute><DashboardLayout /></ProtectedRoute>}>
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/generate" element={<Generate />} />
          <Route path="/publish" element={<Publish />} />
          <Route path="/scan" element={<Scan />} />
          <Route path="/status" element={<Status />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/profile" element={<Profile />} />
          {/* The browser half of the CLI's device-code login. Inside the guard
              on purpose: the signed-in session IS the authorisation for the
              token the terminal collects, so an anonymous visitor must be sent
              to sign in first — and ProtectedRoute brings them back here with
              the ?code= intact. */}
          <Route path="/cli" element={<CliAuth />} />
        </Route>

        {/* The admin console is its own portal, not a section of the app.
            Deliberately outside ProtectedRoute: /admin must be reachable while
            logged out and answer with its OWN sign-in (AdminLayout renders
            AdminLogin) rather than bouncing to the product's /login and
            dumping an admin on the user dashboard afterwards. AdminLayout is
            the gate; the server's allowlist is the authority. */}
        <Route path="/admin" element={<AdminLayout />}>
          <Route index element={<AdminOverview />} />
          <Route path="users" element={<AdminUsers />} />
          <Route path="system" element={<AdminSystem />} />
        </Route>

        <Route path="*" element={<NotFound />} />
      </Routes>
    </AnimatePresence>
  );
}

/** The product chrome — NavBar and Footer — around the routes.
 *
 *  The admin portal opts out of all of it. It brings its own header and has no
 *  business showing "Generate / Publish / Scan" links or the marketing footer:
 *  it's a separate console that happens to be served by the same app. Keying
 *  off the path (rather than, say, whether the user is an admin) means the
 *  logged-out /admin sign-in is bare too, instead of appearing under a product
 *  nav offering to sign you up.
 */
function Chrome() {
  const { pathname } = useLocation();
  const isAdminPortal = pathname === "/admin" || pathname.startsWith("/admin/");

  if (isAdminPortal) {
    return <ErrorBoundary><AnimatedRoutes /></ErrorBoundary>;
  }

  return (
    <div className="min-h-screen flex flex-col">
      <NavBar />
      <div className="flex-1 pt-8 pb-4">
        <ErrorBoundary>
          <AnimatedRoutes />
        </ErrorBoundary>
      </div>
      <Footer />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuroraBackground />
        <ScrollProgress />
        <Chrome />
      </AuthProvider>
    </BrowserRouter>
  );
}
