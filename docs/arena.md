# L'arena evolutiva

Due agenti LLM rivali, stesso capitale di partenza, un mese di tempo. A fine
mese sopravvive **uno solo**, e solo se ha chiuso in profitto. Il sopravvissuto
diventa campione (il suo DNA muove denaro reale), viene clonato e il clone viene
mutato: la generazione successiva è lui stesso più una sua variante.

## Il DNA

Il DNA è un dict JSON persistito su `agents.dna`. I bound numerici esistono solo
per tenere i valori rappresentabili (niente NaN, negativi, percentuali assurde):
**non sono una politica di rischio**. Fonte: `arena/dna.py`.

### Geni numerici

| Gene | Min | Max | Intero | Default | Significato |
|---|---|---|---|---|---|
| `conviction_scale` | 0.1 | 5.0 | no | 1.5 | moltiplicatore della size proposta dall'LLM |
| `max_positions` | 1 | 60 | sì | 20 | posizioni simultanee massime |
| `max_orders_per_cycle` | 1 | 40 | sì | 12 | aperture massime in un singolo ciclo |
| `max_position_pct` | 1.0 | 100.0 | no | 35.0 | % dell'equity su una singola posizione |
| `stop_loss_pct` | 0.0 | 90.0 | no | 6.0 | **0 = nessuno stop automatico** |
| `take_profit_pct` | 0.0 | 300.0 | no | 12.0 | **0 = nessun take profit automatico** |
| `min_cash_pct` | 0.0 | 90.0 | no | 0.0 | riserva di liquidità che l'agente vuole tenere |
| `max_holding_days` | 1 | 60 | sì | 10 | 1 = intraday puro, >1 = swing su più sedute |

### Geni booleani

| Gene | Default | Significato |
|---|---|---|
| `allow_long` | `true` | acquisto autorizzato |
| `allow_short` | `true` | vendita allo scoperto autorizzata |
| `allow_pyramiding` | `true` | più posizioni sullo stesso simbolo |

Un agente che si vieta **entrambe** le direzioni non è una strategia: `clamp_dna`
gli restituisce d'ufficio il long.

### Geni testuali

- `risk_profile`, uno tra: `prudente`, `bilanciato`, `aggressivo`,
  `spregiudicato`, `iperattivo`. Default: **`aggressivo`**.
- `strategy`, testo libero. Il default descrive operatività ad alta frequenza in
  entrambe le direzioni, orizzonte libero, molte operazioni, e chiude con
  «l'unico errore fatale è restare fermo e arrivare a fine mese senza profitto».

`clamp_dna()` completa i campi mancanti dal default, riporta i numeri dentro i
bound (arrotondati a 2 decimali, o interi dove previsto), normalizza i booleani
anche da stringa (`"false"`, `"0"`, `"no"`, `"off"`, `""` ⇒ falso) e valida il
profilo di rischio.

## Il credo di sopravvivenza

`survival_creed(floor_pct)` è la parte **fissa** della memoria di ogni agente,
riscritta a ogni riflessione serale. Dichiara quattro cose:

- **SOPRAVVIVENZA** — a fine mese chi ha guadagnato di più sopravvive; chiudere
  a zero o in negativo è morte comunque, anche se il rivale fa peggio.
- **MORTE IMMEDIATA** — sotto il pavimento di bancarotta (default 25% del
  capitale iniziale) l'agente è eliminato all'istante, senza aspettare fine mese.
- **LIBERTÀ** — long e short, intraday e swing, quante operazioni vuole,
  concentrare o frammentare. Nessun filtro esterno veta le sue decisioni. Tra una
  generazione e l'altra può riscrivere da zero strategia e parametri.
- **RITMO** — «chi opera poco non trova l'edge: l'inerzia è la forma di morte più
  comune».

## Mutazione: auto-riscrittura via LLM

`dna.mutate()` clona il DNA e lo muta. **Con l'LLM disponibile la mutazione è
un'auto-riscrittura**: l'agente figlio ridisegna sé stesso.

Il prompt (`rewrite_prompt`) mostra al modello strategia, parametri, permessi e
bound correnti, e gli dice esplicitamente che:

- short e swing sono autorizzati quanto long e intraday;
- può alzare o **azzerare i propri freni** (`stop_loss_pct = 0`,
  `take_profit_pct = 0`, `min_cash_pct = 0`, `max_orders_per_cycle` alto);
- deve preferire una versione **più attiva** della precedente, salvo ragione
  esplicita per rallentare;
- l'unico limite intoccabile è la sopravvivenza;
- deve introdurre una variazione **significativa** (direzione preferita,
  selezione titoli, orizzonte, frequenza, gestione delle perdite).

Risposta attesa:
`{"strategy": "<max 90 parole>", "risk_profile": "...", "genes": {…}}`.
Se il modello risponde con testo non JSON, il testo viene comunque adottato come
nuova strategia. Se la chiamata fallisce si passa al fallback.

### Fallback senza LLM

Jitter numerico, volutamente ampio: ogni gene ha probabilità 0.8 di essere
mutato con uno scarto fino a ±60%; il profilo di rischio cambia con probabilità
0.5; ogni booleano si inverte con probabilità 0.2. Se nessun gene viene estratto,
uno viene forzato.

In ogni caso il figlio non è **mai** identico al genitore: se dopo il clamp
coincidesse, `conviction_scale` viene comunque alterato.

## Il ciclo di decisione

Una sola chiamata LLM per agente per ciclo. Il prompt (`trader.build_prompt`)
contiene, nell'ordine: identità, contesto di sopravvivenza, strategia e profilo
dal DNA, l'elenco esplicito di ciò che l'agente **è autorizzato a fare**
(direzioni, orizzonte, aperture per ciclo, posizioni, size, riserva, gestione
automatica, piramidazione), la memoria, il conto, le posizioni aperte, lo
snapshot di mercato.

Il prompt dichiara a chiare lettere: *«Nessun altro filtro ti ferma: non esistono
soglie di confidence, cooldown o veti esterni. Ciò che proponi entro questi
numeri viene eseguito.»*

Risposta attesa:

```json
[{"action": "open|close", "symbol": "AAPL", "direction": "long|short",
  "size_pct": 25.0, "reason": "..."}]
```

Sulle chiusure `direction` è opzionale: omessa significa «chiudi tutto quel
simbolo». Sinonimi accettati per la direzione: short/sell/vendi/ribasso/bear/
down/corto e long/buy/compra/rialzo/bull/up/lungo. Qualunque errore di parsing
o di rete si traduce in «nessuna azione», mai in un'eccezione che ferma il ciclo.

### Enforce: cosa viene davvero scartato

`trader.enforce` non ha opinioni. Scarta solo ciò che non è eseguibile:

- **chiusure**: passano se il simbolo è tra quelli detenuti, punto;
- **aperture**: scartate se il simbolo non è nello snapshot di mercato, se la
  direzione è vietata dal DNA dell'agente stesso, se la piramidazione è
  disattivata e il simbolo è già in portafoglio (o già pianificato in questo
  ciclo), se si supererebbe `max_positions` o `max_orders_per_cycle`, o se
  l'importo calcolato scende sotto **10 USD** (`MIN_ORDER_USD`, sotto il minimo
  eseguibile dal broker).

Calcolo dell'importo:

```
size_pct  = quello proposto (default 25% se assente o ≤ 0), tetto 100%
requested = cash_iniziale_del_ciclo × size_pct/100 × conviction_scale
reserve   = equity × min_cash_pct/100
spendable = cash_residuo − reserve
amount    = min(requested, equity × max_position_pct/100, spendable)
```

`size_pct` è riferita al cash che l'agente vedeva nel prompt (quello a inizio
ciclo); la disponibilità residua fa solo da tetto man mano che gli ordini
consumano il cash.

## Contabilità dello short

Il libro mastro simulato conosce solo posizioni «lunghe» (`sim_positions` ha
`units` e `entry_price`, niente colonna direzione). Lo short è rappresentato con
due accorgimenti:

1. **marcatore** — la `open_reason` viene prefissata con `[SHORT]`
   (`tag_reason` / `position_direction`);
2. **prezzo specchiato** — in valutazione e chiusura si passa al libro mastro
   `effective_price = max(2 × entry_price − price, 0)`.

Uno short entrato a *E* e chiuso a *P* vale quindi come un long chiuso a
*2E − P*: il PnL è `units × (E − P)`, esattamente quello atteso. Il `max(…, 0)`
significa che se il titolo **raddoppia** il collaterale è bruciato e la
posizione vale zero — non si va sotto zero.

Conseguenze:

- `position_change_pct` restituisce la variazione **a favore dell'agente**:
  per uno short è il segno opposto della variazione di prezzo, così stop loss e
  take profit funzionano identici nelle due direzioni;
- `agent_equity` = cash + somma dei valori mark-to-market; una posizione senza
  prezzo disponibile vale il suo costo;
- l'apertura scala il cash dell'importo pieno, short compresi (il collaterale è
  modellato come impegno di liquidità);
- non ci sono costi di prestito titoli, dividendi, slippage né commissioni: la
  simulazione è **ottimistica** rispetto al mercato reale.

Nel live, invece, lo short è un ordine vero: `open_position` invia
`transaction: "sell"` a eToro. `live.short_kwargs` ispeziona la firma del client
per trovare il parametro di direzione (`direction`, `side`, `transaction`,
`is_buy`, `buy`); se non lo trova solleva `ShortNotSupported` e l'ordine viene
registrato come `skipped` — **mai convertito in long**. La direzione delle
posizioni reali si legge dal campo `isBuy` del portafoglio eToro.

## Nascita, morte, evoluzione

### Bootstrap

`bootstrap_if_needed` crea una generazione quando non c'è **nessun** agente vivo:

- primo avvio: generazione 1 dal DNA di default;
- se i predecessori sono morti in corso di mese (bancarotta): la vita riparte
  dalla generazione successiva, con il DNA del campione se esiste, altrimenti
  quello dell'ultimo agente noto. La selezione non si azzera.

I due figli sono `G<n>-Alfa` (clone del DNA base) e `G<n>-Beta` (mutazione).
Capitale: `arena.starting_capital_eur` (10.000 €) convertito in USD col tasso
corrente; se il servizio cambi non risponde, 1:1 con un warning. Viene emesso un
evento `birth`.

### Bancarotta

A fine di ogni ciclo, `_enforce_survival_floor`: se

```
equity ≤ starting_capital_usd × arena.survival_floor_pct / 100
```

tutte le posizioni vengono liquidate (all'ultimo prezzo noto, o al prezzo di
ingresso se manca), l'agente è marcato morto con motivo «bancarotta» e viene
emesso un evento `death`. Con `survival_floor_pct: 0` il controllo è disattivato.

Questo è **l'unico veto di sistema** sul conto simulato: non modera le
decisioni, decide solo quando la partita è finita.

### Selezione mensile

`evolution.maybe_evolve` gira al primo tick di ogni giorno ed è un no-op finché
il mese memorizzato in `app_settings["arena"]["month"]` coincide con quello
corrente. Quando cambia:

1. **liquidazione forzata** di tutte le posizioni aperte, agli ultimi prezzi noti
   (`only_open=False`): la valutazione si fa a conti chiusi, anche per gli swing
   trader che tengono posizioni overnight;
2. PnL di ciascun agente = equity − capitale iniziale, ordinamento decrescente;
3. **sopravvive il primo, ma solo se il suo PnL è > 0**. Se anche il migliore è
   in pari o in perdita, muoiono tutti;
4. gli altri vengono uccisi con motivo esplicito (`PnL mensile ≤ 0` oppure
   `sconfitto dal rivale`);
5. il sopravvissuto viene marcato `evolved` (ritirato) e nominato **campione**;
   il suo DNA e la sua memoria diventano la base della generazione successiva;
6. se non sopravvive nessuno, la base è il DNA dell'ultimo campione storico
   (o il default), e la memoria riparte dal solo credo di sopravvivenza;
7. nascono due figli: clone + mutante, entrambi con capitale fresco;
8. evento `evolution` con il riepilogo.

Il campione resta `is_champion = true` anche mentre continua a esistere come
riga storica: è il DNA che il trading live usa a ogni ciclo.

## Pausa e controllo manuale

- `POST /arena/pause` / `POST /arena/resume` — il ciclo di training si ferma;
  lo stato vive in `app_settings["arena"]["paused"]`.
- `POST /arena/cycle` — forza un ciclo di training adesso (test/monitoraggio).
- `POST /live/enable` / `POST /live/disable` — accende e spegne il trading su
  denaro reale. L'attivazione richiede chiavi eToro configurate, un campione
  esistente, kill switch non attivo, breaker non scattato e
  `confirmation: true`.

Tutte richiedono l'identità del proprietario (vedi [sicurezza.md](sicurezza.md)).
