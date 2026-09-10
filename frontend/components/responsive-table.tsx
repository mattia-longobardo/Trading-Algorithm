import * as React from "react";

import {
  MobileField,
  MobileFields,
  MobileItem,
  MobileItemHeader,
  MobileList,
} from "@/components/mobile-list";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

/**
 * Una colonna descritta una volta sola: intestazione, cella desktop e — se
 * serve — la sua resa nella scheda mobile.
 *
 * `mobile` assente = la colonna non compare su mobile (di solito perché il suo
 * contenuto è già nell'intestazione della scheda). `mobile.render` esiste
 * perché sotto `md` la stessa informazione va spesso scritta più corta: senza,
 * si riusa la cella desktop.
 */
export interface ResponsiveColumn<T> {
  key: string;
  header?: React.ReactNode;
  cell: (row: T) => React.ReactNode;
  /** Classi della cella desktop (allineamento, font, larghezze). */
  className?: string;
  /** Classi dell'intestazione desktop. */
  headerClassName?: string;
  mobile?: {
    label: string;
    render?: (row: T) => React.ReactNode;
    /** Occupa l'intera riga della griglia mobile. */
    wide?: boolean;
    /** Posizione nella scheda quando non coincide con l'ordine delle colonne. */
    order?: number;
  };
}

/**
 * Registro tabellare con vista desktop e schede mobili dalla stessa
 * definizione di colonne. Entrambe restano nel DOM e la CSS sceglie quale
 * mostrare (come faceva ogni pagina a mano prima di questo componente).
 *
 * `mobileHeader` è un render prop e non una proiezione automatica delle
 * colonne: la prima riga della scheda mette insieme campi diversi (simbolo +
 * lato + PnL + stato) e non corrisponde a nessuna colonna singola.
 */
export function ResponsiveTable<T>({
  rows,
  columns,
  rowKey,
  mobileHeader,
  mobileFooter,
  tableClassName,
  rowClassName,
}: {
  rows: T[];
  columns: ResponsiveColumn<T>[];
  rowKey: (row: T) => string;
  mobileHeader?: (row: T) => React.ReactNode;
  /** Coda della scheda mobile (azioni della riga), sotto i campi. */
  mobileFooter?: (row: T) => React.ReactNode;
  tableClassName?: string;
  rowClassName?: (row: T) => string | undefined;
}) {
  const mobileColumns = columns
    .filter((column) => column.mobile)
    .sort((a, b) => (a.mobile!.order ?? 0) - (b.mobile!.order ?? 0));
  return (
    <>
      <div className="max-md:hidden">
        <Table className={tableClassName}>
          <TableHeader>
            <TableRow>
              {columns.map((column) => (
                <TableHead key={column.key} className={column.headerClassName}>
                  {column.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={rowKey(row)} className={rowClassName?.(row)}>
                {columns.map((column) => (
                  <TableCell key={column.key} className={column.className}>
                    {column.cell(row)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <MobileList>
        {rows.map((row) => (
          <MobileItem key={rowKey(row)} className={cn(rowClassName?.(row))}>
            {mobileHeader && <MobileItemHeader>{mobileHeader(row)}</MobileItemHeader>}
            {mobileColumns.length > 0 && (
              <MobileFields>
                {mobileColumns.map((column) => {
                  const value = (column.mobile!.render ?? column.cell)(row);
                  // valore nullo = campo assente per QUESTA riga (un dettaglio
                  // vuoto non merita un'etichetta a vuoto sulla scheda)
                  if (value == null) return null;
                  return (
                    <MobileField
                      key={column.key}
                      label={column.mobile!.label}
                      wide={column.mobile!.wide}
                    >
                      {value}
                    </MobileField>
                  );
                })}
              </MobileFields>
            )}
            {mobileFooter?.(row)}
          </MobileItem>
        ))}
      </MobileList>
    </>
  );
}
