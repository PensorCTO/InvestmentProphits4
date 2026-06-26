import sqlite3
from shared.poly_costs import PolyCostModel

def run():
    conn = sqlite3.connect("data/ip4_sqld_primary.db")
    c = conn.cursor()
    c.execute("SELECT trade_id, direction, entry_price, exit_price FROM trade_execution WHERE direction = 'NO' AND status = 'CLOSED_STOP_LOSS'")
    rows = c.fetchall()
    
    if not rows:
        print("No corrupted NO trades found.")
        return

    for row in rows:
        trade_id, direction, entry_price, exit_price = row
        print(f"Fixing {trade_id}: entry={entry_price}, exit={exit_price}")
        
        new_entry = round(1.0 - entry_price, 6)
        sl, tp = PolyCostModel.compute_brackets(new_entry, direction="NO")
        
        c.execute("""
            UPDATE trade_execution 
            SET entry_price = ?, bracket_stop_loss = ?, bracket_take_profit = ?
            WHERE trade_id = ?
        """, (new_entry, sl, tp, trade_id))
    
    conn.commit()
    print("Repaired NO trades.")

run()
