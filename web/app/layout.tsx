import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Support Issue Radar",
  description: "Groups support tickets into ranked issues",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
