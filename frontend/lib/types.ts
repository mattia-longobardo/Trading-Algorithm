// Tipi del contratto API backend (FastAPI) — tutte le rotte passano da /api/*

export interface CircuitBreakerStatus {
  tripped: boolean;
  reason: string | null;
  until: string | null;
}

export interface ArenaStateInfo {
  month: string | null;
  paused: boolean;
  live_enabled: boolean;
}

export interface ChampionRef {
  id: string;
  name: string;
  generation: number;
}

export interface Status {
  kill_switch_active: boolean;
  circuit_breaker: CircuitBreakerStatus;
  arena: ArenaStateInfo;
  champion: ChampionRef | null;
  market_open: boolean;
  /** Sessioni di borsa aperte adesso: "europe" | "usa". */
  open_sessions: string[];
  next_cycle_at: string | null;
  equity_usd: number | null;
  equity_change_day_pct: number | null;
}

export type ExecutionStatus =
  | "pending"
  | "filled"
  | "failed"
  | "skipped"
  | "rejected"
  | "cancelled";

export interface Execution {
  id: string;
  run_id: string;
  symbol: string;
  side: "buy" | "sell";
  amount_usd: number;
  status: ExecutionStatus;
  detail: string | null;
  execution_price: number | null;
  etoro_position_id: string | number | null;
  created_at: string;
}

export interface ExecutionsResponse {
  executions: Execution[];
}

export interface Position {
  etoro_position_id: string | number;
  symbol: string;
  instrument_id: number;
  amount_usd: number;
  direction: TradeDirection;
  entry_price: number;
  current_price: number | null;
  unrealized_pnl_usd: number | null;
  unrealized_pnl_pct: number | null;
  sector: string | null;
  opened_at: string;
}

export interface Anomaly {
  symbol: string;
  detail: string;
  detected_at: string;
}

export interface Portfolio {
  positions: Position[];
  cash_usd: number;
  equity_usd: number;
  exposure_usd: number;
  anomalies: Anomaly[];
  max_trade_amount_usd: number;
  capital_source: "etoro";
}

export interface BacktestMetrics {
  total_return_pct: number | null;
  cagr_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown_pct: number | null;
  calmar: number | null;
  alpha: number | null;
  beta: number | null;
  information_ratio: number | null;
  win_rate_pct: number | null;
  profit_factor: number | null;
  recovery_factor: number | null;
  expectancy_usd: number | null;
  exposure_pct: number | null;
  max_win_usd: number | null;
  max_loss_usd: number | null;
  std_win_usd: number | null;
  std_loss_usd: number | null;
}

export interface DateRangeValue {
  from: string;
  to: string;
}

export interface BacktestSummary {
  metrics: BacktestMetrics;
  n_closed_trades: number;
  n_days: number;
  insufficient_sample: boolean;
  annualization_available: boolean;
  risk_free_rate_pct: number | null;
}

export interface EquityPoint {
  date: string;
  equity_usd: number;
  spy_lump_sum_usd: number | null;
  spy_cash_flow_matched_usd: number | null;
}

export interface EquityCurve {
  points: EquityPoint[];
  note_dividends: string;
}

export interface ClosedTrade {
  etoro_position_id: string | number;
  symbol: string;
  amount_usd: number;
  entry_price: number;
  close_price: number | null;
  realized_pnl_usd: number | null;
  opened_at: string;
  closed_at: string | null;
  close_reason: string | null;
  sector: string | null;
}

export interface TradesResponse {
  trades: ClosedTrade[];
}

export interface MonthlyRow {
  year: number;
  months: (number | null)[];
}

export interface MonthlyReturns {
  rows: MonthlyRow[];
}

export interface IngestResult {
  filename: string;
  chunks_indexed: number;
  /** Titoli impattati, dedotti dal contenuto del documento. */
  tickers: string[];
}

export interface KnowledgeStatus {
  arango_up?: boolean;
  qdrant_up?: boolean;
  graph?: string | null;
  search_view?: string | null;
  collections: {
    news_kb?: number;
    trade_memory?: number;
    market_nodes?: number;
    market_edges?: number;
    [key: string]: number | undefined;
  };
  rss_feeds: string[];
  last_fetch: string | null;
}

export interface ArenaConfigInfo extends ArenaStateInfo {
  starting_capital_usd?: number;
  cycle_minutes?: number;
  max_symbols?: number;
  markets?: Record<string, { open_utc?: string; close_utc?: string }>;
}

export interface AppSettings {
  /** Fuso di sola presentazione: non sposta gli orari di esecuzione (UTC). */
  timezone: string;
  /** Valuta di sola presentazione: i conti restano in USD. */
  currency: string;
  arena: ArenaConfigInfo;
  api_keys_configured: boolean;
  openai_configured: boolean;
}

export interface SettingsUpdate {
  timezone?: string;
  currency?: string;
}

export interface CurrencyOption {
  code: string;
  label: string;
}

/** Tassi USD→valuta serviti da /fx/rates (fonte BCE via Frankfurter). */
export interface FxRates {
  base: string;
  rates: Record<string, number>;
  fetched_at: string | null;
  /** true = tasso non aggiornato (rete giù): la UI lo segnala invece di mentire. */
  stale: boolean;
  source: string;
  currencies: CurrencyOption[];
}

export interface AuditEntry {
  id: string | number;
  changed_at: string;
  key: string;
  old_value: unknown;
  new_value: unknown;
  source: string;
}

export interface AuditResponse {
  entries: AuditEntry[];
}

export interface AccountCredentials {
  user_id: string;
  email: string | null;
  display_name: string | null;
  etoro_api_key_configured: boolean;
  etoro_user_key_configured: boolean;
  openai_api_key_configured: boolean;
}

export interface TradeItem {
  id: string;
  position_id: string | number | null;
  execution_id: string | null;
  symbol: string;
  side: "buy" | "sell";
  status: string;
  amount_usd: number;
  entry_price: number | null;
  current_price: number | null;
  pnl_usd: number | null;
  created_at: string;
  detail: string | null;
  can_close: boolean;
  can_cancel: boolean;
}

export interface TradeHistoryItem {
  id: string;
  symbol: string;
  side: "buy" | "sell";
  status: string;
  amount_usd: number;
  price: number | null;
  pnl_usd: number | null;
  opened_at: string;
  closed_at: string | null;
  detail: string | null;
}

export interface NewsItem {
  text: string;
  source: string;
  tickers: string[];
  published_at: string;
  url?: string;
}

// --- Arena evolutiva ---------------------------------------------------------

export type AgentStatus = "alive" | "dead" | "evolved";

/** Verso di un trade o di una posizione aperta. */
export type TradeDirection = "long" | "short";

/** Aggregato per verso (long/short) dentro le metriche di un agente. */
export interface DirectionSplit {
  n: number;
  pnl_usd: number;
  /** null se non ci sono trade in quel verso. */
  win_rate_pct: number | null;
}

/**
 * Metriche di performance del conto simulato di un agente.
 * I campi nullable lo sono quando il campione è troppo piccolo per calcolarli.
 */
export interface AgentMetrics {
  pnl_usd: number;
  /** null se il capitale iniziale è <= 0. */
  return_pct: number | null;
  equity_usd: number;
  exposure_pct: number;
  cash_pct: number;
  n_trades: number;
  n_wins: number;
  n_losses: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  expectancy_usd: number | null;
  avg_win_usd: number | null;
  /** Valore negativo. */
  avg_loss_usd: number | null;
  best_trade_usd: number | null;
  worst_trade_usd: number | null;
  /** Negativo o 0; null sotto i 2 punti di equity. */
  max_drawdown_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  avg_holding_hours: number | null;
  trades_per_day: number | null;
  long: DirectionSplit;
  short: DirectionSplit;
  /** true se i trade o i punti giornalieri sono troppo pochi: metriche indicative. */
  insufficient_sample: boolean;
}

export interface AgentDna {
  conviction_scale: number;
  max_positions: number;
  max_orders_per_cycle: number;
  max_position_pct: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  min_cash_pct: number;
  /** Giorni di borsa massimi per posizione: 1 = day trading, fino a 5 = swing. */
  max_holding_days?: number;
  risk_profile: string;
  strategy: string;
  [key: string]: unknown;
}

export interface AgentSimPosition {
  id: string;
  symbol: string;
  amount_usd: number;
  entry_price: number;
  opened_at: string;
  open_reason: string;
  direction: TradeDirection;
}

export interface ArenaAgent {
  id: string;
  name: string;
  generation: number;
  status: AgentStatus;
  is_champion: boolean;
  parent_id: string | null;
  born_at: string | null;
  died_at: string | null;
  death_reason: string | null;
  month: string;
  dna: AgentDna;
  memory?: string;
  starting_capital_usd: number;
  cash_usd: number;
  /** Somma delle posizioni aperte; assente sui payload più vecchi. */
  invested_usd?: number;
  equity_usd: number;
  pnl_month_usd: number;
  open_positions: AgentSimPosition[];
}

export interface ArenaSession {
  name: string;
  open_utc: string;
  close_utc: string;
  open_now: boolean;
}

export interface ArenaOverview {
  state: ArenaStateInfo;
  market_open: boolean;
  sessions: ArenaSession[];
  next_cycle_at: string | null;
  days_to_evaluation: number;
  generation: number;
  agents: ArenaAgent[];
  champion: ArenaAgent | null;
  lineage: ArenaAgent[];
}

export interface AgentEquityPoint {
  ts: string;
  equity_usd: number;
}

export interface AgentSimTrade {
  id: string;
  symbol: string;
  amount_usd: number;
  entry_price: number;
  close_price: number;
  pnl_usd: number;
  opened_at: string;
  closed_at: string;
  open_reason: string;
  close_reason: string;
  direction: TradeDirection;
  /** Durata della posizione in ore (1 decimale). */
  holding_hours: number;
  /** pnl_usd / amount_usd * 100; null se amount_usd è 0. */
  return_pct: number | null;
}

export interface AgentDetail {
  agent: ArenaAgent;
  equity: AgentEquityPoint[];
  trades: AgentSimTrade[];
  metrics: AgentMetrics;
}

export interface ArenaEventItem {
  id: string;
  ts: string;
  event: string;
  payload: Record<string, unknown>;
}

export interface ArenaEventsResponse {
  events: ArenaEventItem[];
}

export interface ArenaStateResponse {
  state: ArenaStateInfo;
}
