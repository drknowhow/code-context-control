import { useEffect, useState } from "react";

export interface AuthState {
  accessToken: string | null;
  expiresAt: number | null;
}

export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({ accessToken: null, expiresAt: null });
  useEffect(() => {
    const raw = window.sessionStorage.getItem("ledgerlite.auth");
    if (raw) setState(JSON.parse(raw) as AuthState);
  }, []);
  return state;
}
