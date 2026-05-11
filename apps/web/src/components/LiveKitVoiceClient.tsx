"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DisconnectReason,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import type {
  RemoteParticipant,
  RemoteTrack,
  RemoteTrackPublication,
} from "livekit-client";

import { AgentStatusBadge, AgentState } from "@/components/clinic/AgentStatusBadge";
import { ClinicHeader } from "@/components/clinic/ClinicHeader";
import { ConnectButton } from "@/components/clinic/ConnectButton";
import type { VoiceConnectionState } from "@/components/clinic/ConnectButton";
import { TranscriptPane } from "@/components/clinic/TranscriptPane";
import { WebRTCGuard } from "@/components/WebRTCGuard";

const CONNECT_URL = "/api/connect";

type ConnectResponse = {
  url: string;
  token: string;
  room_name: string;
  session_id: string;
};

type TranscriptEntry = {
  role: "user" | "bot";
  text: string;
};

type DataMessage = {
  role: "user" | "bot" | null;
  text: string | null;
  agent: AgentState | null;
  eventType: string | null;
  metrics: string | null;
};

type CallActivity =
  | "Idle"
  | "Listening"
  | "Processing"
  | "Thinking"
  | "Speaking"
  | "Connected";

function parseAgentMarker(text: string): AgentState | null {
  const match = text.match(/\[\[AGENT:(\w+)\]\]/);
  if (!match) return null;
  const agent = match[1] as AgentState;
  return ["triage", "booking", "faq", "billing"].includes(agent) ? agent : null;
}

function readNestedText(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return null;

  const objectValue = value as Record<string, unknown>;
  return (
    readNestedText(objectValue.text) ??
    readNestedText(objectValue.transcript) ??
    readNestedText(objectValue.message) ??
    readNestedText(objectValue.content)
  );
}

function emptyDataMessage(eventType: string | null = null): DataMessage {
  return { role: null, text: null, agent: null, eventType, metrics: null };
}

function formatMetrics(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;

  const metrics = data as Record<string, unknown>;
  const ttfb = Array.isArray(metrics.ttfb)
    ? metrics.ttfb
        .map((item) => {
          if (!item || typeof item !== "object") return null;
          const metric = item as Record<string, unknown>;
          const processor =
            typeof metric.processor === "string"
              ? metric.processor.replace(/#\d+$/, "")
              : "processor";
          const value = typeof metric.value === "number" ? metric.value : null;
          return value === null ? null : `${processor} ${Math.round(value * 1000)}ms`;
        })
        .filter(Boolean)
        .join(" · ")
    : "";

  const tokens = Array.isArray(metrics.tokens)
    ? metrics.tokens
        .map((item) => {
          if (!item || typeof item !== "object") return null;
          const metric = item as Record<string, unknown>;
          const total =
            typeof metric.total_tokens === "number" ? metric.total_tokens : null;
          return total === null ? null : `${total} tokens`;
        })
        .filter(Boolean)
        .join(" · ")
    : "";

  return ttfb || tokens || null;
}

function extractDataMessage(payload: Uint8Array): DataMessage {
  const raw = new TextDecoder().decode(payload);
  if (!raw.trim()) return emptyDataMessage();

  try {
    const message = JSON.parse(raw) as Record<string, unknown>;
    const label = typeof message.label === "string" ? message.label : "";
    const type = typeof message.type === "string" ? message.type : "";
    const text = readNestedText(message) ?? readNestedText(message.data);
    const agent = text ? parseAgentMarker(text) : null;
    const metrics = type === "metrics" ? formatMetrics(message.data) : null;

    const isUserTranscript =
      type.includes("user-transcription") ||
      type.includes("user-transcript") ||
      type === "transcription" ||
      type === "user";
    const isBotTranscript =
      type.includes("bot-transcription") ||
      type.includes("bot-transcript") ||
      type === "bot" ||
      type === "message";

    if (text && isUserTranscript) {
      return { role: "user", text, agent, eventType: type, metrics };
    }
    if (text && isBotTranscript) {
      return { role: "bot", text, agent, eventType: type, metrics };
    }

    const isRtviEvent = label === "rtvi-ai" || type.length > 0;
    if (isRtviEvent) {
      return { role: null, text: null, agent, eventType: type, metrics };
    }

    return text
      ? { role: null, text, agent, eventType: type || "text", metrics }
      : emptyDataMessage();
  } catch {
    return {
      role: null,
      text: raw,
      agent: parseAgentMarker(raw),
      eventType: "text",
      metrics: null,
    };
  }
}

function activityFromEvent(type: string | null): CallActivity | null {
  switch (type) {
    case "user-started-speaking":
      return "Listening";
    case "user-stopped-speaking":
      return "Processing";
    case "bot-llm-started":
      return "Thinking";
    case "bot-llm-stopped":
    case "bot-tts-started":
    case "bot-started-speaking":
      return "Speaking";
    case "bot-tts-stopped":
    case "bot-stopped-speaking":
      return "Connected";
    default:
      return null;
  }
}

export function LiveKitVoiceClient() {
  const roomRef = useRef<Room | null>(null);
  const audioContainerRef = useRef<HTMLDivElement>(null);
  const expectedRawTextRoleRef = useRef<"user" | "bot">("bot");
  const lastTranscriptRef = useRef<{ role: "user" | "bot"; text: string; at: number } | null>(
    null,
  );
  const [agentState, setAgentState] = useState<AgentState>("triage");
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [connectionState, setConnectionState] =
    useState<VoiceConnectionState>("idle");
  const [activity, setActivity] = useState<CallActivity>("Idle");
  const [lastMetrics, setLastMetrics] = useState<string | null>(null);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cleanupAudioElements = useCallback(() => {
    audioContainerRef.current?.querySelectorAll("audio").forEach((el) => {
      el.remove();
    });
  }, []);

  const handleTrackSubscribed = useCallback(
    (
      track: RemoteTrack,
      _publication: RemoteTrackPublication,
      _participant: RemoteParticipant,
    ) => {
      if (track.kind !== Track.Kind.Audio) return;
      const audioElement = track.attach();
      audioElement.autoplay = true;
      audioElement.dataset.livekitTrack = "bot-audio";
      audioContainerRef.current?.appendChild(audioElement);
    },
    [],
  );

  const handleTrackUnsubscribed = useCallback((track: RemoteTrack) => {
    track.detach().forEach((element) => element.remove());
  }, []);

  const handleDataReceived = useCallback((payload: Uint8Array) => {
    const message = extractDataMessage(payload);

    if (message.agent) {
      setAgentState(message.agent);
    }

    if (message.metrics) {
      setLastMetrics(message.metrics);
    }

    const nextActivity = activityFromEvent(message.eventType);
    if (nextActivity) {
      setActivity(nextActivity);
    }

    if (message.eventType === "user-started-speaking") {
      expectedRawTextRoleRef.current = "user";
    }
    if (message.eventType === "bot-llm-started") {
      expectedRawTextRoleRef.current = "bot";
    }

    const text = message.text;
    if (!text) return;
    const role = message.role ?? expectedRawTextRoleRef.current;
    const normalizedText = text.trim();
    if (!normalizedText) return;

    const lastTranscript = lastTranscriptRef.current;
    const now = Date.now();
    if (
      lastTranscript &&
      lastTranscript.role === role &&
      lastTranscript.text.toLowerCase() === normalizedText.toLowerCase() &&
      now - lastTranscript.at < 2500
    ) {
      return;
    }

    setTranscript((prev) => [
      ...prev,
      { role, text: normalizedText },
    ]);
    lastTranscriptRef.current = { role, text: normalizedText, at: now };

    if (role === "user") {
      expectedRawTextRoleRef.current = "bot";
    }
  }, []);

  const disconnect = useCallback(async () => {
    const room = roomRef.current;
    if (!room) {
      setConnectionState("disconnected");
      return;
    }

    setConnectionState("disconnecting");
    await room.localParticipant.setMicrophoneEnabled(false);
    room.disconnect();
    roomRef.current = null;
    cleanupAudioElements();
    setIsMicEnabled(false);
    setAgentState("triage");
    setActivity("Idle");
    setConnectionState("disconnected");
  }, [cleanupAudioElements]);

  const connect = useCallback(async () => {
    setConnectionState("connecting");
    setError(null);
    setTranscript([]);
    setAgentState("triage");
    setActivity("Processing");
    setLastMetrics(null);
    expectedRawTextRoleRef.current = "bot";
    lastTranscriptRef.current = null;

    try {
      const response = await fetch(CONNECT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });

      if (!response.ok) {
        const errorBody = await response.json().catch(() => null);
        const detail =
          errorBody && typeof errorBody.detail === "string"
            ? `: ${errorBody.detail}`
            : "";
        throw new Error(`Connect failed: ${response.status}${detail}`);
      }

      const credentials = (await response.json()) as ConnectResponse;
      const room = new Room({
        adaptiveStream: true,
        dynacast: true,
      });

      room
        .on(RoomEvent.TrackSubscribed, handleTrackSubscribed)
        .on(RoomEvent.TrackUnsubscribed, handleTrackUnsubscribed)
        .on(RoomEvent.DataReceived, handleDataReceived)
        .on(RoomEvent.Disconnected, (_reason?: DisconnectReason) => {
          roomRef.current = null;
          cleanupAudioElements();
          setIsMicEnabled(false);
          setActivity("Idle");
          setConnectionState("disconnected");
        });

      await room.connect(credentials.url, credentials.token);
      await room.localParticipant.setMicrophoneEnabled(true);

      roomRef.current = room;
      setIsMicEnabled(true);
      setActivity("Connected");
      setConnectionState("connected");
    } catch (err) {
      const message =
        err instanceof TypeError && err.message === "Failed to fetch"
          ? `Failed to fetch ${CONNECT_URL}. Check that the Next.js dev server is running and that its API proxy can reach FastAPI on port 8000.`
          : err instanceof Error
            ? err.message
            : "Unable to connect";
      setError(message);
      setActivity("Idle");
      setConnectionState("error");
      cleanupAudioElements();
    }
  }, [
    cleanupAudioElements,
    handleDataReceived,
    handleTrackSubscribed,
    handleTrackUnsubscribed,
  ]);

  const toggleMic = useCallback(async () => {
    const room = roomRef.current;
    if (!room || connectionState !== "connected") return;
    const nextValue = !isMicEnabled;
    await room.localParticipant.setMicrophoneEnabled(nextValue);
    setIsMicEnabled(nextValue);
  }, [connectionState, isMicEnabled]);

  useEffect(() => {
    return () => {
      roomRef.current?.disconnect();
      cleanupAudioElements();
    };
  }, [cleanupAudioElements]);

  const isConnected = connectionState === "connected";

  return (
    <WebRTCGuard>
      <div className="flex h-screen max-w-2xl flex-col mx-auto px-4">
        <ClinicHeader />

        <div className="flex items-center justify-between gap-3 py-3 border-b border-gray-800">
          <div className="flex items-center gap-3">
            <span className="text-sm text-gray-400">Active agent:</span>
            <AgentStatusBadge state={agentState} />
          </div>
          <div className="text-right">
            <span className="block text-xs uppercase tracking-wide text-gray-500">
              LiveKit WebRTC · {activity}
            </span>
            {lastMetrics ? (
              <span className="block max-w-72 truncate text-[11px] text-gray-600">
                {lastMetrics}
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex justify-center py-6">
          <div
            className={`flex h-16 w-48 items-center justify-center gap-1 rounded-lg border border-gray-800 bg-gray-950 ${
              isConnected ? "opacity-100" : "opacity-55"
            }`}
            aria-hidden="true"
          >
            {[0, 1, 2, 3, 4].map((bar) => (
              <span
                key={bar}
                className={`w-2 rounded-full bg-blue-400 ${
                  isConnected ? "animate-pulse" : ""
                }`}
                style={{
                  height: `${18 + bar * 7}px`,
                  animationDelay: `${bar * 120}ms`,
                }}
              />
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-hidden">
          {error ? (
            <div className="rounded-lg border border-amber-800 bg-amber-950/40 px-4 py-3 text-sm text-amber-200">
              {error}
            </div>
          ) : (
            <TranscriptPane entries={transcript} />
          )}
        </div>

        <div className="flex items-center justify-between py-4 border-t border-gray-800">
          <button
            type="button"
            onClick={toggleMic}
            disabled={!isConnected}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
              isMicEnabled
                ? "bg-gray-800 text-gray-100 hover:bg-gray-700"
                : "bg-red-900 text-red-100 hover:bg-red-800"
            }`}
          >
            {isMicEnabled ? "Mic On" : "Mic Off"}
          </button>
          <ConnectButton
            transportState={connectionState}
            onConnect={connect}
            onDisconnect={disconnect}
          />
        </div>

        <div ref={audioContainerRef} className="hidden" />
      </div>
    </WebRTCGuard>
  );
}
