import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "silicon-notebook",
  description: "Semiconductor knowhow notebook beta"
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

