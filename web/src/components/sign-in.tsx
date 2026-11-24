"use client";

import { useState } from "react";

import { Notice } from "@/components/ui/kit";
import { supabase } from "@/lib/supabase";

export function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<{ tone: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(kind: "in" | "up") {
    const sb = supabase();
    if (!sb) return;
    setBusy(true);
    setMessage(null);
    const { error } =
      kind === "in"
        ? await sb.auth.signInWithPassword({ email, password })
        : await sb.auth.signUp({ email, password });
    setBusy(false);
    if (error) setMessage({ tone: "error", text: error.message });
    else if (kind === "up") setMessage({ tone: "success", text: "Account created. If email confirmation is on, check your inbox, then sign in." });
  }

  return (
    <form
      className="flex w-full flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        submit("in");
      }}
    >
      <label>
        <span className="field-label">Email</span>
        <input className="field h-9" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label>
        <span className="field-label">Password</span>
        <input className="field h-9" type="password" autoComplete="current-password" required minLength={8}
          value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      <button disabled={busy} className="btn btn-primary btn-lg mt-1 w-full">{busy ? "Signing in…" : "Sign in"}</button>
      <button type="button" disabled={busy} onClick={() => submit("up")} className="btn btn-ghost w-full">Create an account</button>
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
    </form>
  );
}
