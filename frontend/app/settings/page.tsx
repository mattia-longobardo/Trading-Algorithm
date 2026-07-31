"use client";

import * as React from "react";
import { CoinsIcon, DnaIcon, KeyRoundIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/page-header";
import { Stamp } from "@/components/stamp";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  MobileField,
  MobileFields,
  MobileItem,
  MobileItemHeader,
  MobileList,
} from "@/components/mobile-list";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
import { SearchableSelect } from "@/components/ui/searchable-select";
import type { SearchableOption } from "@/components/ui/searchable-select";
import {
  useFxRates,
  useSettings,
  useSettingsAudit,
  useAccountCredentials,
  useUpdateCredentials,
  useUpdateSettings,
  usePortfolio,
} from "@/lib/queries";
import { fmtNum } from "@/lib/format";
import { useDisplay } from "@/lib/money";
import { timeZoneOptions } from "@/lib/timezones";
import type { AppSettings } from "@/lib/types";

function fmtAuditValue(v: unknown): string {
  if (v == null) return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function TimezoneCard({ settings }: { settings: AppSettings }) {
  const update = useUpdateSettings();
  const [timezone, setTimezone] = React.useState(settings.timezone);

  // ~450 fusi: la lista si costruisce una volta sola dal database tz del browser.
  const zones = React.useMemo(() => timeZoneOptions(), []);
  const dirty = timezone !== settings.timezone;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Fuso orario</CardTitle>
        <CardDescription>
          Solo presentazione: cicli, EOD e valutazione mensile girano sempre in
          UTC sugli orari di borsa USA.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-1.5">
          <Label htmlFor="timezone">Fuso orario di visualizzazione</Label>
          <SearchableSelect
            id="timezone"
            value={timezone}
            onValueChange={setTimezone}
            options={zones}
            placeholder="Seleziona un fuso"
            searchPlaceholder="Cerca fuso o città…"
            emptyText="Nessun fuso trovato"
          />
        </div>
        <Button
          disabled={!dirty || update.isPending}
          onClick={() => update.mutate({ timezone })}
        >
          {update.isPending ? "Salvataggio…" : "Salva"}
        </Button>
      </CardContent>
    </Card>
  );
}

function ArenaConfigCard({ settings }: { settings: AppSettings }) {
  const arena = settings.arena;
  const marketLabel = (name: string) =>
    name === "europe" ? "Borse europee" : name === "usa" ? "Borsa USA" : name;
  const rows: [string, string][] = [
    ["Budget mensile per agente", `${arena.starting_capital_eur ?? 10000} €`],
    ["Ciclo di trading", `ogni ${arena.cycle_minutes ?? 60} min`],
    ...Object.entries(arena.markets ?? {}).map(
      ([name, w]): [string, string] => [
        `${marketLabel(name)} (UTC)`,
        `${w?.open_utc ?? "?"} – ${w?.close_utc ?? "?"}`,
      ],
    ),
    ["Titoli per ciclo", String(arena.max_symbols ?? 24)],
    ["Mese in corso", arena.month ?? "—"],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <DnaIcon className="text-muted-foreground size-4" />
          Arena evolutiva
        </CardTitle>
        <CardDescription>
          Parametri dell&apos;allenamento (config/settings.yaml). Stop loss, take
          profit e size vivono nel DNA degli agenti e si evolvono da soli.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
        {rows.map(([label, value]) => (
          <div key={label}>
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
              {label}
            </p>
            <p className="mt-0.5 font-mono text-[13px] font-medium tabular-nums">
              {value}
            </p>
          </div>
        ))}
        <div className="col-span-2 flex items-center gap-2 pt-1">
          <span className="text-muted-foreground">Trading live</span>
          {arena.live_enabled ? (
            <Stamp tone="solid-danger">Attivo</Stamp>
          ) : (
            <Stamp tone="neutral">Spento</Stamp>
          )}
          <span className="text-muted-foreground">· Allenamento</span>
          {arena.paused ? (
            <Stamp tone="caution">In pausa</Stamp>
          ) : (
            <Stamp tone="approved">Attivo</Stamp>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function CurrencyCard({ settings }: { settings: AppSettings }) {
  const update = useUpdateSettings();
  const fx = useFxRates();
  const [currency, setCurrency] = React.useState(settings.currency);

  const options: SearchableOption[] = React.useMemo(() => {
    const rates = fx.data?.rates ?? {};
    const currencies = fx.data?.currencies ?? [{ code: "USD", label: "Dollaro USA" }];
    return currencies.map((option) => ({
      value: option.code,
      label: `${option.code} — ${option.label}`,
      hint:
        option.code === "USD" || !rates[option.code]
          ? undefined
          : fmtNum(rates[option.code], 4),
      keywords: option.label,
    }));
  }, [fx.data]);

  const rate = currency === "USD" ? 1 : fx.data?.rates?.[currency];
  const dirty = currency !== settings.currency;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CoinsIcon className="text-muted-foreground size-4" />
          Valuta
        </CardTitle>
        <CardDescription>
          eToro opera in dollari: importi e storico restano registrati in USD e
          vengono convertiti solo per la visualizzazione, ai cambi BCE.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-1.5">
          <Label htmlFor="currency">Valuta di visualizzazione</Label>
          <SearchableSelect
            id="currency"
            value={currency}
            onValueChange={setCurrency}
            options={options}
            placeholder="Seleziona una valuta"
            searchPlaceholder="Cerca valuta…"
            emptyText="Nessuna valuta trovata"
            disabled={fx.isLoading}
          />
        </div>

        <div className="border-primary/30 bg-primary/5 rounded-md border p-3">
          <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
            Cambio applicato
          </p>
          <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
            {currency === "USD"
              ? "1 USD = 1 USD"
              : rate
                ? `1 USD = ${fmtNum(rate, 4)} ${currency}`
                : "n/d"}
          </p>
          <p className="text-muted-foreground mt-1 text-xs">
            {fx.data?.fetched_at
              ? `Aggiornato il ${new Date(fx.data.fetched_at).toLocaleString("it-IT")}`
              : "Cambio non ancora recuperato"}
            {fx.data?.stale ? " — non aggiornato, importi mostrati in USD" : ""}
          </p>
        </div>

        <Button
          disabled={!dirty || update.isPending}
          onClick={() => update.mutate({ currency })}
        >
          {update.isPending ? "Salvataggio…" : "Salva valuta"}
        </Button>
      </CardContent>
    </Card>
  );
}

function KeysCard() {
  const account = useAccountCredentials();
  const update = useUpdateCredentials();
  const [etoroApiKey, setEtoroApiKey] = React.useState("");
  const [etoroUserKey, setEtoroUserKey] = React.useState("");
  const [openaiApiKey, setOpenaiApiKey] = React.useState("");

  const save = () => {
    const body: { etoro_api_key?: string; etoro_user_key?: string; openai_api_key?: string } = {};
    if (etoroApiKey) body.etoro_api_key = etoroApiKey;
    if (etoroUserKey) body.etoro_user_key = etoroUserKey;
    if (openaiApiKey) body.openai_api_key = openaiApiKey;
    update.mutate(body, { onSuccess: () => { setEtoroApiKey(""); setEtoroUserKey(""); setOpenaiApiKey(""); } });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRoundIcon className="text-muted-foreground size-4" />
          Chiavi API personali
        </CardTitle>
        <CardDescription>
          Credenziali del conto eToro REALE e di OpenAI, cifrate sul server e
          associate al tuo account Authentik. L&apos;allenamento usa solo i dati di
          mercato; gli ordini partono unicamente col live attivo.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5 text-sm">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">API eToro</span>
            {account.data?.etoro_api_key_configured ? (
              <Stamp tone="approved">Configurate</Stamp>
            ) : (
              <Stamp tone="rejected">Mancanti</Stamp>
            )}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">User eToro</span>
            {account.data?.etoro_user_key_configured ? <Stamp tone="approved">Configurata</Stamp> : <Stamp tone="rejected">Mancante</Stamp>}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">OpenAI</span>
            {account.data?.openai_api_key_configured ? (
              <Stamp tone="approved">Configurata</Stamp>
            ) : (
              <Stamp tone="rejected">Mancante</Stamp>
            )}
          </div>
        </div>
        <div className="grid gap-3">
          <div className="grid gap-1.5"><Label htmlFor="etoro-api-key">API key eToro</Label><Input id="etoro-api-key" type="password" autoComplete="off" value={etoroApiKey} onChange={(event) => setEtoroApiKey(event.target.value)} placeholder={account.data?.etoro_api_key_configured ? "Configurata — inserisci per sostituire" : "Inserisci API key"} /></div>
          <div className="grid gap-1.5"><Label htmlFor="etoro-user-key">User key eToro</Label><Input id="etoro-user-key" type="password" autoComplete="off" value={etoroUserKey} onChange={(event) => setEtoroUserKey(event.target.value)} placeholder={account.data?.etoro_user_key_configured ? "Configurata — inserisci per sostituire" : "Inserisci user key"} /></div>
          <div className="grid gap-1.5"><Label htmlFor="openai-api-key">API key OpenAI</Label><Input id="openai-api-key" type="password" autoComplete="off" value={openaiApiKey} onChange={(event) => setOpenaiApiKey(event.target.value)} placeholder={account.data?.openai_api_key_configured ? "Configurata — inserisci per sostituire" : "Inserisci API key"} /></div>
          <Button disabled={update.isPending || (!etoroApiKey && !etoroUserKey && !openaiApiKey)} onClick={save}>Salva chiavi personali</Button>
        </div>
      </CardContent>
    </Card>
  );
}

function AuditCard() {
  const { data, isLoading, error } = useSettingsAudit();
  const display = useDisplay();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit log</CardTitle>
        <CardDescription>
          Ogni cambio di impostazioni è registrato con valore precedente e nuovo
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={5} />
        ) : error || !data ? (
          <ErrorState error={error} />
        ) : data.entries.length === 0 ? (
          <p className="text-muted-foreground py-8 text-center text-sm">
            Nessuna modifica registrata
          </p>
        ) : (
          <>
          <div className="max-md:hidden">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Data</TableHead>
                <TableHead>Chiave</TableHead>
                <TableHead>Da</TableHead>
                <TableHead>A</TableHead>
                <TableHead>Origine</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.entries.map((e) => (
                <TableRow key={String(e.id)}>
                  <TableCell className="font-mono text-[13px] whitespace-nowrap tabular-nums">
                    {display.dateTime(e.changed_at)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{e.key}</TableCell>
                  <TableCell className="text-muted-foreground max-w-48 truncate font-mono text-xs">
                    {fmtAuditValue(e.old_value)}
                  </TableCell>
                  <TableCell className="max-w-48 truncate font-mono text-xs">
                    {fmtAuditValue(e.new_value)}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {e.source}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          </div>
          <MobileList>
            {data.entries.map((e) => (
              <MobileItem key={String(e.id)}>
                <MobileItemHeader>
                  <span className="font-mono text-xs font-medium">{e.key}</span>
                  <span className="text-muted-foreground text-xs">{e.source}</span>
                </MobileItemHeader>
                <MobileFields>
                  <MobileField label="Da">
                    <span className="text-muted-foreground font-mono text-xs break-all">
                      {fmtAuditValue(e.old_value)}
                    </span>
                  </MobileField>
                  <MobileField label="A">
                    <span className="font-mono text-xs break-all">
                      {fmtAuditValue(e.new_value)}
                    </span>
                  </MobileField>
                  <MobileField label="Data" wide>
                    <span className="font-mono text-xs tabular-nums">
                      {display.dateTime(e.changed_at)}
                    </span>
                  </MobileField>
                </MobileFields>
              </MobileItem>
            ))}
          </MobileList>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function SettingsPage() {
  const { data: settings, isLoading, error } = useSettings();
  const portfolio = usePortfolio();
  const display = useDisplay();

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Configurazione"
        title="Impostazioni"
        description={
          <>
            Persistite in app_settings su Postgres. Capitale disponibile
            rilevato direttamente da eToro:{" "}
            <span className="text-foreground font-mono tabular-nums">
              {display.money(portfolio.data?.cash_usd)}
            </span>
          </>
        }
      />

      {isLoading ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <CardSkeleton className="h-80 w-full" />
          <CardSkeleton className="h-80 w-full" />
        </div>
      ) : error || !settings ? (
        <ErrorState error={error} />
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <ArenaConfigCard settings={settings} />
          <KeysCard />
          {/* key = valori server: il form si riallinea quando cambiano lato backend */}
          <TimezoneCard key={settings.timezone} settings={settings} />
          <CurrencyCard key={settings.currency} settings={settings} />
          <AuditCard />
        </div>
      )}
    </div>
  );
}
