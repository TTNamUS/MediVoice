"use client";

import { useEffect, useRef } from "react";

interface Entry {
  role: "user" | "bot";
  text: string;
}

export function TranscriptPane({ entries }: { entries: Entry[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to latest message
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

  if (entries.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-gray-600 text-sm">
        Transcript will appear here when the call starts…
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto pr-1 space-y-3 py-2">
      {entries.map((entry, i) => (
        <div
          key={i}
          className={`flex ${entry.role === "user" ? "justify-end" : "justify-start"}`}
        >
          <div
            className={`max-w-[80%] rounded-2xl px-4 py-2 text-sm leading-relaxed ${
              entry.role === "user"
                ? "bg-blue-600 text-white rounded-br-sm"
                : "bg-gray-800 text-gray-100 rounded-bl-sm"
            }`}
          >
            <span className="block text-[10px] font-medium opacity-60 mb-0.5">
              {entry.role === "user" ? "You" : "MediVoice"}
            </span>
            {entry.text}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
