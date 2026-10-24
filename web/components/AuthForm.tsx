"use client";

import Link from "next/link";
import { useActionState } from "react";

import type { FormState } from "@/app/actions";

type Field = { name: string; label: string; type: string; autoComplete: string; minLength?: number };

export function AuthForm(props: {
  title: string;
  submit: string;
  fields: Field[];
  action: (state: FormState, form: FormData) => Promise<FormState>;
  footer: { text: string; href: string; link: string };
}) {
  const [state, formAction, pending] = useActionState(props.action, {});
  return (
    <main className="mx-auto mt-24 w-full max-w-sm px-4">
      <h1 className="mb-6 text-2xl font-semibold">{props.title}</h1>
      <form action={formAction} className="space-y-4">
        {props.fields.map((f) => (
          <label key={f.name} className="block text-sm">
            <span className="mb-1 block font-medium">{f.label}</span>
            <input
              name={f.name}
              type={f.type}
              autoComplete={f.autoComplete}
              minLength={f.minLength}
              required
              className="w-full rounded border border-zinc-300 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-900"
            />
          </label>
        ))}
        {state.error && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {state.error}
          </p>
        )}
        <button
          type="submit"
          disabled={pending}
          className="w-full rounded bg-zinc-900 px-3 py-2 font-medium text-white disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {pending ? "Please wait…" : props.submit}
        </button>
      </form>
      <p className="mt-4 text-sm">
        {props.footer.text}{" "}
        <Link href={props.footer.href} className="underline">
          {props.footer.link}
        </Link>
      </p>
    </main>
  );
}
