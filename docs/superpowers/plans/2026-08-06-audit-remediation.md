# Remediation audit completo (dev-3.0) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Ogni task è pensato per un agente Opus 5 autonomo: contiene file, contesto e criteri di accettazione autosufficienti.

**Goal:** correggere tutti i finding dell'audit del 2026-08-06 (5 agenti: architettura, backend Python, frontend React, sicurezza, semplificazione) in ordine di rischio: prima il percorso denaro reale, poi autorizzazione/sicurezza, poi robustezza, frontend, e infine semplificazione e test.

**Origine:** audit multi-agente su tutto il repo. Il bot muove denaro reale su eToro: la priorità assoluta è la coerenza broker ↔ registro DB e la direzione (long/short) delle posizioni.

**Tech Stack:** FastAPI + SQLAlchemy 2 + Alembic (backend, Python 3.12, uv), Next.js App Router + TypeScript + TanStack Query (frontend), Docker Compose.

## Global Constraints

- Lingua UI, commenti e docstring: **italiano**, tono asciutto come il codice esistente.
- Lavorare SOLO in `/home/mattia/docker/projects/Trading` (checkout `dev-3.0`). NON committare: il commit lo fa l'orchestratore a fine fase.
- Backend: nessuna nuova dipendenza. Test: `cd backend && uv run pytest`. Lint: `uv run ruff check`.
- Frontend: nessuna nuova dipendenza salvo dove il task lo dice esplicitamente (Task 2.4: bump versioni). Verifica: `cd frontend && npm run lint && npx tsc --noEmit`.
- Migrazioni DB: sempre via Alembic (`backend/etoro_bot/db/migrations/`), mai DDL manuale. Ogni migrazione deve avere downgrade funzionante.
- Compatibilità API: il payload esistente non perde campi, solo aggiunte (il frontend è in polling continuo).
- Ogni fix su un percorso monetario aggiunge o aggiorna ALMENO un test che fallirebbe senza il fix.
- Non "sistemare" cose non richieste dal task: diff minimo.

## Ordine ed esecuzione

- **Fase 1 è bloccante e sequenziale** (i task toccano gli stessi file: `arena/live.py`, `db/repo.py`).
- Fase 2, Fase 4 e i task di Fase 3 marcati ∥ sono parallelizzabili fra loro dopo la Fase 1.
- Fase 5 (refactor) SOLO dopo che 1–4 sono verdi: sposta il codice appena corretto.
- Fase 6 (test) può partire in parallelo alla Fase 5 ma va rebasata sui file spostati.

---

## Fase 0 — Quick wins (un task, diff da poche righe, impatto sproporzionato)

### Task 0.1: tre one-liner ad alto impatto

**Files:**
- `backend/etoro_bot/services/app_settings.py:75`
- `backend/etoro_bot/etoro/client.py:118` (+ factory in `api/server.py:419,544` e `_arena_deps`)
- `backend/etoro_bot/api/server.py:447`

**Steps:**
- [ ] `check_live_activation` costruisce `CircuitBreaker(load_breaker_rules())` effimero: sostituire con `get_breaker(...)` (singleton, `safety/circuit_breaker.py:150-169`). L'istanza effimera può chiamare `reset()` → `_save()` e sovrascrivere lo stato condiviso su file.
- [ ] `RateLimiter` è stato di istanza e ogni `_arena_deps()`/`_make_client()` crea un `EtoroClient` nuovo con budget nuovo (pool `execution` 20/60s moltiplicato per ogni chiamante). Condividere: `RateLimiter` a livello di modulo oppure `lru_cache` sulla factory del client con chiave = api key. Si preservano anche le cache `_instrument_types`/`_stocks_industries`.
- [ ] `BacktestService` costruito senza settings: passare `settings=_full_settings()` a `api/server.py:447`.
- [ ] Test: uno per il singleton breaker (due chiamate a `check_live_activation` non creano istanze diverse), uno che due client con stessa key condividono il rate limiter.

**Acceptance:** pytest verde; `grep -n "CircuitBreaker(" backend/etoro_bot/services/app_settings.py` non trova costruzioni dirette.

---

## Fase 1 — Percorso denaro reale (CRITICHE, sequenziale)

### Task 1.1: persistere la direzione delle posizioni (live + sim)

Il bug più grave dell'audit. Live: la direzione è riletta dal portafoglio eToro a ogni ciclo (`arena/live.py:105-112`); su errore `_portfolio_positions` ritorna `[]` e ogni `directions.get(pid, LONG)` degrada in silenzio a LONG → su uno short reale lo stop loss scatta quando la posizione guadagna e il take profit quando perde (`live.py:115-118,585,637,758`). Sim: la direzione è un prefisso `[SHORT]` dentro `open_reason`, testo libero scritto dall'LLM (`arena/engine.py:38,74-85`, `db/models.py:192`).

**Files:**
- `backend/etoro_bot/db/models.py` — colonna `direction` (`String`, not null, default `"long"`) su `bot_positions`, `sim_positions`, `sim_trades`
- Nuova migrazione Alembic con backfill: `bot_positions` da portafoglio non ricostruibile → backfill `"long"` con nota; `sim_*` dal prefisso `[SHORT]` in `open_reason`
- `backend/etoro_bot/db/repo.py` — `register_open_position`, `open_sim_position`, `close_sim_position` accettano/scrivono `direction`
- `backend/etoro_bot/arena/live.py` — scrivere la direzione all'apertura (già nota a `live.py:300`); leggerla dal registro; il portafoglio resta solo corroborazione. Se le direzioni non sono leggibili: **saltare lo sweep SL/TP con log esplicito**, mai assumere long.
- `backend/etoro_bot/arena/engine.py` — usare la colonna, `open_reason` torna solo testo (mantenere parsing legacy in lettura per righe vecchie)
- `backend/etoro_bot/arena/metrics.py:109-110` — metriche long/short dalla colonna
- `backend/etoro_bot/api/server.py:385,744,858` — `/portfolio` espone `direction`; `/trades` e `/trade-history` derivano `side` dalla direzione persistita invece di hardcodare `"buy"`
- `frontend/lib/types.ts:53-64` — `Position.direction`; pagina portafoglio rende la direzione (riusare `DirectionStamp` già esistente, oggi usato solo in /training)

**Acceptance:** test nuovo: posizione short registrata, portafoglio irraggiungibile → sweep SL/TP saltato (non chiusura invertita); test: `position_change_pct` con short usa il segno giusto dalla colonna. Migrazione up+down verde su DB di test. `tsc --noEmit` verde.

### Task 1.2: write-ahead dell'intento ordine (apertura)

Un fill sul broker seguito da crash o da `register_open_position` che solleva (`arena/live.py:330-353`) non lascia traccia: né riga `failed` con `ref=`, né positionId → posizione reale orfana, invisibile, senza stop loss, e un ciclo successivo può riaprire lo stesso simbolo (doppia esposizione). Nota: `ExecutionStatus` non ha `PENDING` ma `cancel_pending_execution`, `POST /executions/{id}/cancel` e `can_cancel` in UI esistono già come percorso morto (`domain.py:16-20`, `repo.py:121-128`, `server.py:774,833-840`) — questo task lo rianima.

**Files:**
- `backend/etoro_bot/domain.py` — aggiungere `PENDING` a `ExecutionStatus`
- `backend/etoro_bot/arena/live.py` (`_open_real_position`) — sequenza: `add_execution(status="pending", detail="ref=<request_id>")` PRIMA di `client.open_position`; su fill → update a `filled` + `register_open_position`; se `register_open_position` fallisce → l'esecuzione resta comunque tracciata con `ref=` così `reconcile_live_positions` (`live.py:427-459`, `_REF_RE`) la adotta al ciclo dopo; su errore broker → update a `failed`
- `backend/etoro_bot/db/repo.py` — metodo di update stato esecuzione (se manca)
- `backend/etoro_bot/arena/live.py:57,424` — il reconcile cerca i pending/failed con query dedicata `status IN ('pending','failed') AND created_at >= since` invece di `list_executions(limit=500)` (in giornate attive le 500 righe non coprono 24h)

**Acceptance:** test nuovo "broker OK, DB KO in apertura": `register_open_position` che solleva → esecuzione presente con `ref=`, e il reconcile successivo adotta la posizione. Test crash simulato fra fill e registrazione (pending orfano → adottato). Pytest verde.

### Task 1.3: chiusure affidabili + reconcile bidirezionale

Due metà dello stesso problema. (a) `_close_real_position` (`arena/live.py:175-203`): se il broker chiude ma `repo.close_position` fallisce, la posizione resta "aperta" a registro per sempre, ritentata a ogni ciclo. `close_position` del client non passa `request_id` e non ha recovery (`etoro/client.py:439-457`). (b) Il reconcile adotta solo aperture, mai chiusure (`live.py:397-490`): una posizione chiusa lato broker (manuale su eToro, margin call, o il caso (a)) gonfia exposure/equity, falsa breaker, sizing e `/portfolio`.

**Files:**
- `backend/etoro_bot/etoro/client.py` — `close_position` accetta `request_id` deterministico (`position_id` + slot) e lo invia come `x-request-id`
- `backend/etoro_bot/arena/live.py` (`_close_real_position`) — se il broker ha chiuso e la scrittura DB fallisce: retry della sola scrittura (idempotente su `etoro_position_id`); se fallisce ancora, lasciare al reconcile
- `backend/etoro_bot/arena/live.py` (`reconcile_live_positions`) — dopo una lettura del portafoglio **riuscita e non vuota**: ogni `bot_positions` aperta assente dal portafoglio (finestra di grazia per ordini appena inviati) va chiusa col `netProfit` da trade history e `pnl_settled` di conseguenza. Se la lettura fallisce: astenersi.
- `backend/etoro_bot/api/server.py:806-829` (`/trades/{id}/close`) — try/except allineato a `/portfolio` (`HTTPException(502)` su errore broker) + stesso pattern di recovery sulla scrittura post-chiusura

**Acceptance:** test "broker chiude, DB KO" → posizione chiusa a registro entro il reconcile successivo, nessun retry infinito. Test reconcile: posizione a registro assente dal portafoglio → chiusa con PnL da trade history; portafoglio in errore → nessuna chiusura. Test endpoint: errore broker → 502, non 500.

### Task 1.4: lock evoluzione ↔ ciclo training + FOR UPDATE

`maybe_evolve` (`arena/evolution.py:57-70`) non acquisisce alcun lock mentre `run_training_cycle` è protetto da `_cycle_lock` (`engine.py:245-263`); lo scheduler può lanciarli in thread separati nello stesso tick (`services/scheduler.py:184-204`). `open_sim_position`/`close_sim_position` fanno read-modify-write su `cash_usd` senza lock di riga (`db/repo.py:384-446`). Lost update sull'equity che decide il **campione** — il DNA che opera con denaro reale.

**Files:**
- `backend/etoro_bot/arena/evolution.py` — `maybe_evolve` acquisisce lo stesso `_cycle_lock` di `run_training_cycle` prima di liquidare/valutare
- `backend/etoro_bot/db/repo.py:384-446` — `with_for_update()` sulla riga `Agent` prima di modificare `cash_usd` (difesa in profondità indipendente dal lock applicativo)

**Acceptance:** test di concorrenza reale (`threading`): evolve + cycle simultanei sullo stesso agente → nessun lost update su `cash_usd` (valore finale = somma deterministica delle operazioni). Pytest verde.

### Task 1.5: invariante mono-processo + stato scheduler persistente

Tutta la sicurezza concorrente poggia su un solo processo uvicorn (lock in-process, singleton breaker): `--workers 2` o una replica moltiplicano gli scheduler → ordini duplicati. Inoltre lo stato scheduler (`cycle_slot`, `eod_done`, `evolve_day`, `news_day` — `services/scheduler.py:167-172,200-204`) è solo in memoria: un riavvio a metà slot rilancia il ciclo con una NUOVA decisione LLM (il `request_id` deduplica stesso simbolo/slot, non decisioni diverse); un restart-loop può tradare più volte nella stessa finestra.

**Files:**
- `backend/etoro_bot/services/scheduler.py` — persistere ultimo slot eseguito e chiavi `eod_done`/`evolve_day`/`news_day` (file JSON in `state/` con scrittura atomica tmp+replace, stesso pattern di `circuit_breaker.py:76-80`); al riavvio, saltare slot/EOD già eseguiti
- `backend/etoro_bot/api/server.py` (startup) o `main.py` — guard all'avvio: advisory lock Postgres (`pg_try_advisory_lock`) attorno allo scheduler/ciclo live; se il lock non è acquisibile, log CRITICO e scheduler disattivato
- `backend/Dockerfile:14` — commento esplicito: MAI `--workers > 1`

**Acceptance:** test: riavvio simulato dentro lo stesso slot → il ciclo NON riparte; slot successivo → riparte. Test advisory lock: seconda istanza non avvia lo scheduler.

---

## Fase 2 — Autorizzazione e sicurezza (∥ dopo Fase 1)

### Task 2.1: `require_owner` fail-closed + token interno obbligatorio

`require_owner` (`api/server.py:102-113`): `except Exception: owner = None` → con Postgres giù il controllo non scatta e chiunque raggiunga l'API può azionare kill switch, `/live/enable`, `/trades/{id}/close`. Inoltre `TRADING_INTERNAL_TOKEN` vuoto disattiva il middleware (`server.py:71-80`) e l'header assente defaulta a `"system"` che `require_owner` autorizza sempre (`server.py:90-99,112`).

**Files:**
- `backend/etoro_bot/api/server.py` — (a) DB non leggibile ⇒ `HTTPException(503)`, mai `owner = None`; distinguere "nessun owner configurato" da "owner non verificabile". (b) Rifiutare qualunque header `X-Trading-User-Id` con valore letterale `"system"` proveniente da HTTP (riservato alle chiamate interne dello scheduler che non passano da FastAPI). (c) Fail-closed all'avvio: se `TRADING_INTERNAL_TOKEN` è vuoto e non c'è un flag esplicito di dev (`TRADING_DEV_MODE=1` o simile), rifiutare l'avvio.
- `.env.example` — documentare il token come obbligatorio
- Test: owner_user_id che solleva → 503 su endpoint owner; header `"system"` via HTTP → 403; avvio senza token e senza flag dev → abort.

**Acceptance:** i tre test sopra verdi; smoke test locale con flag dev documentato nel task report.

### Task 2.2: `require_owner` sugli endpoint knowledge + limite upload prima del read

`PUT /knowledge/rss-feeds` (:618), `POST /knowledge/fetch-news` (:662), `POST /knowledge/ingest` (:693) sono gli unici endpoint mutanti senza `require_owner`: un utente Authentik non-owner può avvelenare la KB globale che alimenta i prompt del trader LLM. Inoltre l'upload fa `await file.read()` (`server.py:709`) PRIMA del check `MAX_UPLOAD_BYTES` (`knowledge/ingest.py:118`): OOM del container (2g) che ospita anche scheduler e breaker.

**Files:**
- `backend/etoro_bot/api/server.py` — `require_owner(identity, ...)` sui tre endpoint; check `Content-Length` + lettura a chunk con abort oltre `MAX_UPLOAD_BYTES` prima di bufferizzare
- Test: non-owner su ciascuno dei tre endpoint → 403; upload oltre limite → 413 senza lettura completa del body.

**Acceptance:** test verdi; nessun altro endpoint mutante senza `require_owner` (verifica con grep su `def.*post|put|delete` vs `require_owner`).

### Task 2.3: ciclo live indipendente dall'LLM per SL/TP e reconcile

`if deps.llm is None: return` (`arena/live.py:542-556`) avviene PRIMA di reconcile, settle e sweep SL/TP (`live.py:558-599`): chiave OpenAI rimossa/scaduta = posizioni reali senza protezione. Correlato: `_live_cash` accetta `credit` assente come `0.0` (`live.py:146-147`) mentre `/portfolio` sulla stessa lettura solleva 502 — degrado doppio e silenzioso.

**Files:**
- `backend/etoro_bot/arena/live.py` — riordinare: reconcile + settle + sweep SL/TP SEMPRE; solo la fase decisione/apertura salta se `deps.llm is None`. Se `credit` manca dal portafoglio: saltare il ciclo con log esplicito (stessa severità del percorso API), non `cash = 0`.
- Test: `deps.llm = None` con posizione oltre stop loss → chiusura eseguita comunque; portafoglio senza `credit` → ciclo saltato, nessun ordine.

**Acceptance:** test verdi; nessuna regressione su `test_arena_live.py`.

### Task 2.4: bump dipendenze frontend con CVE (next-auth, Next.js)

`next-auth@5.0.0-beta.31` ha advisory CRITICA GHSA-8fpg-xm3f-6cx3 ("auth fail-open") e `proxy.ts` è l'UNICO gate dell'app (tutte le rotte `/api/*` muovono denaro). `next@16.2.10` ha GHSA-6gpp-xcg3-4w24 (bypass middleware) e GHSA-p9j2-gv94-2wf4 (SSRF rewrites).

**Files:**
- `frontend/package.json` + `package-lock.json` — bump `next-auth`/`@auth/core` a versione con le CVE risolte; `next` ≥ 16.3.0; `npm audit --audit-level=high` pulito
- Verifica post-bump: `npm run lint`, `npx tsc --noEmit`, `npm run build`; controllo manuale che `proxy.ts` continui a bloccare rotte non autenticate (test: richiesta senza sessione → redirect/401)

**Acceptance:** `npm audit --audit-level=high` senza finding su next/next-auth; build verde.

### Task 2.5: fix open redirect login + key instabile knowledge (frontend HIGH)

(a) `app/login/page.tsx:26`: `redirect(callbackUrl ?? "/")` con `callbackUrl` da query string non validato → open redirect da dominio fidato. (b) `app/knowledge/page.tsx:107`: `` key={`${index}-${feed}`} `` include il valore del textbox → remount e perdita focus a ogni carattere.

**Files:**
- `frontend/app/login/page.tsx` — accettare solo path relativi same-origin (inizia con `/`, non `//`) altrimenti `/`
- `frontend/app/knowledge/page.tsx` — key stabile (index o id generato per riga), mai derivata dal valore del campo

**Acceptance:** digitare più caratteri consecutivi nel campo RSS mantiene il focus (verifica manuale o test); `/login?callbackUrl=https://evil.example` non redirige off-domain. Lint + tsc verdi.

---

## Fase 3 — Robustezza e coerenza contabile (∥ fra loro dopo Fase 1)

### Task 3.1 ∥: equity unificata mark-to-market

Tre definizioni divergenti: live a costo storico (`live.py:574,603,748,255`; `server.py:405`) mentre i prezzi sono già disponibili; sim con `_agent_payload` a costo (`server.py:1048`) vs `agent_equity` MTM (`engine.py:128`) — stessa pagina, numeri diversi. Il denominatore del breaker (`max_daily_loss_pct`) e il sizing (`max_position_pct`) usano il valore stantio. Correlato: `settle_pending_closes` su errore lascia `equity = 0.0` e `record_closed_trade` salta il check drawdown se `equity_usd <= 0` (`live.py:253-257`, `circuit_breaker.py:120-124`) — breaker disattivato in silenzio proprio nel settle.

**Files:**
- `backend/etoro_bot/arena/live.py` — un solo helper `live_equity(positions, prices, cash)` usato da breaker, sizing e `/portfolio`; se equity non calcolabile nel settle: saltare esplicitamente l'update del breaker per quel batch (log warning), non procedere con 0
- `backend/etoro_bot/api/server.py` — `_agent_payload` usa la stessa definizione di `agent_equity` (se i prezzi non sono nella route: ultimo punto `sim_equity`)
- Test: equity MTM ≠ costo con prezzo mosso; settle con equity non calcolabile → breaker non aggiornato e warning loggato.

### Task 3.2 ∥: dedup chiusure in `enforce` + un solo `get_portfolio` per ciclo

`enforce` (`arena/trader.py:113-124`) non deduplica: due `close` sullo stesso simbolo (output LLM plausibile) → secondo ordine al broker e **doppio** `record_closed_trade` sul breaker (drawdown giornaliero contato due volte). `get_portfolio()` chiamato 3-4 volte per ciclo (`live.py:107,146` + reconcile + settle): budget rate-limit sprecato e viste TOCTOU incoerenti.

**Files:**
- `backend/etoro_bot/arena/trader.py` — dedup su `(symbol, direction)` in `enforce`
- `backend/etoro_bot/arena/live.py` — set dei `position_id` già chiusi nel ciclo; una sola lettura portafoglio per ciclo passata come parametro a reconcile/directions/cash/settle
- Test: azione LLM con due close sullo stesso simbolo → un solo ordine, un solo record sul breaker.

### Task 3.3 ∥: `main.py` non importa da `api/` + fix layer

`main.py:17` importa `_arena_deps` privata di `api/server.py` (CLI dipende dal layer API). `arena/metrics.py:13` importa `arena.engine` (e con esso repo/trader/dna) per un helper, violando la propria docstring di purezza.

**Files:**
- Nuovo `backend/etoro_bot/services/deps.py` — `_arena_deps` spostata lì (nome pubblico `build_arena_deps`); `api/server.py` e `main.py` importano da lì
- `arena/dna.py` (o `arena/common.py`) — spostarci `position_direction` + `SHORT_TAG`; `metrics.py` non importa più `engine`
- Test esistenti aggiornati agli import nuovi.

### Task 3.4 ∥: contratto API tipato (response_model + tipi TS allineati)

Nessuna route ha `response_model`: `lib/types.ts` (428 righe a mano) deriva in silenzio (`api.get<T>` è solo un cast). Campi già serviti e mai tipizzati: `BacktestSummary.risk_free_rate_pct`, `ClosedTrade.sector`.

**Files:**
- `backend/etoro_bot/api/server.py` — modelli Pydantic `response_model` almeno sulle ~10 route che portano denaro (`/portfolio`, `/trades`, `/trade-history`, `/executions`, `/arena*`, `/live*`, `/backtest/*`)
- `frontend/lib/types.ts` — allineare (aggiungere `risk_free_rate_pct`, `sector`; `direction` arriva dal Task 1.1)
- Opzionale se il tempo lo consente: script `openapi-typescript` in CI documentato in `docs/api.md`
- Test: `tsc --noEmit` verde; snapshot OpenAPI contiene gli schemi.

### Task 3.5 ∥: pulizia percorsi morti e degradi silenziosi minori

**Files/Steps:**
- [ ] `services/backtest.py:211-223` — `_capital_changes` legge la chiave audit `bot_capital_usd` che non esiste più (`app_settings.py:30`): rimuovere il no-op (o, se si vuole il TWR corretto, alimentarlo dai delta di `credit` fra snapshot equity — scelta da motivare nel report)
- [ ] `db/repo.py:130-143` — `count_filled_today` senza chiamanti e con confronto date fragile (`func.date` in tz di sessione): rimuovere
- [ ] `domain.py:23-24` + `repo.py:83-88` — enum `DecisionStage` a un membro mai istanziato: rimuovere, `stage: str`
- [ ] `etoro/client.py:125-128` — `_trading_segment` ritorna sempre `""` (docstring lo ammette): eliminare property e interpolazioni nei 5 path (righe 333,348,373,406,448)
- [ ] `repo.py:243,264,479` — `closed_positions()`, `equity_series()`, `sim_equity_series()` senza `limit`, servite in polling a 15s: aggiungere limiti ragionevoli + downsampling serie equity; `server.py:1117` lineage N+1 → una query aggregata
- [ ] Pytest + ruff verdi.

---

## Fase 4 — Frontend (∥ dopo Fase 1; Task 2.4/2.5 già coprono i fix di sicurezza)

### Task 4.1: SearchableSelect a11y + debounce ricerca + polling training

(a) `components/ui/searchable-select.tsx:117-155`: pattern combobox ARIA incompleto — manca `aria-controls` trigger→listbox e `aria-activedescendant` sull'input che rifletta l'opzione evidenziata. (b) `app/trades/page.tsx:81-82` e `app/history/page.tsx:33-35`: la ricerca entra nella query key a ogni keystroke → una fetch per carattere. (c) `app/training/page.tsx:48,377`: overview a 15s + un `useArenaAgent` a 15s per card = polling N+1.

**Files:**
- `components/ui/searchable-select.tsx` — id per opzione, `aria-activedescendant` sull'input, `aria-controls` sul trigger
- Hook `useDebouncedValue` condiviso (300ms) in `frontend/hooks/`, usato da trades e history prima della query key
- `app/training/page.tsx` — togliere `refetchInterval` dal per-agent hook (eredita staleness dall'overview); i due agenti attuali non giustificano endpoint batch

**Acceptance:** lint + tsc verdi; una sola fetch dopo pausa di digitazione (verificabile nei test o manualmente); screen reader annuncia l'opzione evidenziata (attributi presenti nel DOM).

---

## Fase 5 — Semplificazione e refactor (solo dopo Fasi 1–4 verdi)

### Task 5.1: estrarre servizi dal monolite `api/server.py` + router

`server.py` è 1282 righe con ~380 righe di logica di dominio nelle route (`/trades` e `/trade-history` fondono posizioni+esecuzioni con parsing filtri duplicato quasi verbatim :729-786 vs :843-917; `/portfolio` calcola PnL/equity/sizing :334-410; `_agent_payload` :1043-1081). I commenti a blocchi esistenti corrispondono 1:1 a router.

**Files:**
- Nuovi `backend/etoro_bot/services/journal.py` (fusione trades/history + `_parse_statuses` condiviso) e `services/portfolio.py`
- Nuovi `backend/etoro_bot/api/routers/*.py` (portfolio, trades, backtest, knowledge, settings, arena, live, safety) montati in `server.py`; le route restano serializzazione pura
- NESSUN cambio funzionale: payload byte-identici (verificare con i test API esistenti)

**Acceptance:** pytest verde; `server.py` sotto ~300 righe; nessuna route cambia path o payload.

### Task 5.2: dedup backend

**Steps:**
- [ ] `arena/metrics.py` vs `services/backtest.py`: drawdown/Sharpe/volatilità reimplementati due volte (statistics vs numpy) → modulo unico `backend/etoro_bot/services/stats.py` su liste di float, chiamato da entrambi (attenzione: parità numerica, test di regressione sui valori attuali)
- [ ] `arena/live.py:579-599` vs `engine.py:136-154`: loop SL/TP live reimplementa `auto_risk_closes` → adattare la funzione condivisa (passare `direction` per posizione, che dal Task 1.1 è persistita)
- [ ] `services/universe.py:92-96` vs `knowledge/tickers.py:82-87`: regex ticker espliciti duplicate e già divergenti (`{1,6}` vs `{1,5}`) → costante condivisa
- [ ] `knowledge/safe_fetch.py:80-105`: mixin `_PinnedConnectionMixin` per le due classi connection
- [ ] `api/server.py`: alzare in testa al file gli import lazy non necessari a rompere cicli (tenere solo quelli motivati, con commento)

**Acceptance:** pytest verde; nessun cambio di comportamento (parità numerica documentata nel report per stats).

### Task 5.3: dedup frontend

**Steps:**
- [ ] Sei pagine ricostruiscono la coppia `<Table>` desktop + `<MobileList>` mobile con campi ripetuti due volte (`app/page.tsx:338-400`, `trades:123-184`, `history:55-81`, `portfolio:145-217`, `settings:318-377`, `backtest:389-491`): estrarre `<ResponsiveTable>` che deriva le `MobileField` dalle colonne, migrare le sei pagine
- [ ] `TradesCard` duplicata fra `training/[agentId]/page.tsx:301-379` e `training/confronto/page.tsx:247-325`: estrarre `<SimTradesTable trades showAgentColumn?>` in `components/arena.tsx`
- [ ] `DnaGrid` (`components/arena.tsx:47-73`) vs `StatGrid` (`training/[agentId]/page.tsx:99-116`): un solo `<LabelValueGrid>`
- [ ] `uploadDocument` (`lib/queries.ts:381-400`) reimplementa la gestione errori di `handle()` (`lib/api.ts:13-40`): esportare `handle` o aggiungere `api.postForm`

**Acceptance:** lint + tsc + `npm run build` verdi; resa visiva invariata (le sei pagine mostrano le stesse colonne/campi di prima).

---

## Fase 6 — Test di regressione mirati (∥ con Fase 5, rebase sui file spostati)

### Task 6.1: suite concorrenza e divergenza broker/DB

Le lacune più gravi rilevate: zero test con `threading`/`concurrent.futures`, zero test "broker OK, DB KO", kill switch testato solo a inizio ciclo e non per-ordine (`live.py:649`).

**Files:** `backend/tests/test_concurrency.py`, estensioni a `test_arena_live.py`, `test_safety.py`

**Steps:**
- [ ] `run_live_cycle` non esegue in overlap sotto thread reali (`_live_lock`)
- [ ] `RateLimiter.acquire` sotto accesso multi-thread reale (oggi solo sequenziale)
- [ ] Kill switch attivato A METÀ ciclo → nessun ordine successivo parte (controllo per-ordine nel loop di apertura)
- [ ] Breaker: integrazione con settle quando equity non disponibile (comportamento post Task 3.1)
- [ ] `require_owner` con repo che solleva → 503 (post Task 2.1)
- [ ] I test dei Task 1.2/1.3/1.4 restano nei rispettivi task; qui solo ciò che manca ancora dopo le Fasi 1–3

**Acceptance:** pytest verde; le nuove classi di test falliscono se si reintroducono i bug (verifica con revert locale a campione di un fix, documentata nel report).

---

## Fuori piano (decisione utente richiesta, NON implementare)

- **Migrazione colonne monetarie `Float` → `Numeric(18,4)` + `Decimal`** (`db/models.py`, tutti i calcoli di cassa/PnL): corretta in linea di principio ma migrazione invasiva su dati live; da valutare a mercati chiusi con backup.
- **Kill switch azionabile (solo attivazione) da utenti autenticati non-owner**: scelta di prodotto fail-safe, non un bug.
- **Rate limiting applicativo sugli endpoint**: rischio basso dietro Authentik; eventualmente middleware Traefik, non codice.
- **`pip-audit` + `npm audit` in CI**: raccomandato, ma non esiste ancora una CI nel repo.
