"use client";

import { Stamp } from "@/components/stamp";
import type { ExecutionStatus } from "@/lib/types";

const EXECUTION_STATUS_META: Record<
  ExecutionStatus,
  { label: string; tone: "approved" | "rejected" | "neutral" | "caution" }
> = {
  pending: { label: "Da aprire", tone: "caution" },
  filled: { label: "Eseguito", tone: "approved" },
  failed: { label: "Fallito", tone: "rejected" },
  skipped: { label: "Skipped", tone: "neutral" },
  rejected: { label: "Respinto", tone: "caution" },
  cancelled: { label: "Annullato", tone: "neutral" },
};

export function ExecutionStatusBadge({ status }: { status: ExecutionStatus }) {
  const meta = EXECUTION_STATUS_META[status] ?? {
    label: status,
    tone: "neutral" as const,
  };
  return <Stamp tone={meta.tone}>{meta.label}</Stamp>;
}

export function SideBadge({ side }: { side: "buy" | "sell" }) {
  return side === "buy" ? (
    <Stamp tone="approved">Buy</Stamp>
  ) : (
    <Stamp tone="rejected">Sell</Stamp>
  );
}
