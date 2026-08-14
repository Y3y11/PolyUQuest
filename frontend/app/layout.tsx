import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PolyUQuest — Structure-Aware RAG over Web Heterogeneous Graphs",
  description:
    "PolyUQuest: Structure-Aware Retrieval-Augmented Generation over Web Heterogeneous Graphs, demonstrated on the PolyU website.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <body className="font-body antialiased">{children}</body>
    </html>
  );
}
