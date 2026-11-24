import { Page } from "@/components/ui/kit";

import { GlobalChartLoader } from "./loader";

export const metadata = { title: "XAUUSD chart · TradingBot" };

export default function GlobalChartPage() {
  return (
    <Page wide>
      <GlobalChartLoader />
    </Page>
  );
}
