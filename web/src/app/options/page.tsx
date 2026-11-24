import { Page } from "@/components/ui/kit";

import { OptionsLoader } from "./loader";

export const metadata = { title: "Options · TradingBot" };

export default function OptionsPage() {
  return (
    <Page wide>
      <OptionsLoader />
    </Page>
  );
}
