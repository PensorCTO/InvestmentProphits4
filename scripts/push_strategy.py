import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.arena_db import connect_arena_db
from database.strategy_store import write_active_strategy_source

def main():
    conn = connect_arena_db()
    with open(PROJECT_ROOT / "engine_2_crucible" / "active_strategy.py", "r") as f:
        source = f.read()
    
    version = write_active_strategy_source(conn, python_source=source, best_score=0.0, commit=True)
    print(f"Pushed strategy to DB. New version: {version}")

if __name__ == "__main__":
    main()
