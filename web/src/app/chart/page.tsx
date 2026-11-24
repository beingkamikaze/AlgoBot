import { Page } from "@/components/ui/kit";

import { ChartLoader } from "./loader";

export const metadata = { title: "Chart · TradingBot" };

export default function ChartPage() {
  return (
    <Page wide>
      <ChartLoader />
    </Page>
  );
}
