import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api, getToken, onSessionEnded, setToken, type User } from "./api";
import { homePath } from "./nav";

interface AuthState {
  user: User | null;
  loading: boolean;
  notice: string | null;
  signIn: (token: string, user: User) => void;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  clearNotice: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(Boolean(getToken()));
  const [notice, setNotice] = useState<string | null>(null);
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      setUser(await api.get<User>("/me"));
    } catch {
      setToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    onSessionEnded((err) => {
      setToken(null);
      setUser(null);
      setNotice(
        err.code === "ERR_TOKEN_EXPIRED"
          ? `${err.message} Anything you had entered was saved automatically.`
          : "Please sign in to continue.",
      );
      navigate("/");
    });
    void refresh();
  }, [navigate, refresh]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      notice,
      signIn: (token, u) => {
        setToken(token);
        setUser(u);
        setNotice(null);
      },
      signOut: async () => {
        try {
          await api.post("/auth/logout");
        } catch {
          /* already signed out */
        }
        setToken(null);
        setUser(null);
        navigate("/");
      },
      refresh,
      clearNotice: () => setNotice(null),
    }),
    [user, loading, notice, navigate, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** Where a role lands: the first item of its own sidebar (see nav.ts). */
export function homeFor(user: User) {
  return homePath(user.role);
}
