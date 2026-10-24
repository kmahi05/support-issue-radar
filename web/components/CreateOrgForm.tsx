"use client";

import { useActionState } from "react";

import { createOrgAction, type CreateOrgState } from "@/app/actions";

const input =
  "w-full rounded border border-zinc-300 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900";

export function CreateOrgForm() {
  const [state, formAction, pending] = useActionState<CreateOrgState, FormData>(
    createOrgAction,
    {},
  );
  return (
    <section>
      <h2 className="mb-3 text-lg font-semibold">Create an organization</h2>
      <form action={formAction} className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block flex-1 text-sm">
          <span className="mb-1 block font-medium">Name</span>
          <input name="name" required maxLength={120} className={input} />
        </label>
        <label className="block flex-1 text-sm">
          <span className="mb-1 block font-medium">Timezone (IANA)</span>
          <input name="timezone" defaultValue="UTC" className={input} />
        </label>
        <button
          type="submit"
          disabled={pending}
          className="rounded bg-zinc-900 px-4 py-2 font-medium text-white disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900"
        >
          Create
        </button>
      </form>
      {state.error && (
        <p role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">
          {state.error}
        </p>
      )}
      {state.created && (
        <div className="mt-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950">
          <p className="font-medium">Created {state.created.name}. Ingest secret (shown once):</p>
          <code className="mt-1 block break-all">{state.created.ingest_secret}</code>
        </div>
      )}
    </section>
  );
}
