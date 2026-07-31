"""Chiamate LLM (OpenAI Chat Completions) + parsing JSON robusto delle risposte."""

from __future__ import annotations

import json
import re
from typing import Any

# Nessuna chiamata LLM può bloccare un ciclo di trading: senza timeout una
# risposta appesa terrebbe fermo lo stop loss fino al timeout di sistema.
LLM_TIMEOUT_S = 60.0
LLM_MAX_RETRIES = 1


def make_openai_client(api_key: str | None = None) -> Any:
    """Client OpenAI con timeout e retry limitati (usato ovunque nel bot)."""
    import openai

    kwargs: dict[str, Any] = {"timeout": LLM_TIMEOUT_S, "max_retries": LLM_MAX_RETRIES}
    if api_key:
        kwargs["api_key"] = api_key
    return openai.OpenAI(**kwargs)


def call_llm(
    system_blocks: list[dict],
    user_prompt: str,
    model: str,
    max_tokens: int,
    client: Any | None = None,
) -> str:
    """Singola chiamata chat; ritorna il testo della risposta.

    Il client OpenAI è creato lazy (API key da env OPENAI_API_KEY) ed è
    iniettabile/mockabile via parametro `client`. I system_blocks (formato a
    blocchi {"text": ...}) sono concatenati in un unico messaggio system:
    mantenerli byte-identici tra le chiamate sfrutta il prompt caching
    automatico di OpenAI sui prefissi ripetuti.
    """
    if client is None:
        client = make_openai_client()
    messages: list[dict[str, str]] = []
    if system_blocks:
        system_text = "\n\n".join(
            block.get("text", "") for block in system_blocks if block.get("text")
        )
        if system_text:
            messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": user_prompt})
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def extract_json(text: str) -> Any:
    """Primo blocco JSON valido nel testo, anche dentro fence ```json ... ```.

    Prova prima il contenuto dei fence, poi il testo grezzo, partendo da ogni
    `[` o `{` incontrato. Solleva ValueError se non trova nulla di valido.
    """
    decoder = json.JSONDecoder()
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for candidate in [*fenced, text]:
        for match in re.finditer(r"[\[{]", candidate):
            try:
                obj, _ = decoder.raw_decode(candidate[match.start():])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("nessun blocco JSON valido nella risposta LLM")
