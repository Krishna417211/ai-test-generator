import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import * as api from "../utils/api";
import type { User } from "../utils/api";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, name?: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  adoptToken: (token: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const hydrate = async () => {
    try {
      setUser(await api.fetchMe());
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
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
  };
  const refresh = async () => { setUser(await api.fetchMe()); };
  const adoptToken = async (token: string) => {
    api.setToken(token);
    setUser(await api.fetchMe());
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, logout, refresh, adoptToken }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
