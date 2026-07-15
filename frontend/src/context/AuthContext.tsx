import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import * as api from "../utils/api";
import type { User } from "../utils/api";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  /** Token is present but the server couldn't confirm it (down/5xx). */
  unavailable: boolean;
  retry: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, name?: string) => Promise<void>;
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

  const login = async (email: string, password: string) => {
    const { user } = await api.login(email, password);
    setUser(user);
  };
  const signup = async (email: string, password: string, name?: string) => {
    const { user } = await api.signup(email, password, name);
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
    <AuthContext.Provider value={{ user, loading, unavailable, retry: hydrate, login, signup, logout, refresh, adoptToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
