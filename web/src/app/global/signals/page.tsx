import { GlobalSignalsView } from "@/components/global-signals";
import { Page } from "@/components/ui/kit";

export const metadata = { title: "XAUUSD signals · TradingBot" };

export default function GlobalSignalsPage() {
  return (
    <Page wide>
      <GlobalSignalsView />
    </Page>
  );
}
