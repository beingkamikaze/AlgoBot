import { supabase } from "@/lib/supabase";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8100";

export type Health = {
  status: string;
  mode: string;
  supabase_configured: boolean;
  market: {
    now_ist: string;
    is_open: boolean;
    phase: "closed" | "pre-open" | "open" | "post-close";
    reason: string | null;
    next_open: string;
    calendar_known: boolean;
  };
};

export type Bar = { t: number; o: number; h: number; l: number; c: number; v: number; complete: boolean };

export type IndicatorSeries = {
  id: string;
  label: string;
  kind: "line" | "histogram" | "band-upper" | "band-lower" | "step";
  placement: string; // "overlay" or "pane:rsi" | "pane:macd" | "pane:atr"
  values: (number | null)[];
  colors: ("up" | "down" | null)[] | null;
};

export type ChartData = {
  symbol: string;
  name: string | null;
  tf: string;
  range: string;
  source: string;               // "kite_live" | "kite" | "yahoo" (XAUUSD: its own providers)
  source_label?: string;
  delayed: boolean;
  prev_close: number | null;
  last_price: number | null;
  as_of: number | null;
  bars: Bar[];
  indicators: IndicatorSeries[];
  notes: string[];
};

export type GlobalChartData = ChartData & { price_source: "spot" | "futures" };

export type GlobalMarketStatus = {
  is_open: boolean;
  phase: "open" | "daily-break" | "early-close" | "weekend" | "holiday";
  session: "asia" | "london" | "new_york" | null;
  reason: string | null;
  trading_day: string;
  next_open: string;
  next_close: string;
  now_ist: string;
};

export type GlobalDataStatus = {
  history: {
    days_with_bars: number;
    trading_days_with_bars: number;
    first_day: string | null;
    last_day: string | null;
    gap_days: number;
    gap_examples: string[];
    days_left: number;
    start_target: string;
  };
  download: { enabled: boolean; running: boolean; last_error: string | null; backoff_seconds: number; current_day: string | null };
  live: { twelvedata_configured: boolean; calls_today: number; daily_limit: number };
  default_source: "spot" | "futures";
};

export type GlobalDataCheck = {
  ok: boolean | null;
  common_bars: number;
  detail: string;
  move_correlation?: number;
  median_gap?: number;
  gap_swing?: number;
};

export type GlobalBreakdown = { trades: number; win_rate: number | null; expectancy_r: number | null; profit_factor: number | null; net_pnl: number | null };

export type GlobalBacktestSummary = GlobalBreakdown & { costs?: number; max_drawdown?: number };

export type GlobalBacktestReport = {
  strategy: string;
  data_kind: string;
  sessions_in_data: number;
  first_session: string;
  last_session: string;
  ran_at: number;
  summary: GlobalBacktestSummary;
  double_slippage: GlobalBacktestSummary;
  verdict: { text: string; gates: { min_trades: number } };
  by_side: Record<string, GlobalBreakdown>;
  by_market_session: Record<string, GlobalBreakdown>;
};

export type GlobalBacktestOverview = {
  held_back_from: string;
  research_start: string;
  research_days_stored: number;
  research_days_possible: number;
  strategies: { id: string; name: string; description: string }[];
  reports: Record<string, GlobalBacktestReport>;
};

export type Quote = { symbol: string; last: number; prev_close: number | null; change_pct: number | null; as_of: number | null };

export type SearchHit = { symbol: string; name: string; kind: "index" | "equity" };

export type Me = { id: string; email: string | null; mode: string; paper_starting_capital: number };

/** Calls the Python backend, sending the Supabase session token when there is one. */
export type GlobalVerdict = {
  verdict: "PASS" | "BLOCK"; reasons: string[]; warnings: string[]; hard: string[]; overridable: boolean; overridden?: boolean;
  numbers: { units?: number; value?: number; risk_per_unit?: number; max_loss?: number; costs_at_stop?: number; reward_risk?: number };
};

export type GlobalOrder = {
  id: string; symbol: string; side: "buy" | "sell"; lots: number; leg: "entry" | "exit"; trade_id: string | null;
  state: "proposed" | "working" | "filled" | "expired" | "rejected" | "cancelled";
  params: {
    units: number; limit?: number; stop?: number; target?: number | null; source: "spot" | "futures"; last_price?: number;
    price_age_seconds?: number; spread?: number; slippage?: number; why?: string; reason?: string; expired_because?: string;
    override?: { reasons: string[]; note: string }; card?: GlobalCard;
  };
  risk_verdict: GlobalVerdict | null; approved_at: string | null; created_at: string;
};

export type GlobalTrade = {
  id: string; symbol: string; side: "buy" | "sell"; lots: number; units: number; price_source: "spot" | "futures";
  entry_ts: string | null; exit_ts: string | null; entry: number | null; exit: number | null;
  gross_pnl: number | null; costs: number | null; net_pnl: number | null; r_multiple: number | null; exit_reason: string | null;
  rules_followed: boolean | null; trading_day: string | null;
  entry_context: { stop?: number; target?: number | null; why?: string; override?: unknown; card?: GlobalCard } | null;
  last?: number | null; price_age_seconds?: number | null; unrealized?: number | null; stop?: number; target?: number | null;
};

export type GlobalSignal = {
  id: string; symbol: string; strategy_id: string; strategy_version: string; side: "buy" | "sell"; timeframe: string;
  signal_ts: string; price_source: "spot" | "futures"; ref_price: number; stop: number; target_r: number;
  state: "new" | "expired"; expires_at: string; created_at: string; your_ticket?: GlobalOrder["state"] | null;
  explanation: {
    rule: string; conditions: { name: string; met: boolean; detail: string }[]; invalidation: string; stop_reason: string;
    target_reason: string; confidence: string; verdict: string | null; age_when_seen_seconds: number; card: GlobalCard;
  };
};

export type GlobalSignals = {
  signals: GlobalSignal[]; scanner: { at?: number; found?: number; expired?: number; error?: string }; market_open: boolean;
  strategies: { id: string; name: string; description: string }[];
};

export type AgentGroup = { ideas: number; judged: number; avg_r?: number; win_rate?: number; total_r?: number };
export type AgentIdea = {
  id: string; symbol: string; kind: "trade" | "pass"; side: "buy" | "sell" | null; setup: string | null;
  price_source: "spot" | "futures"; ref_price: number | null; stop: number | null; target: number | null; triggers: string[];
  proposal: {
    decision: "trade" | "pass"; side: string; setup: string; stop: number; target: number; why: string[]; wrong_if: string;
    pass_reason: string; plan?: { entry: number; stop: number; target: number; risk: number; reward: number; reward_risk: number | null };
  };
  review: { by?: "rules" | "reviewer"; verdict?: "ok" | "caution" | "reject"; concerns?: string[]; summary?: string };
  state: "new" | "vetoed" | "expired" | "passed"; expires_at: string | null; created_at: string; model: string | null;
  outcome: { status: string; exit?: number; reason?: string; r?: number; exit_at?: string; note?: string } | null;
  your_ticket?: GlobalOrder["state"] | null; facts?: Record<string, unknown>;
};
export type AgentDesk = {
  ideas: AgentIdea[]; market_open: boolean; in_window: boolean; enabled: boolean; ai_ready: boolean; looks_per_day: number;
  cooldown_minutes: number; idea_minutes: number;
  state: { status: string; error: string | null; last_look: string | null; looks_today: number };
  record: { looks: number; all: AgentGroup; passed_checks: AgentGroup; vetoed: AgentGroup; you_took: AgentGroup; note: string };
};

export type TelegramStatus = {
  configured: boolean; bot_username: string | null; linked: boolean; username: string | null; linked_at: string | null;
  loop: string | null; error: string | null;
};
export type TelegramCode = { code: string; expires_at: string; bot_username: string | null; link: string | null };

export type GlobalEvent = { name: string; at: string; ist: string; new_york: string; note: string; minutes_away?: number };
export type GlobalHeadline = { title: string; source: string; time: string | null; ist?: string | null; link?: string };

/** The "why this trade" card: built by code from facts only. */
export type GlobalCard = {
  title: string; source: string; why: string; summary?: string; error?: string;
  plan?: { entry: number; stop: number; target: number | null; risk_per_oz: number; reward_per_oz: number | null;
    reward_risk: number | null; lots: number; ounces: number; max_loss: number | null; flat_by_ist: string | null };
  context?: string[]; typical_5m_move?: number | null; events?: GlobalEvent[]; headlines?: GlobalHeadline[]; risks?: string[];
  confidence?: { grade: string; why: string }; prices_from?: string; made_at?: string;
};

export type GlobalJournal = {
  trade_id: string; ai_postmortem: string | null; mistake_tags: string[]; lessons: string | null; user_notes: string | null;
  ai_model: string | null; updated_at: string;
};

export type GlobalReview = {
  id: string; kind: "daily" | "weekly"; period_start: string; period_end: string; model: string | null; created_at: string;
  stats: { trades: number; net_pnl_dollars?: number; average_R?: number | null; rules_followed_pct?: number };
  body: { headline: string; what_happened: string; discipline: string; patterns: string[]; next_time: string[] };
};

export type GlobalNews = { symbol: string; headlines: GlobalHeadline[]; error: string | null; fetched_at: string; source: string };
export type GlobalMacro = { events: GlobalEvent[]; near: string[]; covered_until: string; sources: string[] };

export type GlobalLimits = {
  max_loss_per_trade: number; max_daily_loss: number; max_monthly_loss: number; max_trades_per_day: number;
  max_consecutive_losses: number; max_open_positions: number; max_position_value: number;
  entry_start_london: string; entry_end_new_york: string; flat_by_new_york: string;
};

export type GlobalPaperOverview = {
  account: { currency: string; starting_capital: number; capital: number; realized_today: number; realized_month: number;
    open_risk: number; entries_today: number; consecutive_losses: number; kill_switch: boolean };
  limits: GlobalLimits;
  window: { entry_start: string; entry_end: string; flat_by: string } | null;
  market: { is_open: boolean; phase: string; reason: string | null; session: string | null; trading_day: string };
  price_source: "spot" | "futures";
  price: { symbol: string; last: number | null; age_seconds: number | null };
  assumed: { spread: number; slippage: number };
  positions: GlobalTrade[];
  orders: GlobalOrder[];
  trades: GlobalTrade[];
  loop: { at?: string; error?: string; filled?: number; expired?: number; closed?: number };
};

export async function api<T>(path: string, init?: { method?: "GET" | "POST" | "PUT"; body?: unknown }): Promise<T> {
  const session = (await supabase()?.auth.getSession())?.data.session;
  const headers: Record<string, string> = session ? { Authorization: `Bearer ${session.access_token}` } : {};
  if (init?.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`${API}/api${path}`, {
    method: init?.method ?? "GET",
    headers,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function inr(n: number): string {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(n);
}

// ---------------------------------------------------------------- strategies, backtests, signals

export type StrategyInfo = {
  id: string;
  version: string;
  name: string;
  description: string;
  market: string;
  timeframe: string;
  defaults: Record<string, number | boolean>;
  param_help: Record<string, string>;
};

export type Stats = {
  trades: number;
  win_rate?: number;
  expectancy_r?: number;
  avg_win_r?: number | null;
  avg_loss_r?: number | null;
  profit_factor?: number | null;
  net_pnl?: number;
  gross_pnl?: number;
  costs?: number;
  max_drawdown?: number;
  sharpe_daily?: number | null;
  worst_losing_streak?: number;
  sessions?: number;
  exit_reasons?: Record<string, number>;
};

export type Group = Record<string, { trades: number; win_rate: number; expectancy_r: number; profit_factor: number | null; net_pnl: number }>;

export type BacktestTrade = {
  symbol: string; side: "buy" | "sell"; session: string; entry_t: number; exit_t: number;
  entry: number; stop: number; target: number; exit: number; exit_reason: string; qty: number;
  gross: number; costs: number; net: number; r: number; rule: string;
  regime: { vix?: string | null; nifty_gap?: string | null };
};

export type Backtest = {
  id: string;
  status: "running" | "done" | "failed";
  created_at: number;
  strategy: { id: string; version: string; version_key: string; name: string; timeframe: string; params: Record<string, unknown> };
  universe: string;
  symbols: string[];
  config: Record<string, number>;
  progress: { done: number; total: number };
  errors: string[];
  period?: { from: string | null; to: string | null };
  report: null | {
    summary: Stats;
    verdict: { text: string; checks: Record<string, boolean>; gates: Record<string, number> };
    in_sample: Stats;
    out_of_sample: Stats;
    split_date: string | null;
    monte_carlo: { runs?: number; max_drawdown_median?: number; max_drawdown_95?: number };
    by_side: Group; by_time: Group; by_month: Group; by_vix: Group; by_nifty_gap: Group; by_symbol: Group;
    equity_curve: { t: number; equity: number }[];
  };
  trades?: BacktestTrade[];
};

export type BacktestBrief = {
  id: string; status: string; created_at: number; strategy: Backtest["strategy"]; universe: string; symbols: number;
  progress: { done: number; total: number }; period?: { from: string | null; to: string | null } | null;
  trades?: number | null; expectancy_r?: number | null; profit_factor?: number | null; net_pnl?: number | null; verdict?: string | null;
};

export type Condition = { name: string; met: boolean; detail: string };
export type Evidence = { source: string; trades: number; expectancy_r: number | null; profit_factor: number | null; win_rate: number | null; regime: string | null };

export type SignalCard = {
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  exchange: string;
  side: "buy" | "sell";
  created_at: string;
  fresh: boolean;
  bars_ago: number;
  explanation: {
    rule: string;
    conditions: Condition[];
    plan: {
      entry: number; stop: number; stop_reason: string; target: number | null; target_reason: string | null; exit_by: string | null;
      quantity: number | null; max_loss_with_charges: number | null;
      instrument?: "equity" | "future"; contract?: string | null; lot_size?: number | null; lots?: number | null;
      notional?: number | null; margin_estimate?: number | null;
    };
    evidence: Evidence[];
    similar: { count: number; wins: number; losses: number; average_r: number | null; note: string | null } | null;
    risks: string[];
    invalidation: string;
    confidence: "no track record" | "low" | "medium" | "high";
    summary: string | null;
  };
};

export type ScanResult = {
  strategy: { id: string; name: string; version_key: string };
  as_of: string;
  session: string;
  replay: boolean;
  market_open_now: boolean;
  regime: { vix: string | null; vix_prev_close: number | null; nifty_gap: string | null; nifty_gap_pct: number | null };
  scanned: number;
  signals: SignalCard[];
  errors: string[];
  data_source?: string | null;   // the least live source used in this scan: "kite_live" | "kite" | "yahoo"
};

export const r = (x: number | null | undefined, d = 2) => {
  if (x == null) return "–";
  const v = Number(x.toFixed(d)) || 0;              // −0.004 → "0.00R", not "-0.00R"
  return `${v > 0 ? "+" : ""}${v.toFixed(d)}R`;
};
export const pct = (x: number | null | undefined) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);

// ---------------------------------------------------------------- paper trading

export type RiskVerdict = { verdict: "PASS" | "BLOCK"; reasons: string[]; warnings: string[]; numbers: Record<string, number> };

export type PaperOrder = {
  id: string;
  signal_id: string | null;
  state: "proposed" | "rejected" | "working" | "filled" | "cancelled" | "expired";
  params: {
    symbol: string; side: "buy" | "sell"; qty: number; type: string; limit?: number; stop?: number; target?: number | null;
    leg: "entry" | "exit"; source?: string; strategy?: string | null; reason?: string; last_price?: number | null;
  };
  risk_verdict: RiskVerdict | null;
  created_at: string;
  approved_at: string | null;
};

export type PaperTrade = {
  id: string; symbol: string; side: "buy" | "sell"; qty: number; entry: number; exit: number | null;
  entry_ts: string; exit_ts: string | null; gross_pnl: number | null; costs: number | null; net_pnl: number | null;
  r_multiple: number | null; mae: number | null; mfe: number | null; exit_reason: string | null;
  entry_context: { stop?: number; target?: number | null; strategy?: string | null; summary?: string | null; source?: string } | null;
  last?: number | null; unrealized?: number | null; stop?: number; target?: number | null;
  underlying_stop?: number | null; underlying_target?: number | null;
};

export type PaperOverview = {
  account: {
    id: string; starting_capital: number; capital: number; cash_free: number; realized_today: number; realized_month: number;
    open_risk: number; entries_today: number; consecutive_losses: number; kill_switch: boolean;
  };
  limits: {
    max_loss_per_trade: number; max_daily_loss: number; max_monthly_loss: number; max_trades_per_day: number;
    max_consecutive_losses: number; max_open_positions: number; max_margin_used_pct: number;
    entry_start: string; entry_end: string; flat_by: string;
    max_loss_per_trade_fno: number | null; allow_naked_short_options: boolean;
  };
  fno_limit_available: boolean;
  positions: PaperTrade[];
  orders: PaperOrder[];
  trades: PaperTrade[];
  market: { is_open: boolean };
};

// ---------------------------------------------------------------- journal and AI

export type AiSummary = { summary: string | null; verdict_in_words: string | null; source: string; note?: string };

export type SimilarFromJournal = {
  available: boolean;
  reason?: string;
  note?: string;
  trades: { trade_id: string; symbol: string; side: string; entry_ts: string; exit_reason: string | null; net_pnl: number | null; r: number | null; similarity: number }[];
  wins?: number;
  losses?: number;
  average_r?: number | null;
};

export type JournalEntry = {
  trade_id: string; ai_postmortem: string | null; user_notes: string | null; mistake_tags: string[];
  emotion: string | null; ai_model?: string | null; lessons?: string | null;
};

export type Review = {
  id: string; kind: "daily" | "weekly" | "brief"; period_start: string; period_end: string; created_at: string; model: string | null;
  stats: Record<string, unknown>;
  body: {
    headline?: string; what_happened?: string; discipline?: string; patterns?: string[]; for_tomorrow?: string[];
    proposals?: { hypothesis: string; strategy_id: string; change: string; evidence: string }[];
    proposals_note?: string;
    market?: string; results_and_events?: string[]; stock_notes?: { symbol: string; note: string }[]; watch_out?: string[];
    unverified_numbers?: number[];
  };
};

export type Proposal = { id: string; hypothesis: string; change: { strategy?: string; change?: string }; evidence: { note?: string; tested?: boolean }; status: string; created_at: string };

export type JournalData = { trades: (PaperTrade & { journal: JournalEntry | null; rules_followed?: boolean })[]; reviews: Review[]; proposals: Proposal[] };

export type AskAnswer = { answer: string; based_on: string[]; model: string; unverified_numbers: number[] };

export type AiStatus = { configured: boolean; provider: string; model: string; tokens_today: number; daily_cap: number };

// ---------------------------------------------------------------- options

export type ChainSide = { ltp: number | null; bid: number | null; ask: number | null; iv: number | null; oi: number | null; chg_oi: number | null; volume: number | null; spread_pct: number | null; delta: number | null; tradingsymbol: string };
export type ChainData = {
  underlying: string; expiry: string; expiries: string[]; spot: number; forward: number; as_of: string; source: "kite" | "nse" | "modeled";
  lot_size: number; atm: number; pcr: number | null; max_pain: number | null; atm_iv: number | null;
  rows: { strike: number; CE?: ChainSide; PE?: ChainSide }[];
};
export type OptionLeg = { kind: "CE" | "PE"; strike: number; side: "buy" | "sell"; lots: number; price: number; tradingsymbol: string; mid: number | null; spread_pct: number | null; oi: number | null; iv: number | null };
export type OptionExits = { type: "debit" | "credit"; stop_pnl: number; target_pnl: number; planned_loss: number; rules: Record<string, number>; underlying_stop: number | null; underlying_target: number | null; side_hint: string | null };
export type StructureData = {
  template: string; description: string; underlying: string; expiry: string; lot_size: number; lots: number; qty: number; spot: number; forward: number;
  source: string; as_of: string; legs: OptionLeg[]; net_premium: number; max_profit: number | null; max_loss: number | null; breakevens: number[];
  greeks: { delta: number; gamma: number; theta: number; vega: number }; margin: number; charges_round_trip: number; naked: boolean; margin_source?: string;
  payoff: { x: number; expiry: number; today: number }[]; label: string; exits: OptionExits;
};

// ---------------------------------------------------------------- lab and webhooks

export type LabTest = {
  status: string; started_at: string; universe: string; error?: string; held_back_from?: string;
  baseline?: { in_sample: number | null; held_back: number | null; held_back_trades: number };
  candidate?: { in_sample: number | null; held_back: number | null; held_back_trades: number };
  bootstrap_share_better?: number; bootstrap_needed?: number; tests_this_month?: number;
  checks?: Record<string, boolean>; passed?: boolean; reasons?: string[]; warnings?: string[];
  baseline_version?: string; candidate_version?: string;
  unit?: "R" | "%"; baseline_is?: string;              // swing tests: % per trade vs holding the universe
};
export type LabProposal = {
  id: string; hypothesis: string; status: string; created_at: string;
  change: { strategy?: string; params?: Record<string, number | boolean>; change?: string; origin?: string; translation?: string };
  evidence: { tested?: boolean; note?: string; not_testable?: string } | null; test: LabTest | null;
};
export type StrategyVersion = { id: string; strategy_id: string; version: string; params: Record<string, unknown>; status: string; user_id: string | null; note?: string | null; paper_trades: number; paper_average_r: number | null };
export type LabData = { proposals: LabProposal[]; variants: StrategyVersion[]; tests_this_month: number; rules: Record<string, number> };
export type WebhookSource = { id: string; name: string; enabled: boolean; created_at: string; last_seen_at: string | null; alerts_received: number; alerts_rejected: number };
export type NewWebhookSource = { id: string; name: string; secret: string; path: string; message_template: string };
export type WebhookEvent = { id: number; source_id: string; received_at: string; remote_ip: string | null; payload: Record<string, unknown>; outcome: string; detail: string | null; order_id: string | null };
export type Inbox = { tickets: (PaperOrder & { explanation: SignalCard["explanation"] | null })[]; events: WebhookEvent[] };

// ---------------------------------------------------------------- live (Zerodha)

export type Gate = { gate: string; ok: boolean; detail: string };
export type LiveTrade = {
  id: string; symbol: string; side: "buy" | "sell"; qty: number; entry: number; entry_ts: string; exit: number | null;
  exit_ts: string | null; net_pnl: number | null; r_multiple: number | null; exit_reason: string | null;
  entry_context: { stop?: number; target?: number | null; protected?: boolean; exiting?: string; exit_requested?: string; summary?: string | null } | null;
};
export type LiveOverview = {
  gates: Gate[];
  session: { logged_in: boolean; kite_user_id?: string; expires_at?: string; logged_in_at?: string };
  limits: { live_max_loss_per_trade: number | null; live_max_daily_loss: number | null; live_max_trades_per_day: number | null;
            live_max_order_value: number | null; live_armed_on: string | null };
  orders: PaperOrder[]; positions: LiveTrade[]; trades: LiveTrade[];
  alerts: { id: number; ts: string; kind: string; detail: Record<string, unknown> }[];
  funds: { net: number; available: number } | null; funds_error?: string;
};
export type LivePreview = {
  order_id: string; exchange: string; symbol: string; side: "buy" | "sell"; qty: number; limit: number; stop: number;
  target: number | null; last_price: number | null; funds: { net: number; available: number }; margin: number | null;
  live_limits: { max_loss_per_trade: number; max_daily_loss: number; max_trades_per_day: number; max_order_value: number };
  management: string | null;      // the trade-management rules this order carries, in words
  verdict: { verdict: string; reasons: string[]; warnings: string[]; numbers: { max_loss?: number; charges_at_stop?: number; value?: number; reward_risk?: number } };
};

/** Whether live orders can be sent right now (every gate open). */
export function liveReady(o: LiveOverview | null): boolean {
  return !!o && o.gates.every((g) => g.ok);
}

// ---------------------------------------------------------------- automatic scan (all strategies)

export type RankedSignal = SignalCard & {
  universe: string; version_label: "default" | "lab";
  rank_info: { confidence: SignalCard["explanation"]["confidence"]; evidence_r: number | null; evidence_trades: number };
};
export type TradeIdea = {
  rank: number; symbol: string; side: "buy" | "sell"; best: RankedSignal; signals: RankedSignal[]; strategies: string[];
  confidence: SignalCard["explanation"]["confidence"]; fresh: boolean; latest: string; conflict: boolean;
  fno: boolean; segment: "index" | "stock";
};
export type AutoScanJob = {
  id: string; status: "running" | "done" | "failed"; started_at: string; progress: { done: number; total: number };
  plan: { strategy: string; name: string; version: string; label: string; universe: string }[];
  errors: string[]; ideas: TradeIdea[]; signal_count?: number; raw_count?: number; took_s?: number;
  as_of?: string; session?: string; regime?: ScanResult["regime"]; market_open_now?: boolean; replay?: boolean; scanned_symbols?: number;
  data_source?: string | null;
};

// ---------------------------------------------------------------- futures and options for a signal

export type FnoFuture = {
  contract: string; symbol: string; expiry: string | null; lot_size: number; lots: number; side: "buy" | "sell";
  notional: number; margin: number; loss_at_stop: number; gain_at_target: number | null; fits_limit: boolean | null;
  limit: number | null; price_note: string; ticket: { symbol: string; side: string; stop: number; target: number | null };
};
export type FnoOption = {
  contract: string; kind: "CE" | "PE"; strike: number; expiry: string; days_to_expiry: number; action: string;
  lot_size: number; lots: number; qty: number; bid: number | null; ask: number; mid: number | null; spread_pct: number | null;
  oi: number | null; iv: number | null; delta: number | null; theta_per_day: number | null; spot: number; as_of: string; source: string;
  setup_failed: boolean; cost: number; max_loss: number; planned_loss: number; est_at_stop: number | null; pnl_at_stop: number | null;
  est_at_target: number | null; pnl_at_target: number | null; fits_limit: boolean | null; limit: number | null; warnings: string[];
  ticket: { underlying: string; expiry: string; template: string; params: Record<string, number>; exits: Record<string, number | string | null> };
};
export type FnoPlan = {
  underlying: string; available: boolean; reason?: string; is_index?: boolean; side?: "buy" | "sell";
  levels?: { entry: number; stop: number; target: number | null };
  future?: FnoFuture; future_error?: string; option?: FnoOption; option_error?: string; note?: string;
  spot?: number; setup_failed?: boolean;
};

// ---------------------------------------------------------------- trade monitor

export type MonitorCheck = { key: string; name: string; ok: boolean; severity: "high" | "medium" | "low"; detail: string };
export type StopMove = { t: number; from: number; to: number; why: string };
export type MonitorAlert = { id: string; at: string; level: "high" | "medium"; title: string; detail: string; trade_id: string };
export type MonitorHealth = "on track" | "watch" | "at risk" | "thesis broken" | "no data" | "closed";
export type MonitorTrade = {
  trade: { id: string; mode: "paper" | "live"; symbol: string; side: "buy" | "sell"; qty: number; entry: number; entry_ts: string;
           exit_ts: string | null; kind: string; strategy: string | null; summary: string | null;
           strategy_link?: { by: "matched" | "you"; signal_time?: string; on?: string } | null };
  underlying: string; direction: "long" | "short";
  /** stop = the stop in force now; initial_stop = the stop at entry (they differ once trade management moved it) */
  levels: { entry: number; stop: number | null; initial_stop?: number | null; target: number | null; entry_t: number };
  management?: null | { description: string | null; moves: StopMove[]; notes: string[]; open_qty: number | null;
                        partial: null | { t: number; qty: number; price: number; at_r: number } };
  health: MonitorHealth; headline: string;
  progress?: { price: number; r: number | null; mfe_r: number | null; mae_r: number | null; to_target_pct: number | null; to_stop_pct: number | null; stop_r?: number | null };
  time?: { minutes_in: number; minutes_to_flat: number };
  checks: MonitorCheck[]; sr: { price: number; label: string; kind: "support" | "resistance" }[];
  indicators?: { ema20: number | null; supertrend: number | null; supertrend_dir: number | null };
  expected?: { trades: number; median_minutes_to_target: number | null; median_minutes_to_stop: number | null; target_share: number;
               exit_mix?: { target: number; stop: number; time: number } } | null;
  option: null | { legs: { contract: string; side: string; entry: number; bid: number | null; ask: number | null; mid: number | null; delta: number | null; theta: number | null; iv: number | null }[];
                   pnl: number | null; stop_pnl: number | null; target_pnl: number | null; spot: number; as_of: string; lot_size: number | null; source?: string };
  alerts: MonitorAlert[]; as_of: string; data_source?: string | null;
};
export type MonitorSummary = { trade_id: string; mode: string; symbol: string; underlying: string | null; health: MonitorHealth; headline: string; r: number | null; option_pnl: number | null; alerts: MonitorAlert[] };
