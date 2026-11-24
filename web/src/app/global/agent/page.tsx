import { GlobalAgentView } from "@/components/global-agent";
import { Page } from "@/components/ui/kit";

export const metadata = { title: "XAUUSD agent · TradingBot" };

export default function GlobalAgentPage() {
  return (
    <Page wide>
      <GlobalAgentView />
    </Page>
  );
}
