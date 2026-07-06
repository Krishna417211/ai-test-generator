import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import { AuthProvider } from "./context/AuthContext";
import AuroraBackground from "./components/AuroraBackground";
import ScrollProgress from "./components/ScrollProgress";
import NavBar from "./components/NavBar";
import Footer from "./components/Footer";
import ProtectedRoute from "./components/ProtectedRoute";
import Home from "./pages/Home";
import Generate from "./pages/Generate";
import Publish from "./pages/Publish";
import Scan from "./pages/Scan";
import Status from "./pages/Status";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import AuthCallback from "./pages/AuthCallback";

function AnimatedRoutes() {
  const location = useLocation();
  const guard = (el: JSX.Element) => <ProtectedRoute>{el}</ProtectedRoute>;
  return (
    <AnimatePresence mode="wait">
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<Home />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/generate" element={guard(<Generate />)} />
        <Route path="/publish" element={guard(<Publish />)} />
        <Route path="/scan" element={guard(<Scan />)} />
        <Route path="/status" element={guard(<Status />)} />
        <Route path="*" element={<Home />} />
      </Routes>
    </AnimatePresence>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuroraBackground />
        <ScrollProgress />
        <div className="min-h-screen flex flex-col">
          <NavBar />
          <div className="flex-1 pt-8 pb-4">
            <AnimatedRoutes />
          </div>
          <Footer />
        </div>
      </AuthProvider>
    </BrowserRouter>
  );
}
