"use client";

import dynamic from "next/dynamic";

// Browser only: the chart reads your saved timeframe and indicators from this browser.
const GlobalChartWorkspace = dynamic(() => import("@/components/global-chart-workspace"), {
  ssr: false,
  loading: () => <div className="flex flex-col gap-3"><div className="skeleton h-12" /><div className="skeleton h-[62vh] min-h-[380px]" /></div>,
});

export function GlobalChartLoader() {
  return <GlobalChartWorkspace />;
}
