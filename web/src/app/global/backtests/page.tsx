import { GlobalBacktests } from "@/components/global-backtests";
import { Page } from "@/components/ui/kit";

export const metadata = { title: "XAUUSD backtests · TradingBot" };

export default function GlobalBacktestsPage() {
  return (
    <Page>
      <GlobalBacktests />
    </Page>
  );
}
