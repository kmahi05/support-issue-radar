import { CreateOrgForm } from "@/components/CreateOrgForm";
import { apiFetch } from "@/lib/api";

type Org = { org_id: string; name: string; timezone: string; role: string; created_at: string };

export default async function HomePage() {
  const { items } = await apiFetch<{ items: Org[] }>("/api/v1/orgs");
  return (
    <div className="space-y-8">
      <section>
        <h1 className="mb-4 text-xl font-semibold">Your organizations</h1>
        {items.length === 0 ? (
          <p className="text-zinc-600 dark:text-zinc-400">You are not in any organization yet.</p>
        ) : (
          <ul className="divide-y divide-zinc-200 rounded border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
            {items.map((o) => (
              <li key={o.org_id} className="flex items-center justify-between px-4 py-3">
                <span className="font-medium">{o.name}</span>
                <span className="text-sm text-zinc-600 dark:text-zinc-400">
                  {o.role} · {o.timezone}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <CreateOrgForm />
    </div>
  );
}
