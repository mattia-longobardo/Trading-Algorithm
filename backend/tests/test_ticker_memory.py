"""Test della memoria evolutiva per ticker: filesystem isolato, LLM fake."""

from datetime import datetime, timedelta, timezone

import pytest

from etoro_bot.knowledge import ticker_memory as tm

NO_LLM = {"knowledge": {"ticker_memory": {"use_llm": False}}}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    yield tmp_path


def _item(text, tickers, published_at="", source="feed"):
    return {"text": text, "tickers": tickers, "published_at": published_at,
            "source": source, "kind": "news"}


def test_update_creates_memory_with_fallback_summary():
    updated = tm.update_memories(
        [_item("Apple presenta il nuovo iPhone", ["AAPL"])], NO_LLM
    )
    assert updated == {"AAPL": 1}
    memory = tm.load_memory("AAPL")
    assert memory["ticker"] == "AAPL"
    assert len(memory["entries"]) == 1
    assert "nuovo iPhone" in memory["summary"]
    assert memory["summary_source"] == "headline"
    assert "nuovo iPhone" in tm.memory_context("AAPL")


def test_update_deduplicates_and_appends():
    items = [_item("Apple presenta il nuovo iPhone", ["AAPL"])]
    tm.update_memories(items, NO_LLM)
    assert tm.update_memories(items, NO_LLM) == {}  # stesso testo: nessuna novità
    tm.update_memories([_item("Apple batte le stime sugli utili", ["AAPL"])], NO_LLM)
    memory = tm.load_memory("AAPL")
    assert [len(memory["entries"])] == [2]


def test_retention_drops_old_entries():
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    tm.update_memories([_item("Notizia vecchissima su Apple", ["AAPL"], published_at=old)], NO_LLM)
    assert tm.load_memory("AAPL") is None or tm.load_memory("AAPL")["entries"] == []
    # una notizia fresca entra e quella oltre retention non ricompare
    tm.update_memories(
        [_item("Notizia fresca su Apple", ["AAPL"]),
         _item("Altra notizia vecchissima", ["AAPL"], published_at=old)],
        NO_LLM,
    )
    memory = tm.load_memory("AAPL")
    assert [e["text"] for e in memory["entries"]] == ["Notizia fresca su Apple"]


def test_llm_summary_updates_memory():
    def fake_llm(system_blocks, user_prompt, model, max_tokens):
        assert "Memoria attuale" in user_prompt and "AAPL" in user_prompt
        return "Tema persistente: cicli iPhone. Rischio: domanda Cina."

    tm.update_memories(
        [_item("iPhone forte in Cina", ["AAPL"])],
        {"knowledge": {"ticker_memory": {"use_llm": True}}},
        llm=fake_llm,
    )
    memory = tm.load_memory("AAPL")
    assert memory["summary"].startswith("Tema persistente")
    assert memory["summary_source"] == "llm"


def test_llm_failure_falls_back_to_headlines():
    def broken_llm(**kwargs):
        raise RuntimeError("niente chiave API")

    tm.update_memories([_item("Apple lancia un buyback", ["AAPL"])], llm=broken_llm,
                       settings={"knowledge": {"ticker_memory": {"use_llm": True}}})
    memory = tm.load_memory("AAPL")
    assert memory["summary_source"] == "headline"
    assert "buyback" in memory["summary"]


def test_documents_and_unsafe_tickers_are_ignored(tmp_path):
    doc = {"text": "analisi caricata", "tickers": ["AAPL"], "kind": "document",
           "published_at": "", "source": "upload"}
    assert tm.update_memories([doc], NO_LLM) == {}
    assert tm.update_memories([_item("exploit", ["../EVIL"])], NO_LLM) == {}
    assert list(tmp_path.rglob("*.json")) == []


def test_disabled_memory_is_noop():
    settings = {"knowledge": {"ticker_memory": {"enabled": False}}}
    assert tm.update_memories([_item("news", ["AAPL"])], settings) == {}
    assert tm.load_memory("AAPL") is None


def test_all_memories_sorted():
    tm.update_memories(
        [_item("news su Microsoft", ["MSFT"]), _item("news su Apple", ["AAPL"])], NO_LLM
    )
    assert [m["ticker"] for m in tm.all_memories()] == ["AAPL", "MSFT"]


# ------------------------------------------------------------- decay (2.7)


def test_entry_weight_halves_at_half_life():
    from etoro_bot.knowledge.ticker_memory import entry_weight

    now = 1_000_000.0
    fresh = {"ts": now}
    old = {"ts": now - 14 * 86400}
    assert entry_weight(fresh, now) == 1.0
    assert abs(entry_weight(old, now) - 0.5) < 1e-9


def test_entry_weight_access_boost_beats_recency():
    from etoro_bot.knowledge.ticker_memory import entry_weight

    now = 1_000_000.0
    old_but_used = {"ts": now - 14 * 86400, "access_count": 12}
    fresh_never_used = {"ts": now - 1 * 86400}
    assert entry_weight(old_but_used, now) > entry_weight(fresh_never_used, now)


def test_memory_context_records_access(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    from etoro_bot.knowledge.ticker_memory import load_memory, memory_context

    path = tmp_path / "ticker_memory"
    path.mkdir()
    now = _time.time()
    (path / "AAPL.json").write_text(_json.dumps({
        "ticker": "AAPL", "summary": "sintesi",
        "entries": [
            {"id": "a", "ts": now - 3600, "date": "2026-08-29", "text": "news 1"},
            {"id": "b", "ts": now - 7200, "date": "2026-08-29", "text": "news 2"},
        ],
    }), encoding="utf-8")

    class NoKB:
        available = False

    text = memory_context("AAPL", kb=NoKB())
    assert "news 1" in text
    memory = load_memory("AAPL")
    counts = {e["id"]: e.get("access_count", 0) for e in memory["entries"]}
    assert counts == {"a": 1, "b": 1}
