"use client";

import dynamic from "next/dynamic";

// Browser only: the chart reads your saved symbol, timeframe and indicators from this browser.
const ChartWorkspace = dynamic(() => import("@/components/chart-workspace"), {
  ssr: false,
  loading: () => <div className="flex flex-col gap-3"><div className="skeleton h-12" /><div className="skeleton h-[62vh] min-h-[380px]" /></div>,
});

export function ChartLoader() {
  return <ChartWorkspace />;
}
