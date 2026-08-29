# Piano di miglioramento Trading 3.x — idee da Kronos, Vibe-Trading, TradingAgents, nautilus_trader

> **Nota per gli esecutori:** questo è un piano-roadmap, non un piano di implementazione.
> Copre sottosistemi indipendenti: ogni fase, quando viene presa in carico, va espansa nel suo
> piano TDD dettagliato con `superpowers:writing-plans` prima di toccare codice.

**Data:** 2026-08-29 · **Branch di riferimento:** dev-3.0

> **STATO (2026-08-29): ESEGUITO.** Tutte le fasi implementate su dev-3.0
> (commit da `90d6edd` a `43f8a36`), 367 test verdi. Restano fuori, come da
> piano: 3.2 fine-tuning e 3.3 candele sintetiche (solo se il replay
> giustifica Kronos), tool-calling completo (opzione futura di 2.6),
> event-bus nautilus (nei non-goal). Kronos e evolution_pool sono opt-in
> in settings.yaml; il vecchio valore della password Arango va ruotato.

**Obiettivo:** portare il bot da "1 chiamata LLM su 4 scalari + torneo a 2 agenti senza costi di
transazione" a un sistema con valutazione onesta delle strategie, pipeline decisionale
multi-stadio, segnali quantitativi veri e guardrail di rischio strutturali — riusando i pattern
delle quattro repo analizzate.

**Fonti analizzate:**

| Repo | Cosa è | Licenza | Cosa prendiamo |
|---|---|---|---|
| [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos) | Foundation model per candele OHLCV (tokenizer BSQ + transformer, 4M–102M param, HuggingFace `NeoQuasar/*`) | MIT | Forecast prezzo/volatilità, ranking cross-sezionale, scenari Monte-Carlo |
| [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | Workspace agentico di ricerca finanziaria (ReAct loop, 74 tool MCP, swarm YAML, mandate live) | MIT | Grounding gate, Mandate fail-closed, decay delle strategie, Verdict contract, memoria a emivita |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Framework multi-agente LLM (LangGraph: analisti → dibattito bull/bear → trader → dibattito rischio) | Apache 2.0 | Pipeline a stadi, rating a 5 livelli, riflessione su alpha realizzato, LLM a due tier |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) (upstream del fork tossys01) | Piattaforma algo-trading event-driven Rust+Python | LGPL v3 | Parità backtest/live, risk engine pre-trade, contabilità event-sourced, state machine ordini. **Solo pattern, non dipendenza**: niente adapter eToro, LGPL, vuole possedere il runtime |

**Gap principali della codebase (dall'analisi di dev-3.0):**

1. La sim dell'arena non ha costi di transazione → il PnL che decide chi va live con soldi veri è sistematicamente ottimistico, e il bias cresce con la frequenza di trading che la pressione evolutiva spinge in alto.
2. Zero backtesting: nessun DNA viene valutato prima di rischiare denaro; l'unica valutazione è un mese di calendario con N=2.
3. Il prompt del trader vede solo prezzo, day%, week%, distanza SMA20 e 220 caratteri di news. Volume scaricato e buttato. La KB ibrida (ArangoDB + BM25/cosine + grafo) è costruita ma quasi scollegata; `add_trade_memory`/`search_trade_memory` mai chiamate.
4. Nessun risk engine pre-trade: `trader.enforce()` è solo contabilità; il DNA può mettere stop a 0; `risk_rules.yaml` non esiste e i default entrano in silenzio.
5. Output LLM senza schema: `extract_json()` best-effort, `[]` su qualunque errore, nessuna astensione/confidenza, nessuna validazione dei numeri citati.
6. `db/models.py::Decision.stage` prevede già `analyst|debate|portfolio|risk|reconcile_anomaly` — lo schema per una pipeline a dibattito esiste, la pipeline no (tutto scrive `stage="trader"`).
7. Password ArangoDB hardcoded in `knowledge/kb.py:27` (`DEFAULT_ARANGO_PASS`) e nel default del compose.
8. `get_rates`/`get_instruments` = 1 chiamata HTTP per strumento → ~80 chiamate/ciclo su un pool 120/60s.
9. Docs drift (`docs/architettura.md` descrive ancora Qdrant + 4 container) e leftovers (`data/qdrant/`, `backend/build/`, `REPORTS_DIR`).

---

## Fase 0 — Igiene (prerequisiti, effort S)

### 0.1 Secret ArangoDB fuori dal codice
- **Gap:** #7. **Intervento:** rimuovere `DEFAULT_ARANGO_PASS` da `knowledge/kb.py`; obbligo di `ARANGO_PASS` da env (fail all'avvio se assente e KB abilitata, coerente con il pattern già usato per `X-Trading-Internal-Token`); ruotare la password reale; variabile in `.env` documentata.
- **File:** `backend/etoro_bot/knowledge/kb.py`, `docker-compose.yml`, `.env`.

### 0.2 Pulizia e docs
- **Gap:** #9. **Intervento:** cancellare `data/qdrant/` e `backend/build/`; rimuovere i riferimenti a `REPORTS_DIR`; riscrivere `docs/architettura.md` sullo stato reale (container unico, ArangoDB, taxonomy).
- **File:** `docs/architettura.md`, `Dockerfile`/compose per eventuali residui.

### 0.3 Batch dei rate eToro
- **Gap:** #8. **Intervento:** se l'API supporta id multipli in `get_rates`, batchare; altrimenti cache breve (30–60 s) condivisa per ciclo. Riduce pressione sul rate limiter prima di aggiungere consumatori (Kronos, replay).
- **File:** `backend/etoro_bot/etoro/client.py`, `arena/market.py`.

---

## Fase 1 — Valutazione onesta (nautilus_trader) — la fase a più alto valore

Prima di rendere gli agenti più intelligenti, bisogna rendere onesto il meccanismo che decide
chi va live. Tutto il resto del piano poggia su questa fase.

### 1.1 Costi di transazione nella sim
- **Fonte:** nautilus (fill model con slippage), Vibe-Trading (engine per-mercato con stack costi reale).
- **Gap:** #1. **Intervento:** in `repo.open_sim_position`/`close_sim_position` applicare spread (metà spread per lato, stimabile dal vincolo max-spread 1% già usato nello screening universo, o per-strumento dai dati eToro), commissione/fee fissa configurabile e costo overnight per gli short sintetici. Parametri in `settings.yaml::arena.costs.{spread_pct,fee_usd,short_overnight_pct}` con default prudenti ≠ 0.
- **File:** `backend/etoro_bot/db/repo.py`, `config/settings.yaml`, test in `backend/tests/test_arena_engine.py`.
- **Effort:** S. **Impatto:** alto — corregge la selezione evolutiva alla radice.

### 1.2 Harness di replay storico (parità backtest/live)
- **Fonte:** nautilus — stesso engine, si scambiano solo i bordi (Clock + DataClient + ExecutionClient).
- **Gap:** #2. **Intervento:** `run_training_cycle` consuma dict a forma di `build_snapshot()` e `get_candles` fornisce già il path OHLCV: costruire `arena/replay.py` che (a) scarica/serve candele storiche, (b) le trasforma in snapshot per ogni bar, (c) fa girare `run_training_cycle` con un clock simulato, senza modifiche all'engine. Aggiungere astrazione `Clock` minima (wall vs simulato) usata da engine e scheduler.
- **Uso:** valutare un DNA su N mesi storici prima della promozione; walk-forward per l'evoluzione (1.4); stress test.
- **Attenzione (lezione TradingAgents, issue #203):** look-ahead bias — nel replay il canale news/memoria va troncato alla data simulata o disattivato; mai passare `memory_context` corrente su dati passati.
- **File:** nuovo `backend/etoro_bot/arena/replay.py`, ritocco `arena/engine.py` (iniezione clock), router `backtest` per lanciare replay.
- **Effort:** M. **Impatto:** alto.

### 1.3 Risk engine pre-trade esplicito + Mandate
- **Fonte:** nautilus `RiskEngine` (validazioni pre-trade, trading-state gate) + Vibe-Trading Mandate (dataclass congelata, letta al boot, l'agente non ha path di scrittura, fail-closed).
- **Gap:** #4. **Intervento:** nuovo stadio `safety/mandate.py`: dataclass frozen caricata al boot da `config/risk_rules.yaml` (che oggi **non esiste** — crearlo, ed eliminare il default silenzioso di `load_breaker_rules()`): max notional per ordine, max esposizione totale e per strumento, max ordini/giorno, floor per stop-loss del DNA (uno stop a 0 diventa violazione, non scelta genetica), whitelist/denylist strumenti, stato trading `ACTIVE|REDUCING|HALTED`. Ogni ordine (sim e live) passa dal mandate **dopo** `trader.enforce()` e **prima** dell'esecuzione; rifiuto = evento `OrderDenied` con codice motivo nel journal, mai eccezione. Il kill switch esistente diventa la transizione a `HALTED`; il breaker a `REDUCING`.
- **Punto chiave (Vibe-Trading):** i bound del DNA restano "rappresentabilità", il mandate è la policy di rischio — separazione strutturale, non config che l'LLM può influenzare.
- **File:** nuovo `backend/etoro_bot/safety/mandate.py`, `config/risk_rules.yaml`, hook in `arena/engine.py` e `arena/live.py`, test dedicati.
- **Effort:** M. **Impatto:** alto — è la rete di sicurezza sotto tutte le fasi successive.

### 1.4 Valutazione evolutiva walk-forward
- **Fonte:** nautilus (backtest node), Vibe-Trading (decay state machine).
- **Gap:** #2. **Intervento:** in `arena/evolution.py::maybe_evolve`, prima di promuovere il vincitore a campione live, farlo girare nel replay (1.2) su una finestra out-of-sample recente; promozione solo se supera soglie (Sharpe minimo, max-DD massimo — riusare `services/backtest.py` per le metriche). I mutanti nascono già pre-validati in replay invece che direttamente sul mese live.
- **File:** `backend/etoro_bot/arena/evolution.py`.
- **Effort:** S (dopo 1.2). **Impatto:** alto.

---

## Fase 2 — Decisione LLM più ricca (TradingAgents + Vibe-Trading)

### 2.1 Più segnali nel snapshot
- **Fonte:** TradingAgents (≤8 indicatori non ridondanti scelti per categoria), nautilus (indicatori stateful registrati).
- **Gap:** #3. **Intervento:** `market.py::metrics_from_closes` è l'unico insertion point: aggiungere ATR(14) normalizzato, RSI(14), volatilità realizzata 20d, volume relativo (il volume arriva già da `get_candles` e viene scartato), eventualmente MACD. Tenerli pochi e non ridondanti; entrano nel prompt come riga compatta per simbolo.
- **File:** `backend/etoro_bot/arena/market.py`, `arena/trader.py` (prompt), test.
- **Effort:** S. **Impatto:** medio-alto.

### 2.2 Output strutturato + rating a 5 livelli + astensione
- **Fonte:** TradingAgents (`Buy/Overweight/Hold/Underweight/Sell`, parsing deterministico, "reserve Hold for genuinely balanced evidence"), Vibe-Trading (Verdict contract: parse rigido, malformato = `contract_violation`, mai indovinare).
- **Gap:** #5. **Intervento:** sostituire `extract_json()` best-effort con schema Pydantic della decisione: rating a 5 livelli (mappato su sizing: Overweight = frazione ridotta di `max_position_pct`), `confidence`, `reasoning`, possibilità esplicita di astenersi. Parse fallito → decisione nulla registrata come violazione di contratto nel journal (visibile in UI), non `[]` silenzioso.
- **File:** `backend/etoro_bot/arena/trader.py`, `llm.py`, `db/models.py` (campo rating), frontend `trades`.
- **Effort:** S-M. **Impatto:** medio-alto.

### 2.3 Pipeline multi-stadio con dibattito
- **Fonte:** TradingAgents (architettura completa), Vibe-Trading (preset swarm: ruoli con prompt che pretendono output falsificabili, "what would disprove this").
- **Gap:** #6 — `Decision.stage` è già pronto. **Intervento:** trasformare la singola chiamata di `trader.decide` in pipeline per ciclo:
  1. **Analyst** (tier economico): report strutturato per i simboli candidati (tecnico + news dalla KB);
  2. **Bull vs Bear** (1 round, cap deterministico come TradingAgents: mai loop aperti);
  3. **Trader** (tier profondo): decisione col rating 2.2, vede i report — pattern fan-in su stato tipizzato, non transcript chat;
  4. **Risk judge** (tier profondo): può solo attenuare o porre veto, mai aumentare l'esposizione; scrive `stage="risk"`.
  Due tier di modello (`llm.model_quick` + `llm.model_deep` in settings) — è la leva di costo principale di TradingAgents. Ogni stadio persiste la sua `Decision` con lo stage corretto → il journal e le pagine `training` mostrano il ragionamento completo.
- **Nota costi:** con cicli da 15 min la pipeline completa può girare solo sui simboli con segnale (pre-filtro da 2.1/3.1), non sugli 80.
- **File:** `backend/etoro_bot/arena/trader.py` (split in moduli `arena/pipeline/`), `arena/engine.py`, `config/settings.yaml`.
- **Effort:** L. **Impatto:** alto.

### 2.4 Grounding gate deterministico
- **Fonte:** Vibe-Trading `grounding.py` — l'idea singola migliore di quella repo.
- **Gap:** #5. **Intervento:** validatore senza LLM tra risposta e esecuzione: ogni numero (prezzo, %) e simbolo citato nel `reasoning`/decisione deve esistere nello snapshot passato nel prompt; simbolo mai visto o prezzo in contraddizione → rifiuto con il conflitto specifico rimandato al modello, max 2 retry, poi astensione forzata. Complementare alla sanitizzazione injection già presente (`untrusted.py`): quella protegge l'input, questo l'output.
- **File:** nuovo `backend/etoro_bot/arena/grounding.py`, hook in `trader.decide`.
- **Effort:** S-M. **Impatto:** medio.

### 2.5 Riflessione outcome-graded su alpha
- **Fonte:** TradingAgents `Reflector` — decisione pending → ritorno realizzato + **alpha vs benchmark** → lezione di 2-4 frasi iniettata nei prompt futuri.
- **Intervento:** upgrade di `engine._reflect()`: oggi il diario è una riscrittura libera; agganciare ogni chiusura di posizione al suo esito (ritorno raw + alpha vs SPY, calcolabile con il fetcher già in `services/backtest.py`) e far generare una lezione breve per trade, archiviata append-only (pattern `TradingMemoryLog`: markdown, `[data | ticker | rating | esito]`, recupero deterministico ultime 5 stesso-ticker + 3 cross-ticker — niente embeddings). Il diario dell'agente pesca da lì.
- **File:** `backend/etoro_bot/arena/engine.py`, nuovo `arena/reflection.py`, riuso `services/backtest.py`.
- **Effort:** M. **Impatto:** medio-alto.

### 2.6 Attivare la KB nelle decisioni
- **Gap:** #3 — infrastruttura già costruita e ferma. **Intervento minimo:** alzare il canale da 220 caratteri: nel report Analyst (2.3) entrare con `kb.search_news()` (ibrido BM25+cosine già pronto) e `get_ticker_graph_context()` per esteso; cablare finalmente `add_trade_memory`/`search_trade_memory` sulle chiusure (in coppia con 2.5). **Opzione successiva:** tool-calling vero (l'LLM interroga KB/candele da sé, stile Vibe-Trading) — ma solo dopo la pipeline, e con la lezione di TradingAgents in mente: i dati social/rumorosi conviene pre-fetcharli nel prompt, il loro analyst con tool allucinava.
- **File:** `backend/etoro_bot/arena/market.py`, `knowledge/kb.py`, pipeline 2.3.
- **Effort:** S (pre-fetch) / M (tool-calling). **Impatto:** medio.

### 2.7 Memoria con decay a emivita
- **Fonte:** Vibe-Trading (decay esponenziale, emivita 14 giorni, boost all'accesso).
- **Intervento:** `ticker_memory.py` ha già retention 30d/40 entry; aggiungere peso di importanza con decay esponenziale + boost quando una entry viene richiamata, così il `summary` LLM pesa le notizie per rilevanza attuale e non solo per età. Allineato al recency-decay già usato in `search_news`.
- **File:** `backend/etoro_bot/knowledge/ticker_memory.py`.
- **Effort:** S. **Impatto:** basso-medio.

---

## Fase 3 — Kronos come motore quantitativo

Prerequisiti: 1.1–1.2 (senza valutazione onesta un segnale in più è solo rumore in più).

### 3.1 Servizio di forecast Kronos
- **Modello:** partire da `Kronos-small` (24.7M, contesto 512) o `Kronos-mini` (4.1M, contesto 2048) — girano su CPU, MIT, pesi da HuggingFace `NeoQuasar/*`; si vendorizza la directory `model/` (niente pacchetto pip).
- **Intervento:** modulo `forecast/kronos.py` con `KronosPredictor.predict_batch()` su tutto l'universo (stessa lookback/pred_len per tutti — perfetto per lo scan degli 80 simboli): servono più candele di oggi (`CANDLES=21` → ~200+ daily; il cache 1h esiste già). Output per simbolo nel snapshot:
  - `kronos_ret_pred` — ritorno atteso a N barre (close predetto vs ultimo close);
  - `kronos_rank` — percentile cross-sezionale del ritorno predetto (il framing RankIC del paper: il segnale è più credibile come ranking che come forecast puntuale);
  - `kronos_up_prob` + `kronos_vol_pred` — da `sample_count=N` path Monte-Carlo: frazione di path in rialzo e dispersione (proxy volatilità).
- **Usi:** (a) pre-filtro dei simboli che entrano nella pipeline 2.3 (top/bottom decile del rank); (b) campi nel prompt; (c) `kronos_vol_pred` per sizing e distanza stop nel risk judge.
- **Cautele (dal report):** accuratezza direzionale zero-shot vicina al coin-flip su singolo asset — usarlo cross-sezionalmente e **validarlo nel replay 1.2 prima di dargli peso**; output stocastico (fissare seed); girarlo in job schedulato (non nel percorso critico del ciclo) e cachare per ciclo.
- **File:** nuovo `backend/etoro_bot/forecast/` (vendored model + wrapper), `arena/market.py`, `services/scheduler.py` (job), `backend/pyproject.toml` (torch — attenzione al peso dell'immagine Docker: valutare extra opzionale).
- **Effort:** M-L. **Impatto:** medio-alto (da verificare empiricamente — il replay decide).

### 3.2 Fine-tuning per l'universo eToro (opzionale, dopo 3.1)
- La pipeline `finetune_csv/` di Kronos accetta CSV `timestamps,open,high,low,close,volume,amount` — esattamente ciò che `get_candles` fornisce. Fine-tuning di Kronos-small sui ticker della watchlist se lo zero-shot delude nel replay. Richiede GPU NVIDIA; sessione one-shot, non runtime.

### 3.3 Candele sintetiche per stress test (opzionale)
- Generazione di scenari sintetici realistici con Kronos per stress-testare i DNA nel replay (crash, regimi volatili) oltre lo storico disponibile.

---

## Fase 4 — Evoluzione arena più robusta

### 4.1 Decay state machine per i campioni
- **Fonte:** Vibe-Trading `strategy_store/decay.py` (active→monitoring→decayed→disabled su bande Sharpe/IC con contatori di violazioni consecutive).
- **Intervento:** il campione live oggi resta in carica fino al torneo successivo. Aggiungere monitoraggio rolling (Sharpe 30d, max-DD) del campione: sotto soglia per K giorni consecutivi → stato `monitoring` (sizing ridotto via mandate `REDUCING`), poi `decayed` → stop live e torneo anticipato. Metriche già disponibili in `services/backtest.py`.
- **File:** `backend/etoro_bot/arena/evolution.py`, `safety/mandate.py`.
- **Effort:** M. **Impatto:** medio-alto (protegge il capitale reale dal decadimento silenzioso dell'edge).

### 4.2 Popolazione più ampia via replay
- N=2 su un mese è statisticamente debolissimo. Col replay (1.2) i tornei generazionali possono girare su storico con 4–8 varianti di DNA a costo LLM contenuto (tier economico), promuovendo al mese live solo i 2 migliori. L'arena live resta a 2 — cambia solo il vivaio.
- **File:** `arena/evolution.py`, `arena/replay.py`.
- **Effort:** M (dopo 1.2).

### 4.3 Briefing con Verdict contract
- **Fonte:** Vibe-Trading `scheduled_research` — output che termina con sezione `## Verdict` parse-abile (`- SYMBOL: STATE - reason`), malformato = violazione, mai indovinato.
- **Intervento:** l'EOD (`run_eod`) e il news job producono già testo libero; aggiungere il blocco Verdict machine-readable così la UI (`benchmark`, `history`) rende le chiamate del giorno senza ri-parsare prosa.
- **File:** `arena/engine.py` (EOD), frontend.
- **Effort:** S. **Impatto:** basso (qualità della vita).

---

## Cosa NON fare (deliberato)

- **Non adottare nautilus_trader come dipendenza:** niente adapter eToro, LGPL, framework che vuole possedere il runtime — si copiano i pattern. Idem Vibe-Trading (monolite ~2.700 file) e TradingAgents (niente layer esecuzione): si estraggono le idee, non il codice.
- **Non introdurre LangGraph:** la pipeline 2.3 sono 4 chiamate sequenziali con cap fissi — orchestrazione a mano, zero dipendenze nuove.
- **Non fare event-bus/message-bus completo stile nautilus:** il refactor event-driven totale non ripaga finché il sistema resta a cadenza 15 min; l'astrazione `Clock` + replay (1.2) dà l'80% del valore (parità backtest/live) con il 20% del lavoro. Rivalutare solo se si scende a cadenza intraday spinta.
- **Non usare ChromaDB/embeddings per la memoria trade:** TradingAgents stessa è regredita a markdown append-only con retrieval deterministico; ArangoDB+fastembed resta per le news dove già c'è.

## Ordine consigliato e razionale

```
0.1 → 0.2 → 0.3          (igiene, 1 sessione)
1.1 → 1.3 → 1.2 → 1.4    (valutazione onesta + rete di sicurezza)
2.1 → 2.2 → 2.4 → 2.5 → 2.6-prefetch → 2.7   (arricchimento incrementale, ogni voce indipendente)
2.3                       (pipeline dibattito — dopo 2.1/2.2, è il pezzo grosso)
3.1 (→ 3.2/3.3 se il replay lo giustifica)
4.1 → 4.2 → 4.3
```

La logica: prima si rende **onesto** il metro di giudizio (costi, replay, mandate), poi si rende
**più intelligente** il giudicato (segnali, pipeline, Kronos), infine si rende **più robusta**
l'evoluzione. Invertire l'ordine significherebbe selezionare agenti più bravi a sfruttare i bias
del simulatore.

## Rischi trasversali (lezioni dalle repo)

- **Look-ahead bias** (TradingAgents issue #203): nel replay ogni fonte informativa va troncata alla data simulata; gli LLM sono comunque pre-addestrati sul periodo di backtest — i risultati storici vanno letti come upper bound.
- **Non-determinismo LLM/Kronos:** fissare seed dove possibile, valutare su più run, mai su una singola passata.
- **Costi LLM:** la pipeline 2.3 moltiplica le chiamate — due tier di modello + pre-filtro dei simboli sono obbligatori, non opzionali.
- **Peso Docker:** torch per Kronos gonfia l'immagine unificata — extra opzionale in `pyproject.toml` o container separato se necessario.
