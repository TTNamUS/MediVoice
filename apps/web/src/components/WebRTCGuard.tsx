"use client";

import type { ReactNode } from "react";
import { useEffect, useState } from "react";

function canUseWebRTC() {
  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof window.RTCPeerConnection === "undefined" ||
    !navigator.mediaDevices?.getUserMedia
  ) {
    return false;
  }

  try {
    const connection = new RTCPeerConnection();
    connection.close();
    return true;
  } catch {
    return false;
  }
}

export function WebRTCGuard({ children }: { children: ReactNode }) {
  const [isSupported, setIsSupported] = useState<boolean | null>(null);

  useEffect(() => {
    setIsSupported(canUseWebRTC());
  }, []);

  if (isSupported === null) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-neutral-950 px-6 text-neutral-100">
        <p className="text-sm text-neutral-400">Loading voice client...</p>
      </main>
    );
  }

  if (!isSupported) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-neutral-950 px-6 text-neutral-100">
        <section className="max-w-md rounded-2xl border border-neutral-800 bg-neutral-900 p-6 shadow-2xl">
          <h1 className="text-lg font-semibold">WebRTC is unavailable</h1>
          <p className="mt-3 text-sm leading-6 text-neutral-300">
            LiveKit voice calls require a browser with WebRTC enabled, microphone
            access, and a secure origin such as <code>https://</code> or{" "}
            <code>http://localhost</code>.
          </p>
          <p className="mt-3 text-sm leading-6 text-neutral-400">
            If you are in a browser preview or embedded frame, open the app in a
            normal browser tab.
          </p>
        </section>
      </main>
    );
  }

  return <>{children}</>;
}
