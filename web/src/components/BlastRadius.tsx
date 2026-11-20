// The blast radius: who a task's changes touch. One task normally concerns one person; an attack
// lights up a ring of strangers.

import type { Review } from "../api";

interface Props {
  review: Review;
  /** The person the request is about (the ticket's customer), when there is one. */
  homeId: number | null;
  homeLabel: string;
}

const W = 520;
const H = 300;
const CX = W / 2;
const CY = H / 2;

function first(name: string, id: string | number): string {
  return name ? name.split(/[\s@]/)[0]! : `#${id}`;
}

export function BlastRadius({ review, homeId, homeLabel }: Props) {
  const people = review.blast ?? [];
  const others = people.filter((p) => p.id !== homeId);
  if (people.length < 2 || others.length === 0) return null;
  const maxMoney = Math.max(1, ...people.map((p) => p.money_cents));
  const radius = (cents: number) => 9 + 9 * Math.sqrt(cents / maxMoney);
  const ring = Math.min(CY - 34, 70 + others.length * 4);
  const placed = others.map((p, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / others.length;
    return { ...p, x: CX + Math.cos(angle) * ring * 1.6, y: CY + Math.sin(angle) * ring };
  });
  const paidOthers = others.filter((p) => p.money_cents > 0).length;
  const label = `${others.length} ${others.length === 1 ? "person" : "people"} besides ${homeLabel} would be ` +
    `affected${paidOthers ? `, and ${paidOthers} of them would receive money` : ""}.`;

  return (
    <figure className="blast">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
        {placed.map((p) => (
          <line key={`l-${p.id}`} x1={CX} y1={CY} x2={p.x} y2={p.y} className="blast-edge" />
        ))}
        <circle cx={CX} cy={CY} r={24} className="blast-home" />
        <text x={CX} y={CY + 4} textAnchor="middle" className="blast-home-text">{homeLabel}</text>
        {placed.map((p) => (
          <g key={p.id}>
            <circle cx={p.x} cy={p.y} r={radius(p.money_cents)} className="blast-other" />
            <text x={p.x} y={p.y + radius(p.money_cents) + 13} textAnchor="middle" className="blast-name">
              {first(p.name, p.id)}
            </text>
          </g>
        ))}
      </svg>
      <figcaption>
        <strong>Who this touches.</strong> {label} A support task normally concerns one person. Bigger circles
        mean more money.
      </figcaption>
    </figure>
  );
}
