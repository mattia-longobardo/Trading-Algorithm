# Configurazione

## Precedenza

```
app_settings (PostgreSQL)  >  config/settings.yaml  >  default in codice
```

Dal database arrivano **solo** `timezone`, `currency` e lo stato dell'arena
(`month`, `paused`, `live_enabled`): sono le uniche chiavi che l'API può
modificare a runtime. Tutto il resto (watchlist, feed, discovery, knowledge, llm,
parametri dell'arena) si legge dal YAML.

`_full_settings()` in `api/server.py` compone le due sorgenti:
`{**load_settings(), **get_effective()}`.

`config/` è montato **read-only** nel container (`./config:/app/config:ro`): per
cambiare un valore si edita il file sull'host e si riavvia il backend.

---

## `config/settings.yaml`

### Presentazione e metriche

| Chiave | Default | Significato |
|---|---|---|
| `timezone` | `Europe/Rome` | **solo presentazione**: lo scheduling è sempre in UTC. Validata come timezone IANA |
| `currency` | `USD` | valuta di visualizzazione. I dati restano in USD, la conversione avviene al rendering |
| `risk_free_rate_pct` | `5.0` | tasso privo di rischio annuo (T-bill 3M) usato da Sharpe e Sortino |
| `benchmark_symbol` | `SPY` | benchmark della curva equity e di alpha/beta/information ratio |

### `arena`

| Chiave | Default | Significato |
|---|---|---|
| `starting_capital_eur` | `10000` | budget mensile di **ciascun** agente, convertito in USD al tasso corrente (1:1 con warning se i cambi non sono raggiungibili) |
| `cycle_minutes` | `15` | minuti tra un ciclo di decisioni e il successivo, mentre almeno una borsa è aperta |
| `max_symbols` | `80` | titoli mostrati ai trader in ogni snapshot di mercato |
| `survival_floor_pct` | `25` | equity sotto questa % dell'iniziale ⇒ bancarotta immediata. `0` disattiva il controllo |
| `markets.<nome>.open_utc` / `close_utc` | `europe` 07:00–15:30, `usa` 13:30–20:00 | sessioni di borsa in UTC (orari estivi). A fine sessione si chiudono solo le posizioni che hanno esaurito il `max_holding_days` del DNA |

Il default in codice, se `arena.markets` manca, è lo stesso EU+USA
(`services/scheduler.DEFAULT_SESSIONS`). È accettata anche la vecchia forma
`market_open_utc` / `market_close_utc`, interpretata come sola sessione `usa`.

### `watchlist`

Elenco di simboli eToro (`symbolFull`). Per le borse europee il suffisso indica
la piazza: `.MI` Milano, `.DE` Xetra, `.PA` Parigi, `.NV` Amsterdam, `.MC`
Madrid, `.L` Londra, `.ZU` Zurigo, `.CO` Copenaghen… Senza punto = USA.
Il file ne contiene un centinaio tra azioni USA/UE ed ETF.

`arena/market.py::market_of_symbol` usa il suffisso per decidere a quale
sessione appartiene un titolo. Suffissi riconosciuti come europei: `MI DE PA NV
MC L BR LS ST OL HE CO VI ZU`.

### `universe_discovery`

Scoperta dinamica di titoli oltre la watchlist. Default in
`services/universe.DEFAULTS`; il YAML li sovrascrive.

| Chiave | Default codice | Valore nel YAML | Significato |
|---|---|---|---|
| `enabled` | `true` | `true` | attiva la discovery |
| `mode` | `llm` | `llm` | `llm` (scout + fallback regex) oppure `regex` (solo citazioni esplicite) |
| `size` | `5` | `8` | titoli dinamici tenuti oltre la watchlist |
| `llm_max_proposals` | `10` | `10` | tetto alle proposte dello scout |
| `min_confidence` | `0.5` | `0.5` | confidence minima di una proposta LLM |
| `keep_misses` | `3` | `3` | refresh senza riconferma prima di uscire (isteresi anti-churn) |
| `min_mentions` | `2` | `2` | item news distinti per candidarsi in modalità regex |
| `max_evaluated` | `20` | `20` | candidati ammessi allo screening (limita le chiamate API) |
| `min_price_usd` | `5.0` | `5.0` | niente penny stock |
| `min_history_days` | `120` | `120` | candele giornaliere minime: niente IPO fresche |
| `min_avg_dollar_volume_usd` | `20 000 000` | `20000000` | controvalore medio scambiato (ultime 20 sedute) |
| `max_annualized_vol_pct` | `60.0` | `60` | volatilità annualizzata massima (log-return, ultime ~60 sedute) |
| `max_spread_pct` | `1.0` | `1.0` | spread bid/ask massimo, quando calcolabile |
| `news_half_life_days` | `3.0` | `3.0` | peso delle citazioni: dimezza ogni N giorni |
| `max_age_days` | `7` | *(assente)* | oltre questa età lo stato persistito è ignorato e la discovery si considera ferma |

Lo stato vive in `$STATE_DIR/discovered_universe.json`.

### `knowledge`

| Chiave | Default | Significato |
|---|---|---|
| `news_half_life_days` | `7.0` | in ricerca sulla KB le news dimezzano il peso ogni N giorni |
| `news_max_age_days` | `45` | oltre: eliminate da `news_kb` dalla pipeline (i documenti caricati restano) |
| `ticker_memory.enabled` | `true` | attiva la memoria per titolo |
| `ticker_memory.retention_days` | `30` | le notizie escono dalla memoria dopo N giorni |
| `ticker_memory.max_entries` | `40` | tetto alle voci conservate per titolo |
| `ticker_memory.use_llm` | `true` | sintesi evolutiva via LLM; senza, fallback alle ultime 5 headline |

### `llm`

| Chiave | Default | Significato |
|---|---|---|
| `model` | `gpt-5.6-terra` | modello usato da trader, scout universo, riflessioni e memorie |
| `max_tokens` | `2048` | tetto di token in risposta (`max_completion_tokens`) |

Il client OpenAI è creato con `timeout = 60 s` e `max_retries = 1`
(`llm.LLM_TIMEOUT_S` / `LLM_MAX_RETRIES`): nessuna chiamata LLM può tenere fermo
uno stop loss.

### `news_feeds`

- `generic`: elenco di URL RSS/Atom scaricati così come sono.
- `per_ticker`: template con `{ticker}`, applicati a **tutto l'universo
  effettivo** (watchlist + titoli scoperti).

Ogni utente può sovrascrivere l'elenco dalla UI (`PUT /knowledge/rss-feeds`,
massimo 50 feed); l'override è salvato in `app_settings` con chiave
`rss:<sha256(user_id)[:32]>`. Ogni URL viene validato contro SSRF prima di
essere accettato.

Il parser tiene al massimo 15 item per feed, rifiuta i documenti che dichiarano
una DTD (`<!DOCTYPE`, vettore XXE) e non interrompe gli altri feed se uno
fallisce.

---

## `config/risk_rules.yaml`

Unico file di freni, e sono freni di **sopravvivenza**: non moderano
l'aggressività. Tutta la gestione operativa vive nel DNA degli agenti.

| Chiave | Default | Significato |
|---|---|---|
| `circuit_breaker.max_daily_loss_pct` | `25.0` | perdita giornaliera, in % dell'equity, oltre la quale si bloccano le **aperture** (mai le chiusure). `0` disattiva |
| `circuit_breaker.max_consecutive_losses` | `0` | **disattivato**: una serie di stop non minaccia la sopravvivenza. Valori > 0 riaccendono il freno |
| `circuit_breaker.cooloff_hours` | `1` | durata del blocco dopo lo scatto |

Le chiavi sconosciute vengono ignorate (`load_breaker_rules` filtra sui campi
noti della dataclass).

---

## Variabili d'ambiente

### Richieste dal deployment (`.env` + `docker-compose.yml`)

| Variabile | Servizio | Obbligatoria | Note |
|---|---|---|---|
| `TZ` | tutti | consigliata | `Europe/Rome`. Solo presentazione |
| `POSTGRES_USER` | postgres, backend | sì | entra in `DATABASE_URL` |
| `POSTGRES_DB` | postgres, backend | sì | entra in `DATABASE_URL` |
| `DB_TRADING_PASSWORD` | postgres, backend | sì | password del DB |
| `TRADING_HOST` | frontend | sì | hostname Traefik e `AUTH_URL` |
| `AUTHENTIK_HOST` | frontend | sì | compone `AUTH_AUTHENTIK_ISSUER` |
| `AUTH_AUTHENTIK_ID` | frontend | sì | client id OIDC |
| `AUTH_AUTHENTIK_SECRET` | frontend | sì | client secret OIDC |
| `AUTH_SECRET` | frontend, backend | sì | segreto di sessione Auth.js; ripiego per la cifratura credenziali |
| `TRADING_CREDENTIALS_SECRET` | backend | consigliata | segreto dedicato alla cifratura (PBKDF2). Se vuoto vale `AUTH_SECRET` |
| `TRADING_INTERNAL_TOKEN` | frontend, backend | consigliata | vuoto = controllo disattivato (solo sviluppo locale) |

### Impostate dal compose nel container backend

| Variabile | Valore nel compose | Default in codice | Uso |
|---|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…@trading-postgres:5432/…` | `postgresql+psycopg://bot:bot@localhost:5432/etoro_bot` | connessione SQLAlchemy |
| `QDRANT_URL` | `http://trading-qdrant:6333` | `http://localhost:6333` | knowledge base vettoriale |
| `CONFIG_DIR` | `/app/config` | `<repo>/config` | dove cercare i due YAML |
| `KILL_SWITCH_DIR` | `/app/state` | `.` | file `KILL_SWITCH`, `circuit_breaker.json`, stato per utente |
| `STATE_DIR` | `/app/state` | `/app/state` | universo scoperto, memorie ticker, cache cambi |
| `KNOWLEDGE_BASE_DIR` | `/app/knowledge_base` | `/app/knowledge_base` | documenti caricati (`uploads/`) |
| `REPORTS_DIR` | `/app/reports` | — | cartella report (montata; non letta dal codice attuale) |
| `FASTEMBED_CACHE_DIR` | `/app/state/fastembed_cache` | idem | cache dei modelli di embedding |
| `HF_HOME`, `XDG_CACHE_HOME` | `/app/state/...` | — | tengono le cache dentro il volume di stato |

### Variabili del frontend

`BACKEND_URL` (default `http://trading-backend:8000`), `TRADING_INTERNAL_TOKEN`,
`AUTH_SECRET`, `AUTH_TRUST_HOST`, `AUTH_URL`, `AUTH_AUTHENTIK_ID`,
`AUTH_AUTHENTIK_SECRET`, `AUTH_AUTHENTIK_ISSUER`. Nessuna ha prefisso
`NEXT_PUBLIC`: il token non finisce mai nel bundle del browser.

### Variabili operative e di sviluppo

| Variabile | Effetto |
|---|---|
| `ETORO_BOT_KILL=1` | kill switch attivo, **non disattivabile da codice**: vince sempre sul file |
| `DISABLE_SCHEDULER=1` | il backend parte senza avviare APScheduler (test, debug) |
| `TEST_DATABASE_URL` | i test usano questo Postgres invece di avviarne uno effimero via Docker |
| `ETORO_API_KEY`, `ETORO_USER_KEY` | lette **solo** dalla CLI `python -m etoro_bot.services.universe`. Il server non le usa mai |
| `OPENAI_API_KEY` | usata solo se `make_openai_client()` viene invocato senza chiave esplicita (percorsi CLI). Il server passa sempre la chiave cifrata dell'utente |

### Chiavi API personali

`ETORO_API_KEY`, `ETORO_USER_KEY` e `OPENAI_API_KEY` **non** vanno nel `.env` del
deployment. Si inseriscono da *Impostazioni → Chiavi API personali*: il backend
le cifra e le salva in `user_credentials`, legate all'identità Authentik. Vedi
[sicurezza.md](sicurezza.md#credenziali-personali-cifrate).

---

## Impostazioni modificabili a runtime

`PUT /settings` accetta **solo** due chiavi (`services/app_settings._MUTABLE_KEYS`):

| Chiave | Validazione | Effetto |
|---|---|---|
| `timezone` | timezone IANA valida | solo come vengono mostrati date e orari nella UI |
| `currency` | tra le 31 valute supportate da `services/fx.SUPPORTED_CURRENCIES` | solo la conversione di presentazione |

Qualsiasi altra chiave produce un 422. Ogni cambio è tracciato in
`settings_audit` (chiave, valore vecchio, valore nuovo, sorgente).

Lo stato dell'arena (`paused`, `live_enabled`) ha endpoint dedicati e non passa
da `PUT /settings`.

### Cambi valuta

I tassi arrivano da [Frankfurter](https://frankfurter.dev) (riferimenti BCE,
nessuna API key), con cache in memoria e su disco (`$STATE_DIR/fx_rates.json`),
TTL 6 ore. Se il servizio non risponde si tiene l'ultimo tasso noto marcato
`stale: true`; nel caso peggiore resta la sola identità USD→USD e la UI mostra
dollari dichiarandolo, invece di inventare un cambio.

---

## Limiti di frequenza eToro

`etoro/rate_limiter.py` fa pacing preventivo per pool condivisi, usando l'**80%**
del budget dichiarato:

| Pool | Budget | Endpoint |
|---|---|---|
| `execution` | 20 / 60 s | apertura e chiusura posizioni |
| `trading-info` | 60 / 60 s | portafoglio, lookup ordini |
| `market-data` | 120 / 60 s | catalogo, prezzi, candele |
| `default` | 60 / 60 s | trade history |

Politica di errore del client: 429 ⇒ rispetta `Retry-After`; 5xx ⇒ backoff
esponenziale; 4xx (≠429) ⇒ errore immediato senza retry. Massimo 3 tentativi.
Le chiavi API non compaiono mai nei log né nei messaggi d'errore.
