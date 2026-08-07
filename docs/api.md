# API REST

FastAPI, `backend/etoro_bot/api/server.py`. Titolo `Trading Bot API`,
versione `3.0.0`. Documentazione interattiva generata da FastAPI su `/docs` e
`/redoc` (raggiungibili solo dall'interno della rete Docker, e comunque
soggette al token interno).

## Regole generali

- **Token interno obbligatorio.** Ogni richiesta deve portare
  `X-Trading-Internal-Token` uguale a `TRADING_INTERNAL_TOKEN`, altrimenti
  **401**. Unica rotta esente: `GET /health`. Se la variabile è vuota il
  backend non parte, salvo `TRADING_DEV_MODE=1` (sviluppo locale: controllo
  disattivato, warning nei log).
- **Identità.** Arriva dagli header `X-Trading-User-Id`,
  `X-Trading-User-Email`, `X-Trading-User-Name`, iniettati lato server dal proxy
  Next dopo aver verificato la sessione Authentik. In assenza,
  `X-Trading-User-Id` vale `anonimo` (nessun privilegio). Il valore letterale
  `system` è riservato ai job interni: via HTTP dà **403**.
- **Proprietario.** Nella colonna «Auth», *proprietario* significa che
  l'endpoint chiama `require_owner`: passa solo l'identità che ha configurato le
  chiavi eToro, altrimenti **403**. Finché nessuno ha configurato le chiavi il
  controllo è un no-op; se il proprietario non è verificabile (DB giù) è **503**.
- **Codici ricorrenti.** `202` per i job avviati in background, `403` per il
  controllo di proprietà, `404` risorsa inesistente, `409` conflitto di stato,
  `415` estensione non supportata, `422` validazione/guardrail, `502` API eToro
  non disponibile.
- Il client non può raggiungere direttamente il backend: dal browser i percorsi
  qui elencati si usano con prefisso `/api` (es. `/api/status`).

---

## Salute e stato

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/health` | **nessuna** (esente dal token) | `{"status": "ok"}`. Sonda di healthcheck del container |
| GET | `/status` | token | Riepilogo generale: kill switch, circuit breaker (`tripped`, `reason`, `until`), stato arena, campione, `market_open`, `open_sessions`, `next_cycle_at`, ultima equity e variazione giornaliera. Resta consultabile anche a DB giù |

---

## Esecuzioni

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/executions?limit=` | token | Giornale delle esecuzioni (`limit` ≤ 500, default 50): lato, importo, stato, dettaglio, prezzo, `etoro_position_id` |

> Le tabelle `runs` e `decisions` continuano a essere scritte dal ciclo live
> (`arena/live.py`) come traccia forense, ma non hanno rotte HTTP: si leggono
> direttamente dal database.

---

## Portafoglio e operatività

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/portfolio` | token (richiede chiavi eToro) | Posizioni **del bot** con prezzo corrente e PnL non realizzato, più `cash_usd`, `equity_usd`, `exposure_usd` e `max_trade_amount_usd` (dal `max_position_pct` del campione). **502** se il portafoglio eToro non è raggiungibile, **422** se le chiavi mancano |
| GET | `/trades?statuses=&symbol=` | token | Vista operativa: posizioni aperte (stato `open`, `can_close: true`) più le esecuzioni che non sono già rappresentate da una posizione (esclude le `filled` con `etoro_position_id`). `statuses` è una lista separata da virgole; `symbol` è un match parziale case-insensitive |
| POST | `/trades/{position_id}/close` | **proprietario** | Chiusura manuale di una posizione reale. Body `{"confirmation": "CHIUDI"}` (esatto, altrimenti 422). Prova a leggere subito `closeRate`/`netProfit` dalla trade history; se non c'è ancora, la posizione resta in attesa di liquidazione. 404 se non è aperta |
| POST | `/executions/{execution_id}/cancel` | **proprietario** | Annulla un'esecuzione ancora `pending`. **409** se è già terminale o inesistente |
| GET | `/trade-history?statuses=&date_from=&date_to=&symbol=` | token | Storico unificato: posizioni aperte, posizioni chiuse (con `pnl_usd`) ed esecuzioni non `filled`, ordinato per data decrescente |

---

## Backtest e metriche

Le date sono `YYYY-MM-DD`. I prezzi del benchmark passano dalle chiavi eToro
dell'utente, ma il fallimento del fetch è assorbito: senza chiavi le metriche del
bot restano calcolabili e il confronto col benchmark (alpha, beta, information
ratio, curve SPY) risulta semplicemente `null`.

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/backtest/summary?date_from=&date_to=` | token | Metriche: total return, CAGR, volatilità, Sharpe, Sortino, max drawdown, Calmar, alpha, beta, information ratio, win rate, profit factor, recovery factor, expectancy, esposizione, max/std di vincite e perdite. Più `n_closed_trades`, `n_days`, `insufficient_sample`, `annualization_available`, `risk_free_rate_pct` |
| GET | `/backtest/equity-curve?benchmark=spy&date_from=&date_to=` | token | Punti della curva equity contro benchmark, con nota sui dividendi non inclusi nel prezzo SPY |
| GET | `/backtest/trades` | token | Elenco dei round-trip chiusi |
| GET | `/backtest/monthly-returns` | token | Rendimenti mensili |

---

## Knowledge base e news

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/knowledge/status` | token | Stato delle collezioni Qdrant + feed RSS dell'utente + timestamp dell'ultimo fetch |
| PUT | `/knowledge/rss-feeds` | token | Sostituisce l'elenco feed dell'utente. Body `{"feeds": [...]}`. Ogni URL deve essere `http(s)` e superare il controllo SSRF; massimo 50. **422** su URL non valido o non ammesso |
| GET | `/news` | token | Ultime news scaricate per questo utente (`items`, `updated_at`), lette dal file di stato |
| POST | `/knowledge/fetch-news` | token | **202**. Avvia in background fetch + pipeline completa (indicizza, pota, aggiorna memorie, refresh universo) |
| POST | `/knowledge/ingest` | token | Upload multipart (`file`). Estensioni ammesse: `.pdf .docx .pptx .xlsx .md .txt`. **415** estensione non supportata, **422** contenuto non processabile. Ritorna `chunks_indexed` e i `tickers` rilevati |

> L'universo dinamico non ha rotte HTTP: la discovery gira dentro la pipeline
> news (schedulata, oppure on-demand via `POST /knowledge/fetch-news`) e il suo
> stato vive nel file di stato letto dagli agenti. Per un refresh manuale fuori
> dall'API: `python -m etoro_bot.services.universe`.

---

## Account e impostazioni

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/account/credentials` | token | Identità corrente e **solo** i flag `etoro_api_key_configured`, `etoro_user_key_configured`, `openai_api_key_configured`. I valori non sono mai esposti |
| PUT | `/account/credentials` | token | Salva le chiavi personali cifrate. Body: `etoro_api_key`, `etoro_user_key`, `openai_api_key` (tutti opzionali; `null` lascia invariato il valore esistente) |
| GET | `/settings` | token | Impostazioni effettive (`timezone`, `currency`, blocco `arena`) più `api_keys_configured` e `openai_configured` |
| PUT | `/settings` | **proprietario** | Modifica `timezone` e/o `currency`. Qualsiasi altra chiave ⇒ **422**, come una timezone IANA non valida o una valuta non supportata |
| GET | `/settings/audit` | token | Storico dei cambi di impostazione (chiave, valore vecchio/nuovo, sorgente, data) |
| GET | `/fx/rates?refresh=` | token | Tassi USD→valuta e valute selezionabili. `refresh=true` forza la rilettura remota. Non fallisce mai: al peggio risponde `stale: true` con la sola identità USD |

---

## Arena

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| GET | `/arena` | token | Panoramica: stato (`month`, `paused`, `live_enabled`), sessioni con flag `open_now`, `next_cycle_at`, `days_to_evaluation`, generazione corrente, agenti vivi (DNA, memoria, cash, equity, PnL del mese, posizioni aperte), campione, e `lineage` con tutta la discendenza storica (senza memoria) |
| GET | `/arena/agents/{agent_id}` | token | Dettaglio di un agente: payload completo, serie equity simulata, trade chiusi. **404** se l'agente non esiste |
| GET | `/arena/events?limit=` | token | Log evolutivo (`limit` ≤ 500, default 100): `birth`, `death`, `evolution`, `pause`, `resume`, `live_on`, `live_off`, `error` |
| POST | `/arena/pause` | **proprietario** | Sospende i cicli di allenamento; registra un evento `pause` |
| POST | `/arena/resume` | **proprietario** | Riprende l'allenamento; evento `resume` |
| POST | `/arena/cycle` | **proprietario** | **202**. Forza un ciclo di allenamento in background (test/monitoraggio) |

---

## Trading live

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| POST | `/live/enable` | conferma + guardrail | Accende il trading su **denaro reale** col DNA del campione. Body `{"confirmation": true}` obbligatorio (**422** altrimenti). `check_live_activation` esige inoltre: chiavi eToro configurate, un campione esistente, kill switch non attivo, circuit breaker non scattato — ogni violazione è un **422** con messaggio esplicito. Evento `live_on` |
| POST | `/live/disable` | **proprietario** | Spegne il trading live (l'arena continua ad allenarsi). Evento `live_off` |

---

## Kill switch

| Metodo | Rotta | Auth | Descrizione |
|---|---|---|---|
| POST | `/kill-switch` | **proprietario** | Crea il file `KILL_SWITCH`: blocca ogni ordine |
| DELETE | `/kill-switch` | **proprietario** | Rimuove il file. Se `ETORO_BOT_KILL=1` è impostato nell'ambiente il kill switch resta attivo: la risposta lo riporta in `kill_switch_active` |

---

## Note di implementazione

- I job avviati con **202** (`/knowledge/fetch-news`, `/arena/cycle`) girano in
  thread daemon: la risposta non attende l'esito, che va letto dai log o dallo
  stato successivo.
- Gli import dei moduli pesanti sono lazy dentro le funzioni di rotta: l'API
  resta avviabile anche se qualche extra opzionale (`rag`) non è installato.
- Il `Repository` è memoizzato con `lru_cache` (una sola sessione factory per
  processo); il `CircuitBreaker` è un'istanza condivisa per file di stato.
- Lo scheduler parte nel `lifespan` dell'app, salvo `DISABLE_SCHEDULER=1`.
