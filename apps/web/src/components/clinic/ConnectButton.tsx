"use client";

export type VoiceConnectionState =
  | "idle"
  | "connecting"
  | "connected"
  | "ready"
  | "disconnecting"
  | "disconnected"
  | "error";

const LABEL: Record<string, string> = {
  idle: "Start Call",
  connecting: "Connecting...",
  connected: "End Call",
  ready: "End Call",
  disconnecting: "Disconnecting...",
  disconnected: "Start Call",
  error: "Retry",
};

const STYLE: Record<string, string> = {
  idle: "bg-blue-600 hover:bg-blue-500 text-white",
  connecting: "bg-gray-600 text-gray-300 cursor-wait",
  connected: "bg-red-700 hover:bg-red-600 text-white",
  ready: "bg-red-700 hover:bg-red-600 text-white",
  disconnecting: "bg-gray-600 text-gray-300 cursor-wait",
  disconnected: "bg-blue-600 hover:bg-blue-500 text-white",
  error: "bg-amber-600 hover:bg-amber-500 text-white",
};

type ConnectButtonProps = {
  transportState: VoiceConnectionState;
  onConnect: () => void | Promise<void>;
  onDisconnect: () => void | Promise<void>;
};

export function ConnectButton({
  transportState,
  onConnect,
  onDisconnect,
}: ConnectButtonProps) {
  const busy = transportState === "connecting" || transportState === "disconnecting";
  const isConnected = transportState === "ready" || transportState === "connected";

  const handleClick = () => {
    if (busy) return;

    if (isConnected) {
      void onDisconnect();
    } else {
      void onConnect();
    }
  };

  const label = LABEL[transportState] ?? "Start Call";
  const style = STYLE[transportState] ?? STYLE.idle;

  return (
    <button
      onClick={handleClick}
      disabled={busy}
      className={`px-6 py-2.5 rounded-xl font-semibold text-sm transition-colors duration-150 disabled:opacity-50 ${style}`}
    >
      {label}
    </button>
  );
}
