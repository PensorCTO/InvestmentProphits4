import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.arena_db import connect_arena_db
from database.portfolio_store import reset_apex_wallet

def main():
    conn = connect_arena_db()
    result = reset_apex_wallet(conn, commit=True)
    print(f"Reset wallet. Closed positions: {result['closed_positions']}, New capital: {result['initial_capital']}")

if __name__ == "__main__":
    main()
