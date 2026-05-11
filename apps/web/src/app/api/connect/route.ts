import { NextResponse } from "next/server";

function normalizeServerBotBaseUrl(value: string | undefined): string {
  const raw = value?.trim() || "http://localhost:8000";
  const withProtocol = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
  return withProtocol
    .replace(/\/+$/, "")
    .replace(/\/connect$/i, "");
}

const BOT_BASE_URL = normalizeServerBotBaseUrl(
  process.env.BOT_BASE_URL ?? process.env.NEXT_PUBLIC_BOT_BASE_URL,
);

export async function POST() {
  const connectUrl = `${BOT_BASE_URL}/connect`;

  try {
    const response = await fetch(connectUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    const text = await response.text();
    const contentType = response.headers.get("content-type") ?? "application/json";

    return new NextResponse(text, {
      status: response.status,
      headers: { "content-type": contentType },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json(
      {
        detail: `Next.js proxy could not reach ${connectUrl}: ${message}`,
      },
      { status: 502 },
    );
  }
}
