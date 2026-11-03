import type { Metadata, Viewport } from "next";
import { Public_Sans } from "next/font/google";

import "./globals.css";

// Public Sans is the US federal web typeface (US Web Design System): a neo-grotesque with tabular figures.
const sans = Public_Sans({ subsets: ["latin"], weight: ["400", "500", "700"], variable: "--font-sans", display: "swap" });

export const metadata: Metadata = {
  title: "TaxDiff",
  description: "Shows which of 40 synthetic tax year 2025 returns a rule or fact change reaches, which lines move, and where each return is routed next.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#151515" },
  ],
};

// Sets the saved theme before the first paint, so a dark page never flashes light.
const themeScript = `try{var t=localStorage.getItem("taxdiff:theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={sans.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <a href="#main" className="skip-link">Skip to the run</a>
        {children}
      </body>
    </html>
  );
}
