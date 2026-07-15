import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import * as api from "../utils/api";
import type { User } from "../utils/api";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  /** Token is present but the server couldn't confirm it (down/5xx). */
  unavailable: boolean;
  retry: () => Promise<void>;
  /** Resolves to where the attempt landed — a session, an OTP challenge, or a
   *  verification wall. Only "ok" means the user is now logged in. */
  login: (email: string, password: string) => Promise<api.LoginResult>;
  signup: (email: string, password: string, name?: string) => Promise<api.LoginResult>;
  /** Finish an OTP login with the emailed code. */
  completeOtp: (challengeId: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  adoptToken: (token: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);

  // A page refresh re-runs this. If the server is briefly unreachable (a
  // restart, a flaky network), retry rather than treating it as a logout —
  // otherwise a blip bounces the user off the page they were on.
  const hydrate = async () => {
    setLoading(true);
    // This tab's sessionStorage is empty on every new tab, even while another
    // tab is signed in — ask them before concluding nobody is logged in. Costs
    // a short wait only when there's genuinely no token to find.
    await api.requestSessionFromOtherTabs();
    for (let attempt = 0; ; attempt++) {
      try {
        setUser(await api.fetchMe());
        setUnavailable(false);
        setLoading(false);
        return;
      } catch (e) {
        if (!(e instanceof api.AuthUnavailableError)) {
          setUser(null);
          setUnavailable(false);
          setLoading(false);
          return;
        }
        if (attempt >= 2) {
          // Keep the token: it's probably still good, the server isn't there.
          setUnavailable(true);
          setLoading(false);
          return;
        }
        await sleep(400 * 2 ** attempt);
      }
    }
  };

  useEffect(() => { hydrate(); }, []);

  // Serve other tabs for as long as this one is open, and follow them out when
  // one logs out. Both listen on `storage`, which only fires cross-tab.
  useEffect(() => {
    const stopServing = api.serveSessionToOtherTabs();
    const stopListening = api.onLogoutElsewhere(() => {
      setUser(null);
      setUnavailable(false);
    });
    return () => { stopServing(); stopListening(); };
  }, []);

  // login/signup no longer imply a session — they may hand back an OTP
  // challenge or a verification wall instead. Only set the user when one was
  // actually issued, and let the caller route on `status`.
  const login = async (email: string, password: string) => {
    const result = await api.login(email, password);
    if (result.status === "ok" && result.user) setUser(result.user);
    return result;
  };
  const signup = async (email: string, password: string, name?: string) => {
    const result = await api.signup(email, password, name);
    if (result.status === "ok" && result.user) setUser(result.user);
    return result;
  };
  const completeOtp = async (challengeId: string, code: string) => {
    const { user } = await api.verifyLoginOtp(challengeId, code);
    setUser(user);
  };
  const logout = async () => {
    await api.logout();
    setUser(null);
    setUnavailable(false);
  };
  const refresh = async () => { setUser(await api.fetchMe()); };
  const adoptToken = async (token: string) => {
    api.setToken(token);
    setUser(await api.fetchMe());
  };

  return (
    <AuthContext.Provider value={{ user, loading, unavailable, retry: hydrate, login, signup, completeOtp, logout, refresh, adoptToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
