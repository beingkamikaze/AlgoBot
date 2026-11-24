"use client";

import dynamic from "next/dynamic";

// Browser only: the page reads a signal's levels from the address bar.
const OptionsWorkspace = dynamic(() => import("@/components/options-workspace"), {
  ssr: false,
  loading: () => <div className="flex flex-col gap-3"><div className="skeleton h-10 w-72" /><div className="skeleton h-16" /><div className="skeleton h-[60vh]" /></div>,
});

export function OptionsLoader() {
  return <OptionsWorkspace />;
}
