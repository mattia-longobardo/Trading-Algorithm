"use client";

import { ShieldAlertIcon } from "lucide-react";

import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { DotIndicator, Stamp } from "@/components/stamp";
import { ThemeToggle } from "@/components/theme-toggle";
import { useStatus } from "@/lib/queries";
import { useDisplay } from "@/lib/money";
import { fmtDateTime } from "@/lib/format";

/** Timbro di modalità: live acceso (denaro reale) o solo allenamento. */
export function LiveBadge({
  liveEnabled,
  className,
}: {
  liveEnabled: boolean;
  className?: string;
}) {
  return liveEnabled ? (
    <Stamp tone="solid-danger" className={className}>
      Live
    </Stamp>
  ) : (
    <Stamp tone="accent" className={className}>
      Allenamento
    </Stamp>
  );
}

/** Striscia strumenti: timbri di stato, indicatori safety, prossimo ciclo. */
export function SiteHeader({ userSlot }: { userSlot?: React.ReactNode }) {
  const { data: status, isLoading } = useStatus();
  const display = useDisplay();

  return (
    <header className="bg-background/95 supports-[backdrop-filter]:bg-background/85 sticky top-0 z-40 flex h-12 shrink-0 items-center gap-3 border-b px-4 backdrop-blur md:px-6">
      <SidebarTrigger className="text-muted-foreground -ml-1" />
      <Separator orientation="vertical" className="h-4" />
      <div className="flex items-center gap-1.5">
        {isLoading || !status ? (
          <Skeleton className="h-[18px] w-28" />
        ) : (
          <>
            <LiveBadge liveEnabled={status.arena.live_enabled} />
            {status.arena.paused && (
              <Stamp tone="caution">Training in pausa</Stamp>
            )}
            {status.market_open && (
              <Stamp tone="accent" className="gap-1.5">
                <span className="relative flex size-1.5">
                  <span className="bg-primary absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 motion-reduce:animate-none" />
                  <span className="bg-primary relative inline-flex size-1.5 rounded-full" />
                </span>
                {(status.open_sessions ?? [])
                  .map((s) => (s === "europe" ? "EU" : s === "usa" ? "USA" : s))
                  .join(" + ") || "Mercato"}{" "}
                aperto
              </Stamp>
            )}
          </>
        )}
      </div>
      <div className="ml-auto flex items-center gap-4">
        {status && (
          <div className="hidden items-center gap-4 md:flex">
            <DotIndicator
              label="Kill switch"
              active={status.kill_switch_active}
              activeTone="danger"
            />
            <DotIndicator
              label="Breaker"
              active={status.circuit_breaker.tripped}
              activeTone="caution"
            />
          </div>
        )}
        {status?.next_cycle_at && (
          <>
            <Separator orientation="vertical" className="hidden h-4 lg:block" />
            {/* Lo scheduling è in UTC, ma qui si legge nel fuso scelto: il
                title conserva l'orario UTC per chi deve confrontarlo coi log. */}
            <span
              className="text-muted-foreground hidden font-mono text-[11px] tracking-[0.08em] uppercase lg:inline"
              title={`${fmtDateTime(status.next_cycle_at, "UTC")} UTC`}
            >
              Prossimo ciclo{" "}
              <span className="text-foreground tabular-nums normal-case">
                {display.dateTime(status.next_cycle_at)}
              </span>{" "}
              {display.tzLabel}
            </span>
          </>
        )}
        <ThemeToggle />
        {userSlot}
      </div>
    </header>
  );
}

/** Banner rosso persistente quando il trading LIVE è attivo. */
export function RealEnvironmentBanner() {
  const { data: status } = useStatus();
  if (!status || !status.arena.live_enabled) return null;
  return (
    <div className="bg-negative flex items-center justify-center gap-2 px-4 py-1.5 text-center font-mono text-xs font-medium tracking-[0.08em] text-white uppercase dark:text-[#141517]">
      <ShieldAlertIcon className="size-3.5 shrink-0" />
      Trading live attivo — il campione muove denaro vero
    </div>
  );
}
