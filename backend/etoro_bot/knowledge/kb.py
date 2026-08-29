"""RAG e Financial Knowledge Graph su ArangoDB 3.12 + fastembed (BAAI/bge-small-en-v1.5, 384 dim).

Include:
  1. Ricerca Ibrida (ArangoSearch View `news_search_view`): unisce BM25 fulltext + cosine similarity vettoriale + decadimento temporale.
  2. Financial Knowledge Graph (`market_graph`, `market_nodes`, `market_edges`): relazioni tra ticker, settori, filiera e macro-fattori.
  3. GraphRAG Cross-Asset Context: arricchimento causale per prompt trading (contagio tra fornitori/competitor/settori).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ARANGO_URL = "http://arangodb:8529"
DEFAULT_ARANGO_DB = "trading"
DEFAULT_ARANGO_USER = "root"

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
VECTOR_SIZE = 384

NEWS_COLLECTION = "news_kb"
TRADE_MEMORY_COLLECTION = "trade_memory"
MARKET_NODES_COLLECTION = "market_nodes"
MARKET_EDGES_COLLECTION = "market_edges"
MARKET_GRAPH_NAME = "market_graph"
NEWS_SEARCH_VIEW = "news_search_view"

# Decadimento temporale delle news in ricerca: il peso dimezza ogni half-life.
DEFAULT_NEWS_HALF_LIFE_DAYS = 7.0
_LEGACY_AGE_HALF_LIVES = 2.0


def parse_published_ts(value: str) -> float | None:
    """Timestamp epoch da una data RSS (RFC 822) o Atom/ISO 8601; None se illeggibile."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def sanitize_node_key(key: str) -> str:
    """Rende una stringa una chiave ArangoDB valida (lettere, numeri, underscore, trattini)."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(key).strip())


def news_payload(item: dict, now: float | None = None) -> dict:
    """Payload di un item: testo, fonte, ticker, tipo e timestamp."""
    now = now if now is not None else time.time()
    kind = str(item.get("kind") or "news")
    published_ts = parse_published_ts(str(item.get("published_at") or ""))
    if published_ts is None and kind == "news":
        published_ts = now
    return {
        "text": str(item.get("text") or "").strip(),
        "source": item.get("source", ""),
        "tickers": [str(t).upper().strip() for t in (item.get("tickers") or []) if str(t).strip()],
        "published_at": item.get("published_at", ""),
        "kind": kind,
        "published_ts": published_ts,
        "indexed_ts": now,
    }


def recency_weight(
    payload: dict, half_life_days: float, now: float | None = None
) -> float:
    """Peso ∈ (0,1] del punto in ricerca: le news vecchie contano meno."""
    if str(payload.get("kind") or "news") == "document":
        return 1.0
    now = now if now is not None else time.time()
    half_life = max(float(half_life_days), 0.1)
    ts = payload.get("published_ts") or payload.get("indexed_ts")
    if ts is None:
        return 0.5 ** _LEGACY_AGE_HALF_LIVES
    age_days = max(0.0, (now - float(ts)) / 86400.0)
    return 0.5 ** (age_days / half_life)


def rerank_by_recency(
    results: list[dict], half_life_days: float, now: float | None = None
) -> list[dict]:
    """Riordina i risultati per score semantico × peso di recency."""
    reranked = []
    for item in results:
        weight = recency_weight(item, half_life_days, now)
        reranked.append(
            {
                **item,
                "raw_score": item.get("score"),
                "recency_weight": round(weight, 4),
                "score": (item.get("score") or 0.0) * weight,
            }
        )
    reranked.sort(key=lambda r: r["score"], reverse=True)
    return reranked


def point_id_for_text(text: str) -> str:
    """Id punto deterministico dal testo (SHA1 → UUID): stesso testo, stesso id."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest[:32]))


class KnowledgeBase:
    """Accesso unificato a Vettori, Full-text ArangoSearch e Financial Knowledge Graph su ArangoDB."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("ARANGO_URL", DEFAULT_ARANGO_URL)
        self.db_name = os.environ.get("ARANGO_DB", DEFAULT_ARANGO_DB)
        self.username = os.environ.get("ARANGO_USER", DEFAULT_ARANGO_USER)
        # Nessun default: il secret arriva solo dall'ambiente. Senza password la KB
        # degrada a no-op (stesso comportamento di Arango irraggiungibile).
        self.password = os.environ.get("ARANGO_PASSWORD")
        self.available = False
        self._warned = False
        self._client: Any = None
        self._db: Any = None
        self._embedder: Any = None
        if not self.password:
            self._degrade("ARANGO_PASSWORD non impostata: KB disabilitata")
            return
        try:
            from arango import ArangoClient

            self._client = ArangoClient(hosts=self.url)
            self._db = self._client.db(self.db_name, username=self.username, password=self.password)
            self._db.collections()
            self.available = True
        except Exception as exc:
            self._degrade(f"ArangoDB non disponibile su {self.url}/{self.db_name}: {exc}")

    def _degrade(self, reason: str) -> None:
        self.available = False
        if not self._warned:
            self._warned = True
            logger.warning("KnowledgeBase in modalità degradata: %s", reason)

    def _embed(self, text: str) -> list[float] | None:
        try:
            if self._embedder is None:
                from fastembed import TextEmbedding

                cache_dir = os.environ.get("FASTEMBED_CACHE_DIR", "/app/state/fastembed_cache")
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                self._embedder = TextEmbedding(model_name=EMBEDDING_MODEL, cache_dir=cache_dir)
            raw_vec = next(iter(self._embedder.embed([text])))
            return [float(x) for x in raw_vec]
        except Exception as exc:
            self._degrade(f"fastembed non disponibile: {exc}")
            return None

    def _search(
        self,
        collection: str,
        query: str,
        limit: int,
        tickers: list[str] | None = None,
    ) -> list[dict]:
        if not self.available:
            self._degrade("ricerca ignorata (KB non disponibile)")
            return []
        vector = self._embed(query)
        if vector is None:
            return []
        try:
            aql = f"""
            FOR doc IN @@collection
              FILTER (@tickers == null OR LENGTH(INTERSECTION(doc.tickers, @tickers)) > 0)
              LET score = COSINE_SIMILARITY(doc.vector, @query_vector)
              SORT score DESC
              LIMIT @limit
              RETURN MERGE(doc, {{ score: score }})
            """
            bind_vars = {
                "@collection": collection,
                "query_vector": vector,
                "tickers": tickers if tickers else None,
                "limit": limit,
            }
            cursor = self._db.aql.execute(aql, bind_vars=bind_vars)
            return list(cursor)
        except Exception as exc:
            self._degrade(f"ricerca su {collection} fallita: {exc}")
            return []

    def _upsert(self, collection: str, docs: list[dict]) -> None:
        try:
            if not self._db.has_collection(collection):
                self._db.create_collection(collection)
            coll = self._db.collection(collection)
            coll.import_bulk(docs, on_duplicate="replace")
        except Exception as exc:
            self._degrade(f"upsert su {collection} fallito: {exc}")

    # -- Gestione Collections, Views & Named Graphs ----------------------------

    def ensure_collections(self) -> None:
        """Inizializza collections, ArangoSearch View e Financial Knowledge Graph."""
        if not self.available:
            self._degrade("ensure_collections ignorata (KB non disponibile)")
            return
        try:
            # 1. Document & Edge Collections
            for name in (NEWS_COLLECTION, TRADE_MEMORY_COLLECTION, MARKET_NODES_COLLECTION):
                if not self._db.has_collection(name):
                    self._db.create_collection(name)
            if not self._db.has_collection(MARKET_EDGES_COLLECTION):
                self._db.create_collection(MARKET_EDGES_COLLECTION, edge=True)

            # 2. Named Graph
            self.ensure_market_graph()

            # 3. ArangoSearch View
            self.ensure_search_view()
        except Exception as exc:
            self._degrade(f"creazione collections/view fallita: {exc}")

    def ensure_search_view(self) -> bool:
        """Crea o aggiorna la View ArangoSearch `news_search_view` su `news_kb`."""
        if not self.available:
            return False
        try:
            views = [v["name"] for v in self._db.views()]
            if NEWS_SEARCH_VIEW not in views:
                self._db.create_arangosearch_view(
                    name=NEWS_SEARCH_VIEW,
                    properties={
                        "links": {
                            NEWS_COLLECTION: {
                                "analyzers": ["identity", "text_en"],
                                "fields": {
                                    "text": {"analyzers": ["text_en", "identity"]},
                                    "source": {"analyzers": ["identity"]},
                                    "tickers": {"analyzers": ["identity"]},
                                },
                                "includeAllFields": False,
                                "trackListPositions": False,
                            }
                        }
                    },
                )
                logger.info("ArangoSearch view '%s' creata con successo", NEWS_SEARCH_VIEW)
            return True
        except Exception as exc:
            logger.warning("ensure_search_view non riuscito: %s", exc)
            return False

    def ensure_market_graph(self) -> bool:
        """Crea il grafo `market_graph` e popola la tassonomia iniziale se vuoto."""
        if not self.available:
            return False
        try:
            if not self._db.has_graph(MARKET_GRAPH_NAME):
                self._db.create_graph(
                    MARKET_GRAPH_NAME,
                    edge_definitions=[
                        {
                            "edge_collection": MARKET_EDGES_COLLECTION,
                            "from_vertex_collections": [MARKET_NODES_COLLECTION],
                            "to_vertex_collections": [MARKET_NODES_COLLECTION],
                        }
                    ],
                )
                logger.info("Named Graph '%s' creato con successo", MARKET_GRAPH_NAME)

            # Popola la tassonomia di base se non ci sono nodi
            nodes_coll = self._db.collection(MARKET_NODES_COLLECTION)
            if nodes_coll.count() == 0:
                self._seed_taxonomy()
            return True
        except Exception as exc:
            logger.warning("ensure_market_graph non riuscito: %s", exc)
            return False

    def _seed_taxonomy(self) -> None:
        """Popola nodi e archi finanziari di default definiti in `taxonomy.py`."""
        try:
            from etoro_bot.knowledge.taxonomy import DEFAULT_MARKET_NODES, DEFAULT_MARKET_EDGES

            nodes_docs = []
            for n in DEFAULT_MARKET_NODES:
                symbol = n["id"]
                key = sanitize_node_key(f"ticker_{symbol}" if n.get("type") in ("ticker", "etf") else symbol)
                nodes_docs.append(
                    {
                        "_key": key,
                        "id": symbol,
                        "name": n.get("name", symbol),
                        "type": n.get("type", "ticker"),
                        "sector": n.get("sector", ""),
                        "category": n.get("category", ""),
                        "market": n.get("market", "usa"),
                        "description": n.get("description", ""),
                    }
                )
            if nodes_docs:
                self._db.collection(MARKET_NODES_COLLECTION).import_bulk(nodes_docs, on_duplicate="update")

            edges_docs = []
            for e in DEFAULT_MARKET_EDGES:
                from_id = e["from"]
                to_id = e["to"]
                from_key = sanitize_node_key(f"ticker_{from_id}" if not from_id.startswith("sector_") and not from_id.startswith("macro_") else from_id)
                to_key = sanitize_node_key(f"ticker_{to_id}" if not to_id.startswith("sector_") and not to_id.startswith("macro_") else to_id)
                edge_key = sanitize_node_key(f"{from_key}_{e['relation']}_{to_key}")
                edges_docs.append(
                    {
                        "_key": edge_key,
                        "_from": f"{MARKET_NODES_COLLECTION}/{from_key}",
                        "_to": f"{MARKET_NODES_COLLECTION}/{to_key}",
                        "relation": e.get("relation", "REL"),
                        "weight": float(e.get("weight", 1.0)),
                    }
                )
            if edges_docs:
                self._db.collection(MARKET_EDGES_COLLECTION).import_bulk(edges_docs, on_duplicate="update")
            logger.info("Tassonomia finanziaria inizializzata: %d nodi, %d archi", len(nodes_docs), len(edges_docs))
        except Exception as exc:
            logger.warning("Errore durante il seeding della tassonomia: %s", exc)

    # -- Ricerca Ibrida (ArangoSearch) e Standard ------------------------------

    def search_news(
        self,
        query: str,
        tickers: list[str] | None = None,
        limit: int = 5,
        half_life_days: float = DEFAULT_NEWS_HALF_LIFE_DAYS,
        hybrid: bool = True,
    ) -> list[dict]:
        """Ricerca Ibrida (BM25 + Vettori + Recency) in `news_kb`."""
        if not self.available:
            self._degrade("search_news ignorata (KB non disponibile)")
            return []

        clean_tickers = [t.upper().strip() for t in tickers] if tickers else None
        now = time.time()

        # Tentativo Ricerca Ibrida nativa tramite ArangoSearch View
        if hybrid:
            try:
                vector = self._embed(query)
                aql = f"""
                FOR doc IN {NEWS_SEARCH_VIEW}
                  SEARCH (
                    @tickers == null
                    OR doc.tickers IN @tickers
                    OR ANALYZER(doc.tickers IN @tickers, "identity")
                    OR ANALYZER(PHRASE(doc.text, @query), "text_en")
                    OR ANALYZER(STARTS_WITH(doc.text, @query), "text_en")
                  )
                  LET vec_sim = (@query_vector != null AND doc.vector != null) ? COSINE_SIMILARITY(doc.vector, @query_vector) : 0.0
                  LET bm25_val = BM25(doc)
                  LET norm_bm25 = MIN([bm25_val / 12.0, 1.0])
                  LET hybrid_score = (vec_sim > 0) ? (0.7 * vec_sim + 0.3 * norm_bm25) : norm_bm25
                  LET age_days = MAX([0.0, (@now - (doc.published_ts ? doc.published_ts : doc.indexed_ts)) / 86400.0])
                  LET recency = (doc.kind == 'document') ? 1.0 : POW(0.5, age_days / @half_life)
                  LET final_score = hybrid_score * recency
                  SORT final_score DESC
                  LIMIT @limit
                  RETURN MERGE(doc, {{
                    score: final_score,
                    raw_score: hybrid_score,
                    vec_score: vec_sim,
                    bm25_score: bm25_val,
                    recency_weight: recency
                  }})
                """
                cursor = self._db.aql.execute(
                    aql,
                    bind_vars={
                        "query": query,
                        "query_vector": vector,
                        "tickers": clean_tickers,
                        "now": now,
                        "half_life": max(float(half_life_days), 0.1),
                        "limit": limit,
                    },
                )
                results = list(cursor)
                if results:
                    return results
            except Exception as exc:
                logger.debug("Ricerca ibrida ArangoSearch non riuscita (%s), fallback su vector search", exc)

        # Fallback Standard Vector + Python Rerank
        pool = self._search(NEWS_COLLECTION, query, max(limit * 4, 20), tickers=clean_tickers)
        return rerank_by_recency(pool, half_life_days, now=now)[:limit]

    # -- GraphRAG & Context Enrichment -----------------------------------------

    def get_ticker_graph_context(
        self, symbol: str, max_depth: int = 1, news_limit: int = 2
    ) -> dict[str, Any]:
        """Recupera il contesto causale a grafo (Settore, Fornitori, Competitor, News correlate)."""
        clean_symbol = str(symbol).upper().strip()
        result: dict[str, Any] = {
            "symbol": clean_symbol,
            "sector": None,
            "competitors": [],
            "suppliers": [],
            "customers": [],
            "macro": [],
            "cross_asset_news": [],
            "summary_hint": "",
        }
        if not self.available:
            return result

        try:
            node_key = sanitize_node_key(f"ticker_{clean_symbol}")
            start_id = f"{MARKET_NODES_COLLECTION}/{node_key}"

            # Query 1-2 hop su market_graph
            aql = f"""
            LET start = DOCUMENT(@start_id)
            LET neighbors = (
              FOR v, e IN 1..@max_depth ANY @start_id GRAPH '{MARKET_GRAPH_NAME}'
                RETURN DISTINCT {{
                  id: v.id,
                  name: v.name,
                  type: v.type,
                  sector: v.sector,
                  relation: e.relation,
                  is_outgoing: (e._from == @start_id)
                }}
            )
            RETURN {{ start: start, neighbors: neighbors }}
            """
            cursor = self._db.aql.execute(
                aql,
                bind_vars={"start_id": start_id, "max_depth": max_depth},
            )
            data = next(iter(cursor), {})
            start_node = data.get("start")
            neighbors = data.get("neighbors") or []

            if start_node and start_node.get("sector"):
                result["sector"] = start_node.get("sector").replace("sector_", "")

            connected_tickers: set[str] = set()
            for item in neighbors:
                nid = item["id"]
                rel = item["relation"]
                is_out = item["is_outgoing"]

                if item["type"] == "sector":
                    result["sector"] = item["name"]
                elif item["type"] == "macro":
                    result["macro"].append(item["name"])
                elif rel == "COMPETITOR_OF":
                    result["competitors"].append(nid)
                    connected_tickers.add(nid)
                elif rel == "SUPPLIER_OF":
                    if is_out:
                        result["customers"].append(nid)
                    else:
                        result["suppliers"].append(nid)
                    connected_tickers.add(nid)
                elif rel == "BELONGS_TO" and item["type"] == "ticker":
                    connected_tickers.add(nid)

            # Cerca le news più recenti sui ticker correlati
            if connected_tickers and news_limit > 0:
                recent_news = self.search_news(
                    query=clean_symbol,
                    tickers=list(connected_tickers),
                    limit=news_limit,
                    half_life_days=3.0,
                )
                for nw in recent_news:
                    nw_tickers = [t for t in (nw.get("tickers") or []) if t in connected_tickers]
                    target = nw_tickers[0] if nw_tickers else clean_symbol
                    result["cross_asset_news"].append(
                        {
                            "ticker": target,
                            "text": (nw.get("text") or "")[:120],
                            "published_at": nw.get("published_at", ""),
                        }
                    )

            # Costruisci un summary hint sintetico per il prompt
            hints = []
            if result["sector"]:
                hints.append(f"Settore: {result['sector']}")
            if result["suppliers"]:
                hints.append(f"Fornitori: {', '.join(result['suppliers'][:3])}")
            if result["customers"]:
                hints.append(f"Clienti: {', '.join(result['customers'][:3])}")
            if result["competitors"]:
                hints.append(f"Competitor: {', '.join(result['competitors'][:3])}")
            if result["cross_asset_news"]:
                top = result["cross_asset_news"][0]
                hints.append(f"Notizia {top['ticker']}: \"{top['text']}\"")
            result["summary_hint"] = " | ".join(hints)

        except Exception as exc:
            logger.debug("get_ticker_graph_context non riuscito per %s: %s", clean_symbol, exc)

        return result

    # -- Ingestione & Purge -----------------------------------------------------

    def add_news(self, items: list[dict]) -> int:
        """Indicizza news in `news_kb` e assicura i nodi dei ticker citati nel grafo."""
        if not self.available or not items:
            if not self.available:
                self._degrade("add_news ignorata (KB non disponibile)")
            return 0
        now = time.time()
        docs = []
        tickers_seen: set[str] = set()

        for item in items:
            payload = news_payload(item, now)
            if not payload["text"]:
                continue
            vector = self._embed(payload["text"])
            if vector is None:
                return 0
            doc_id = point_id_for_text(payload["text"])
            docs.append(
                {
                    "_key": doc_id,
                    "id": doc_id,
                    "vector": vector,
                    **payload,
                }
            )
            for t in payload["tickers"]:
                tickers_seen.add(t)

        if docs:
            self._upsert(NEWS_COLLECTION, docs)

        # Auto-registrazione nodi ticker dinamici nel grafo
        if tickers_seen:
            self._ensure_ticker_nodes(tickers_seen)

        return len(docs) if self.available else 0

    def _ensure_ticker_nodes(self, tickers: set[str]) -> None:
        """Crea nodi ticker in `market_nodes` se non ancora presenti."""
        try:
            coll = self._db.collection(MARKET_NODES_COLLECTION)
            to_insert = []
            for t in tickers:
                symbol = t.upper().strip()
                key = sanitize_node_key(f"ticker_{symbol}")
                if not coll.has(key):
                    to_insert.append(
                        {
                            "_key": key,
                            "id": symbol,
                            "name": symbol,
                            "type": "ticker",
                            "market": "europe" if "." in symbol else "usa",
                        }
                    )
            if to_insert:
                coll.import_bulk(to_insert, on_duplicate="ignore")
        except Exception as exc:
            logger.debug("_ensure_ticker_nodes ignorato: %s", exc)

    def purge_old_news(self, max_age_days: float) -> int:
        """Elimina da `news_kb` le news più vecchie di `max_age_days` giorni."""
        if not self.available:
            self._degrade("purge_old_news ignorata (KB non disponibile)")
            return 0
        try:
            cutoff = time.time() - float(max_age_days) * 86400.0
            aql = f"""
            FOR doc IN {NEWS_COLLECTION}
              FILTER doc.kind == 'news' AND doc.published_ts < @cutoff
              REMOVE doc IN {NEWS_COLLECTION}
              RETURN OLD
            """
            cursor = self._db.aql.execute(aql, bind_vars={"cutoff": cutoff})
            deleted = list(cursor)
            count = len(deleted)
            if count:
                logger.info("news_kb: eliminate %d news più vecchie di %s giorni", count, max_age_days)
            return count
        except Exception as exc:
            self._degrade(f"purge news fallita: {exc}")
            return 0

    def add_trade_memory(self, text: str, payload: dict) -> None:
        """Indicizza un trade chiuso in `trade_memory`."""
        if not self.available:
            self._degrade("add_trade_memory ignorata (KB non disponibile)")
            return
        vector = self._embed(text)
        if vector is None:
            return
        doc_id = point_id_for_text(text)
        self._upsert(
            TRADE_MEMORY_COLLECTION,
            [
                {
                    "_key": doc_id,
                    "id": doc_id,
                    "vector": vector,
                    "text": text,
                    **payload,
                }
            ],
        )

    def search_trade_memory(self, query: str, limit: int = 3) -> list[dict]:
        """Recupera i trade passati più simili al setup corrente."""
        return self._search(TRADE_MEMORY_COLLECTION, query, limit)

    def status(self) -> dict:
        """Stato completo per la pagina Knowledge e diagnostica."""
        if not self.available:
            return {"arango_up": False, "qdrant_up": False, "collections": {}}
        counts: dict[str, int] = {}
        try:
            for name in (NEWS_COLLECTION, TRADE_MEMORY_COLLECTION, MARKET_NODES_COLLECTION, MARKET_EDGES_COLLECTION):
                if self._db.has_collection(name):
                    counts[name] = self._db.collection(name).count()
                else:
                    counts[name] = 0
            views = [v["name"] for v in self._db.views()]
            has_graph = self._db.has_graph(MARKET_GRAPH_NAME)
            return {
                "arango_up": True,
                "qdrant_up": True,
                "collections": counts,
                "graph": MARKET_GRAPH_NAME if has_graph else None,
                "search_view": NEWS_SEARCH_VIEW if NEWS_SEARCH_VIEW in views else None,
            }
        except Exception as exc:
            self._degrade(f"status fallito: {exc}")
            return {"arango_up": False, "qdrant_up": False, "collections": {}}
