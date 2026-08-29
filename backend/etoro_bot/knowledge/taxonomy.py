"""Tassonomia finanziaria di base per il Financial Knowledge Graph in ArangoDB.

Definisce settori, industry, macro-fattori, ticker principali (USA ed Europa)
e le loro relazioni causali (fornitori, competitor, appartenenza a settore, impatto macro).
"""

from __future__ import annotations

from typing import Any

# Nodi settoriali e macro
DEFAULT_MARKET_NODES: list[dict[str, Any]] = [
    # Macro & Tassi
    {
        "id": "macro_rates",
        "name": "Federal Reserve & Interest Rates",
        "type": "macro",
        "category": "Macroeconomics",
        "description": "Decisioni sui tassi di interesse e politica monetaria Fed/BCE",
    },
    {
        "id": "macro_inflation",
        "name": "CPI & Inflation Data",
        "type": "macro",
        "category": "Macroeconomics",
        "description": "Dati sull'inflazione, prezzi al consumo e costi energetici",
    },
    {
        "id": "macro_geopolitics",
        "name": "Geopolitical & Trade Policy",
        "type": "macro",
        "category": "Macroeconomics",
        "description": "Dazi commerciali, tensioni geopolitiche e catene di fornitura globali",
    },

    # Settori principali
    {
        "id": "sector_semi",
        "name": "Semiconductors & Chipmaking",
        "type": "sector",
        "category": "Technology",
        "description": "Progettazione e produzione di semiconduttori, wafer e GPU",
    },
    {
        "id": "sector_ai_cloud",
        "name": "AI Infrastructure & Cloud Computing",
        "type": "sector",
        "category": "Technology",
        "description": "Datacenter hyperscaler, modelli di intelligenza artificiale e SaaS",
    },
    {
        "id": "sector_big_tech",
        "name": "Big Tech Mega-Caps",
        "type": "sector",
        "category": "Technology",
        "description": "Piattaforme digitali ed ecosistemi hardware/software leader di mercato",
    },
    {
        "id": "sector_auto_ev",
        "name": "Automotive & Electric Vehicles",
        "type": "sector",
        "category": "Consumer Cyclical",
        "description": "Veicoli elettrici, guida autonoma e componentistica auto",
    },
    {
        "id": "sector_fin_banking",
        "name": "Financial Services & Banking",
        "type": "sector",
        "category": "Financials",
        "description": "Grandi banche di investimento, pagamenti digitali e broker",
    },
    {
        "id": "sector_energy",
        "name": "Energy & Renewables",
        "type": "sector",
        "category": "Energy",
        "description": "Petrolio, gas naturale, utility energetiche e rinnovabili",
    },
    {
        "id": "sector_consumer",
        "name": "Consumer Discretionary & Retail",
        "type": "sector",
        "category": "Consumer",
        "description": "E-commerce, beni di lusso e grande consumo",
    },

    # Ticker USA
    {"id": "NVDA", "name": "NVIDIA Corp", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "AMD", "name": "Advanced Micro Devices", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "INTC", "name": "Intel Corp", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "TSM", "name": "Taiwan Semiconductor (TSMC)", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "ASML", "name": "ASML Holding", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "AVGO", "name": "Broadcom Inc", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "QCOM", "name": "Qualcomm Inc", "type": "ticker", "sector": "sector_semi", "market": "usa"},
    {"id": "MSFT", "name": "Microsoft Corp", "type": "ticker", "sector": "sector_ai_cloud", "market": "usa"},
    {"id": "AAPL", "name": "Apple Inc", "type": "ticker", "sector": "sector_big_tech", "market": "usa"},
    {"id": "GOOGL", "name": "Alphabet Inc", "type": "ticker", "sector": "sector_ai_cloud", "market": "usa"},
    {"id": "AMZN", "name": "Amazon.com Inc", "type": "ticker", "sector": "sector_consumer", "market": "usa"},
    {"id": "META", "name": "Meta Platforms Inc", "type": "ticker", "sector": "sector_ai_cloud", "market": "usa"},
    {"id": "TSLA", "name": "Tesla Inc", "type": "ticker", "sector": "sector_auto_ev", "market": "usa"},
    {"id": "JPM", "name": "JPMorgan Chase & Co", "type": "ticker", "sector": "sector_fin_banking", "market": "usa"},
    {"id": "V", "name": "Visa Inc", "type": "ticker", "sector": "sector_fin_banking", "market": "usa"},
    {"id": "XOM", "name": "Exxon Mobil Corp", "type": "ticker", "sector": "sector_energy", "market": "usa"},
    {"id": "SPY", "name": "SPDR S&P 500 ETF Trust", "type": "etf", "sector": "sector_big_tech", "market": "usa"},
    {"id": "QQQ", "name": "Invesco QQQ Trust", "type": "etf", "sector": "sector_big_tech", "market": "usa"},

    # Ticker Europei
    {"id": "SAP.DE", "name": "SAP SE", "type": "ticker", "sector": "sector_ai_cloud", "market": "europe"},
    {"id": "ASML.AS", "name": "ASML Holding NV (EU)", "type": "ticker", "sector": "sector_semi", "market": "europe"},
    {"id": "MC.PA", "name": "LVMH Moët Hennessy", "type": "ticker", "sector": "sector_consumer", "market": "europe"},
    {"id": "ENEL.MI", "name": "Enel SpA", "type": "ticker", "sector": "sector_energy", "market": "europe"},
    {"id": "RACE.MI", "name": "Ferrari NV", "type": "ticker", "sector": "sector_auto_ev", "market": "europe"},
    {"id": "ISP.MI", "name": "Intesa Sanpaolo SpA", "type": "ticker", "sector": "sector_fin_banking", "market": "europe"},
]

# Archi e relazioni causali
DEFAULT_MARKET_EDGES: list[dict[str, Any]] = [
    # Appartenenza a Settori
    {"from": "NVDA", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "NVDA", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 0.9},
    {"from": "AMD", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "INTC", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "TSM", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "ASML", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "AVGO", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 0.9},
    {"from": "QCOM", "to": "sector_semi", "relation": "BELONGS_TO", "weight": 0.9},
    {"from": "MSFT", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "MSFT", "to": "sector_big_tech", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "AAPL", "to": "sector_big_tech", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "AAPL", "to": "sector_consumer", "relation": "BELONGS_TO", "weight": 0.8},
    {"from": "GOOGL", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "GOOGL", "to": "sector_big_tech", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "AMZN", "to": "sector_consumer", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "AMZN", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 0.9},
    {"from": "META", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 0.9},
    {"from": "TSLA", "to": "sector_auto_ev", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "TSLA", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 0.8},
    {"from": "JPM", "to": "sector_fin_banking", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "SAP.DE", "to": "sector_ai_cloud", "relation": "BELONGS_TO", "weight": 1.0},
    {"from": "ENEL.MI", "to": "sector_energy", "relation": "BELONGS_TO", "weight": 1.0},

    # Supply Chain (Fornitori e Clienti)
    {"from": "ASML", "to": "TSM", "relation": "SUPPLIER_OF", "weight": 0.95},
    {"from": "ASML", "to": "INTC", "relation": "SUPPLIER_OF", "weight": 0.85},
    {"from": "TSM", "to": "NVDA", "relation": "SUPPLIER_OF", "weight": 0.98},
    {"from": "TSM", "to": "AAPL", "relation": "SUPPLIER_OF", "weight": 0.95},
    {"from": "TSM", "to": "AMD", "relation": "SUPPLIER_OF", "weight": 0.90},
    {"from": "TSM", "to": "QCOM", "relation": "SUPPLIER_OF", "weight": 0.90},
    {"from": "NVDA", "to": "MSFT", "relation": "SUPPLIER_OF", "weight": 0.90},
    {"from": "NVDA", "to": "META", "relation": "SUPPLIER_OF", "weight": 0.90},
    {"from": "NVDA", "to": "AMZN", "relation": "SUPPLIER_OF", "weight": 0.85},
    {"from": "NVDA", "to": "GOOGL", "relation": "SUPPLIER_OF", "weight": 0.85},
    {"from": "NVDA", "to": "TSLA", "relation": "SUPPLIER_OF", "weight": 0.80},
    {"from": "AVGO", "to": "AAPL", "relation": "SUPPLIER_OF", "weight": 0.80},

    # Competitor Diretti
    {"from": "NVDA", "to": "AMD", "relation": "COMPETITOR_OF", "weight": 0.85},
    {"from": "NVDA", "to": "INTC", "relation": "COMPETITOR_OF", "weight": 0.75},
    {"from": "AMD", "to": "INTC", "relation": "COMPETITOR_OF", "weight": 0.85},
    {"from": "MSFT", "to": "GOOGL", "relation": "COMPETITOR_OF", "weight": 0.85},
    {"from": "MSFT", "to": "AMZN", "relation": "COMPETITOR_OF", "weight": 0.80},
    {"from": "AAPL", "to": "GOOGL", "relation": "COMPETITOR_OF", "weight": 0.75},
    {"from": "META", "to": "GOOGL", "relation": "COMPETITOR_OF", "weight": 0.80},

    # Impatti Macro
    {"from": "macro_rates", "to": "sector_ai_cloud", "relation": "IMPACTS", "weight": -0.75},
    {"from": "macro_rates", "to": "sector_semi", "relation": "IMPACTS", "weight": -0.65},
    {"from": "macro_rates", "to": "sector_fin_banking", "relation": "IMPACTS", "weight": 0.70},
    {"from": "macro_inflation", "to": "macro_rates", "relation": "IMPACTS", "weight": 0.90},
    {"from": "macro_inflation", "to": "sector_consumer", "relation": "IMPACTS", "weight": -0.70},
    {"from": "macro_geopolitics", "to": "TSM", "relation": "IMPACTS", "weight": -0.85},
    {"from": "macro_geopolitics", "to": "sector_semi", "relation": "IMPACTS", "weight": -0.80},
]
