import { GlobalPaper } from "@/components/global-paper";
import { Page } from "@/components/ui/kit";

export const metadata = { title: "XAUUSD paper · TradingBot" };

export default function GlobalPaperPage() {
  return (
    <Page wide>
      <GlobalPaper />
    </Page>
  );
}
