import { GlobalMonitor } from "@/components/global-monitor";
import { Page } from "@/components/ui/kit";

export const metadata = { title: "XAUUSD monitor · TradingBot" };

export default function GlobalMonitorPage() {
  return (
    <Page wide>
      <GlobalMonitor />
    </Page>
  );
}
