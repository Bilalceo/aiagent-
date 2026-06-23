import "./globals.css";
import type { Metadata } from "next";
import { LanguageProvider } from "@/lib/i18n";
import SiteHeader from "@/components/SiteHeader";

export const metadata: Metadata = {
  title: "Urologiya klinikasi — AI registrator",
  description: "Urologiya klinikasi AI ovozli registrator boshqaruv paneli",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="uz">
      <body className="bg-slate-50 text-slate-900">
        <LanguageProvider>
          <SiteHeader />
          <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
        </LanguageProvider>
      </body>
    </html>
  );
}
