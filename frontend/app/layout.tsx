import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dealroom | Negotiation sandbox",
  description: "A creator-controlled sandbox for brand negotiations.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
