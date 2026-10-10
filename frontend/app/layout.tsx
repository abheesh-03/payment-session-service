import type { Metadata, Viewport } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Payment Session Demo",
  description: "Create and pay simulated payment sessions. No real money moves.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <div className="container site-header__inner">
            <Link href="/" className="brand">
              Payment Session Demo
            </Link>
            <p className="demo-note">Simulated payments only. No real money is transferred.</p>
          </div>
        </header>
        <main className="container main">{children}</main>
      </body>
    </html>
  );
}
