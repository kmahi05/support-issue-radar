import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ACCESS_COOKIE, REFRESH_COOKIE, apiUrl, refreshTokens, setTokenCookies } from "./session";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function send(path: string, init: RequestInit, token?: string) {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(apiUrl(path), { ...init, headers, cache: "no-store" });
}

/** Call FastAPI with the user's Bearer token. On 401: one refresh, update cookies, retry once. */
export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const store = await cookies();
  let res = await send(path, init, store.get(ACCESS_COOKIE)?.value);
  if (res.status === 401) {
    const refresh = store.get(REFRESH_COOKIE)?.value;
    const pair = refresh ? await refreshTokens(refresh) : null;
    if (!pair) redirect("/login");
    let saved = true;
    try {
      setTokenCookies(store, pair);
    } catch {
      // Server Components cannot set cookies, and the old refresh token is now rotated.
      // Middleware refreshes before the access cookie expires, so this path is rare.
      saved = false;
    }
    if (!saved) redirect("/login");
    res = await send(path, init, pair.access_token);
    if (res.status === 401) redirect("/login");
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = body?.error ?? {};
    throw new ApiError(res.status, err.code ?? "http_error", err.message ?? res.statusText);
  }
  return body as T;
}
