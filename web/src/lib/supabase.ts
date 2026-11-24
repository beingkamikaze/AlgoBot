import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

let client: SupabaseClient | null = null;

/** The browser's Supabase client, or null until web/.env.local is filled in. */
export function supabase(): SupabaseClient | null {
  if (!url || !anonKey) return null;
  client ??= createBrowserClient(url, anonKey);
  return client;
}
