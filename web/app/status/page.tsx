import type { Metadata } from "next";

import StatusPage from "@/components/StatusPage";

export const metadata: Metadata = {
  title: "System status · TaxDiff",
  description: "Every API endpoint and outside service of the dashboard, checked live from your browser.",
};

export default function Page() {
  return <StatusPage />;
}
