"""CLI minimale: un ciclo di allenamento dell'arena, per prova e debug.

`python -m etoro_bot.main` esegue bootstrap (se serve) + un ciclo di trading
simulato per entrambi gli agenti e stampa il summary JSON. Il servizio vero
gira dentro l'API (scheduler in api/server.py).
"""

from __future__ import annotations

import json
import logging
import sys


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from etoro_bot.arena.engine import bootstrap_if_needed, run_training_cycle
    from etoro_bot.db.repo import Repository, make_engine, make_session_factory
    from etoro_bot.services.deps import build_arena_deps

    deps = build_arena_deps(Repository(make_session_factory(make_engine())))
    bootstrap_if_needed(deps)
    summary = run_training_cycle(deps)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
