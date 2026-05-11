export function ClinicHeader() {
  return (
    <header className="flex items-center gap-3 py-5 border-b border-gray-800">
      {/* Simple tooth icon using SVG */}
      <svg
        className="w-8 h-8 text-blue-400 shrink-0"
        viewBox="0 0 24 24"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M12 2C9.5 2 7.5 3.5 6 5c-1.5 1.5-2 3-2 5 0 1.7.4 3.2 1 4.5.8 1.7 1 3.5 1 5.5h2c0-2 .3-4.2 1.2-6.2.4-.9.8-1.8.8-2.8 0-.6.4-1 1-1s1 .4 1 1c0 1 .4 1.9.8 2.8C13.7 15.8 14 18 14 20h2c0-2 .2-3.8 1-5.5.6-1.3 1-2.8 1-4.5 0-2-.5-3.5-2-5-1.5-1.5-3.5-3-4-3z" />
      </svg>
      <div>
        <h1 className="text-lg font-semibold leading-tight">Sunrise Dental Clinic</h1>
        <p className="text-xs text-gray-400">AI Voice Receptionist — powered by MediVoice</p>
      </div>
    </header>
  );
}
