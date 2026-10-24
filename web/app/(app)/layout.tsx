import { logoutAction } from "@/app/actions";
import { apiFetch } from "@/lib/api";

export type Me = {
  user_id: string;
  email: string;
  display_name: string;
  orgs: { org_id: string; name: string; role: string }[];
};

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const me = await apiFetch<Me>("/api/v1/me");
  return (
    <>
      <header className="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
          <span className="font-semibold">Support Issue Radar</span>
          <div className="flex items-center gap-3 text-sm">
            <span className="truncate text-zinc-600 dark:text-zinc-400">{me.email}</span>
            <form action={logoutAction}>
              <button type="submit" className="rounded border border-zinc-300 px-2 py-1 dark:border-zinc-700">
                Log out
              </button>
            </form>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
    </>
  );
}
