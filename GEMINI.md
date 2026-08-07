# zettaranc-skill Project Instructions

This project is a Python-based "Thinking Operating System" and quantitative trading toolset based on the investment framework of zettaranc (万千). It integrates real market data, technical indicator calculation, strategy detection, and LLM-powered decision support.

## Project Overview

- **Purpose:** To provide a comprehensive system for stock analysis, strategy backtesting, and intelligent decision-making (stock/career/life) using a specific investment philosophy.
- **Core Technologies:** 
  - **Language:** Python 3.10+
  - **Data Source:** Tushare API (requires `TUSHARE_TOKEN`).
  - **Database:** SQLite (stored in `data/stock_data.db` by default).
  - **Analysis:** Pandas (vectorized indicator calculations), Technical indicators (60+), Strategies (30+).
  - **Interaction:** CLI (via `modules/cli.py`) and Intent-based Chat (`modules/intent_chat.py`).
  - **LLM Integration:** Supports OpenAI-compatible APIs and MiniMax for character-based commentary and decision frameworks.
- **Architecture:** 
  - **Data Layer:** Tushare API -> `data_sync.py` -> SQLite.
  - **Logic Layer:** `indicators/` (calculation) -> `strategies/` (detection) -> `screener.py` / `backtest.py`.
  - **Interface Layer:** `cli.py` (tools) -> `intent_router.py` (routing) -> `SKILL.md` (LLM persona).

## Building and Running

### Setup
1. **Environment:**
   ```bash
   pip install -r requirements.txt
   cp .env.example .env  # Configure TUSHARE_TOKEN and DATA_MODE
   ```
2. **Initialization:**
   ```bash
   python -m modules.database  # Create tables
   python -m modules.data_sync sync  # Sync basic stock info (once)
   ```

### Key Commands
- **Sync Data:** `python -m modules.data_sync sync --ts_code <CODE> --days <DAYS>`
- **Analyze Stock:** `python -m modules.cli analyze <CODE>` (or use `zt analyze <CODE>` if installed)
- **Screen Stocks:** `python -m modules.cli screen --strategy <STRATEGY> --limit 20`
- **Watchlist:** `python -m modules.cli watchlist scan`
- **Intent Chat:** `python -m modules.intent_chat "Query string"`
- **Run Tests:** `python -m pytest tests/`

## Development Conventions

### Coding Style
- **Pythonic:** Follow PEP 8. Use type hints for function signatures.
- **Data Preparation:** The Python layer is responsible for all data retrieval and calculation. LLMs should only be used for commentary and role-playing, not for raw data processing.
- **Modularity:** Keep indicators in `modules/indicators/` and strategies in `modules/strategies/`.
- **Performance:** Prefer Pandas vectorized operations over loops for technical indicator calculations.

### Testing
- **Framework:** Pytest.
- **Execution:** Run `python -m pytest tests/` before committing.
- **Isolation:** Ensure new strategies or indicators have corresponding unit tests in the `tests/` directory.

### Configuration
- Use `.env` for all sensitive information (Tokens, API Keys).
- `DATA_MODE` options: `jnb` (Tushare + local data) or `websearch` (LLM + Search only).

## Key Files & Directories
- `SKILL.md`: The core LLM persona definition and agentic protocol.
- `modules/`: Main source code.
- `knowledge/`: Trading system theory and documentation.
- `rules/`: LLM role frameworks for career and life advice.
- `data/`: Local SQLite database storage.
- `scripts/`: Batch processing and maintenance scripts.
