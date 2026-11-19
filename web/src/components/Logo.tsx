/** The Rehearsal mark: a dotted draft (the rehearsal) behind a solid tile with a tick (the approved live change). */
export function Logo({ title }: { title?: string }) {
  return (
    <svg viewBox="0 0 48 48" role={title ? "img" : undefined} aria-hidden={title ? undefined : true} className="mark">
      {title && <title>{title}</title>}
      <rect x="5" y="5" width="26" height="26" rx="7" fill="none" stroke="var(--brand)" strokeWidth="3.4"
        strokeDasharray="1 5.6" strokeLinecap="round" />
      <rect x="14" y="14" width="30" height="30" rx="9" fill="var(--mark)" />
      <path d="m21.5 29.5 4.6 4.6 9.4-9.8" fill="none" stroke="var(--on-mark)" strokeWidth="3.6"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
