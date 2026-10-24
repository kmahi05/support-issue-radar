"use server";

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ApiError, apiFetch } from "@/lib/api";
import {
  ACCESS_COOKIE,
  REFRESH_COOKIE,
  apiUrl,
  setTokenCookies,
  type TokenPair,
} from "@/lib/session";

export type FormState = { error?: string };
export type CreateOrgState = { error?: string; created?: { name: string; ingest_secret: string } };

async function post(path: string, body: unknown) {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

async function loginWith(email: string, password: string): Promise<FormState | null> {
  const { ok, data } = await post("/api/v1/auth/login", { email, password });
  if (!ok) return { error: data?.error?.message ?? "Login failed" };
  setTokenCookies(await cookies(), data as TokenPair);
  return null;
}

export async function loginAction(_: FormState, form: FormData): Promise<FormState> {
  const failed = await loginWith(String(form.get("email")), String(form.get("password")));
  if (failed) return failed;
  redirect("/");
}

export async function registerAction(_: FormState, form: FormData): Promise<FormState> {
  const email = String(form.get("email"));
  const password = String(form.get("password"));
  const { ok, data } = await post("/api/v1/auth/register", {
    email,
    password,
    display_name: String(form.get("display_name")),
  });
  if (!ok) return { error: data?.error?.message ?? "Registration failed" };
  const failed = await loginWith(email, password);
  if (failed) return failed;
  redirect("/");
}

export async function logoutAction() {
  const store = await cookies();
  const refresh = store.get(REFRESH_COOKIE)?.value;
  if (refresh) await post("/api/v1/auth/logout", { refresh_token: refresh });
  store.delete(ACCESS_COOKIE);
  store.delete(REFRESH_COOKIE);
  redirect("/login");
}

export async function createOrgAction(_: CreateOrgState, form: FormData): Promise<CreateOrgState> {
  try {
    const org = await apiFetch<{ name: string; ingest_secret: string }>("/api/v1/orgs", {
      method: "POST",
      body: JSON.stringify({
        name: String(form.get("name")),
        timezone: String(form.get("timezone") || "UTC"),
      }),
    });
    revalidatePath("/");
    return { created: { name: org.name, ingest_secret: org.ingest_secret } };
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
}
