"""Knowledge base: stato, feed RSS, news, ingest documenti."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from etoro_bot.api import dependencies as deps
from etoro_bot.api.dependencies import UserIdentity, current_user, require_owner
from etoro_bot.config import load_settings

log = logging.getLogger("etoro_bot.api")

router = APIRouter()


def _rss_feeds(identity: UserIdentity) -> list[str]:
    stored = deps.get_repo().get_setting(deps.user_setting_key("rss", identity.user_id))
    if isinstance(stored, list):
        return [str(item) for item in stored]
    feeds = load_settings().get("news_feeds", {})
    return list(feeds.get("generic", [])) + list(feeds.get("per_ticker", []))



@router.get("/knowledge/status")
def knowledge_status(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    st = deps.kb().status()
    st["rss_feeds"] = _rss_feeds(identity)
    st["last_fetch"] = deps.last_fetch_marker(identity)
    return st


@router.get("/knowledge/graph")
def knowledge_graph(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    """Ritorna lo stato e la panoramica del Financial Knowledge Graph."""
    kb = deps.kb()
    st = kb.status()
    return {
        "enabled": bool(st.get("graph")),
        "graph_name": st.get("graph"),
        "search_view": st.get("search_view"),
        "collections": st.get("collections", {}),
    }


@router.get("/knowledge/graph/{symbol}")
def knowledge_ticker_graph(
    symbol: str, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    """Ritorna il sub-grafo causale e le news collegate per un ticker specifico."""
    kb = deps.kb()
    return kb.get_ticker_graph_context(symbol, max_depth=1, news_limit=3)


class RssFeedsBody(BaseModel):
    feeds: list[str]



@router.put("/knowledge/rss-feeds")
def update_rss_feeds(
    body: RssFeedsBody, identity: UserIdentity = Depends(current_user)
) -> dict[str, Any]:
    from etoro_bot.knowledge.safe_fetch import UnsafeUrlError, assert_public_url

    require_owner(identity, "cambiare i feed RSS")
    feeds: list[str] = []
    for raw in body.feeds:
        value = raw.strip()
        if not value:
            continue
        if not value.startswith(("https://", "http://")):
            raise HTTPException(422, f"URL feed non valido: {value}")
        # Il backend scarica questi URL da dentro la rete Docker: un feed che
        # punta a un servizio interno lo trasformerebbe in una sonda, con la
        # risposta indicizzata e poi leggibile dalla pagina News.
        try:
            assert_public_url(value)
        except UnsafeUrlError as exc:
            raise HTTPException(422, f"URL feed non ammesso: {exc}") from exc
        if value not in feeds:
            feeds.append(value)
    if len(feeds) > 50:
        raise HTTPException(422, "Massimo 50 feed RSS")
    deps.get_repo().set_setting(deps.user_setting_key("rss", identity.user_id), feeds, source="api")
    return {"rss_feeds": feeds}


def _news_file(identity: UserIdentity) -> Path:
    return deps.user_state_file(identity, "latest_news.json")



@router.get("/news")
def latest_news(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    path = _news_file(identity)
    if not path.exists():
        return {"items": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "updated_at": None}
    return payload



@router.post("/knowledge/fetch-news", status_code=202)
def knowledge_fetch_news(identity: UserIdentity = Depends(current_user)) -> dict[str, Any]:
    # La pipeline indicizza nella knowledge base *globale*, quella che alimenta
    # i prompt del trader: non è uno stato personale dell'utente.
    require_owner(identity, "aggiornare le news")

    def _job() -> None:
        try:
            from etoro_bot.knowledge.fetch_news import fetch_all
            from etoro_bot.knowledge.pipeline import run_news_pipeline

            settings = load_settings()
            settings["news_feeds"] = {"generic": _rss_feeds(identity), "per_ticker": []}
            items = fetch_all(settings)
            run_news_pipeline(
                kb=deps.kb(), settings=settings, items=items,
                client=deps.system_etoro_client(), llm=deps.system_llm(),
            )
            updated_at = datetime.now(UTC).isoformat()
            _news_file(identity).write_text(
                json.dumps({"items": items[:60], "updated_at": updated_at}, ensure_ascii=False),
                encoding="utf-8",
            )
            deps.mark_fetch(identity)
            log.info("fetch news: %d item indicizzati", len(items))
        except Exception:
            log.exception("fetch news fallito")

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "accepted"}


_UPLOAD_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt")



@router.post("/knowledge/ingest")
async def knowledge_ingest(
    file: UploadFile = File(...),
    identity: UserIdentity = Depends(current_user),
) -> dict[str, Any]:
    from etoro_bot.knowledge.ingest import ingest_upload
    from etoro_bot.knowledge.parsers import UnsupportedFileTypeError

    require_owner(identity, "caricare documenti nella knowledge base")
    filename = file.filename or ""
    if not filename.lower().endswith(_UPLOAD_SUFFIXES):
        raise HTTPException(
            415,
            f"estensione non supportata per '{filename}': estensioni ammesse "
            f"{', '.join(_UPLOAD_SUFFIXES)}",
        )

    content = await file.read()
    try:
        outcome = ingest_upload(filename, content, kb=deps.kb())
    except (UnsupportedFileTypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    safe_name = Path(filename).name
    upload_dir = Path(os.environ.get("KNOWLEDGE_BASE_DIR", "/app/knowledge_base")) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(identity.user_id.encode("utf-8")).hexdigest()[:12]
    (upload_dir / f"{digest}-{safe_name}").write_bytes(content)
    return {
        "filename": filename,
        "chunks_indexed": outcome.chunks,
        "tickers": outcome.tickers,
    }
