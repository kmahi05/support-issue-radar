// Cookie names and helpers shared by middleware (edge) and server code. No "server-only" here
// because middleware imports it.
export const ACCESS_COOKIE = "sir_access";
export const REFRESH_COOKIE = "sir_refresh";

export type TokenPair = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
};

type CookieSetter = {
  set(name: string, value: string, options: Record<string, unknown>): unknown;
};

const base = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};

export function setTokenCookies(store: CookieSetter, pair: TokenPair) {
  // The access cookie expires a little before the token, so middleware refreshes it in time.
  store.set(ACCESS_COOKIE, pair.access_token, {
    ...base,
    maxAge: Math.max(pair.expires_in - 30, 30),
  });
  store.set(REFRESH_COOKIE, pair.refresh_token, { ...base, maxAge: 14 * 24 * 3600 });
}

export function apiUrl(path: string) {
  return `${process.env.API_URL ?? "http://localhost:8003"}${path}`;
}

export async function refreshTokens(refreshToken: string): Promise<TokenPair | null> {
  const res = await fetch(apiUrl("/api/v1/auth/refresh"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
    cache: "no-store",
  });
  return res.ok ? ((await res.json()) as TokenPair) : null;
}
