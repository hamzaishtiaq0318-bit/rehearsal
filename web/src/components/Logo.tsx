/** Two frames: the dashed one is the rehearsal, the solid one is live data that passed review. */
export function Logo({ title }: { title?: string }) {
  return (
    <svg viewBox="0 0 32 32" role={title ? "img" : undefined} aria-hidden={title ? undefined : true}>
      {title && <title>{title}</title>}
      <rect x="3.5" y="3.5" width="17" height="17" rx="4" fill="none" stroke="var(--brand)" strokeWidth="2"
        strokeDasharray="3.2 2.6" />
      <rect x="10" y="10" width="19" height="19" rx="4.5" fill="var(--brand)" />
      <path d="m14.6 19.7 3.2 3.2 6.3-6.6" fill="none" stroke="var(--on-brand)" strokeWidth="2.4"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
