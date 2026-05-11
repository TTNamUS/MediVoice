import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MediVoice — AI Voice Receptionist",
  description: "Real-time voice AI for Sunrise Dental Clinic",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-gray-950 text-gray-100 antialiased">{children}</body>
    </html>
  );
}
