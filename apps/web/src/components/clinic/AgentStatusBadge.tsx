export type AgentState = "triage" | "booking" | "faq" | "billing";

const CONFIG: Record<AgentState, { label: string; color: string }> = {
  triage:  { label: "Triage",  color: "bg-slate-700 text-slate-200" },
  booking: { label: "Booking", color: "bg-blue-900  text-blue-200"  },
  faq:     { label: "FAQ",     color: "bg-green-900 text-green-200" },
  billing: { label: "Billing", color: "bg-amber-900 text-amber-200" },
};

export function AgentStatusBadge({ state }: { state: AgentState }) {
  const { label, color } = CONFIG[state] ?? CONFIG.triage;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${color}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
      {label}
    </span>
  );
}
