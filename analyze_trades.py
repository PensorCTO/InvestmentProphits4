import pandas as pd
from database.replica_store import open_replica

conn = open_replica()
df = pd.read_sql_query("SELECT * FROM trade_execution", conn)
conn.close()

if len(df) == 0:
    print("No trades found.")
    exit()

# Convert datetimes
df['closed_at'] = pd.to_datetime(df['closed_at'], errors='coerce')
# For entry time, we might not have it in the schema? Wait, we have committed_at. Let's see if it's there.
# If committed_at is missing, maybe there's an created_at? Let's check columns.
# We saw: trade_id, agent_id, market_id, direction, entry_price, kelly_size, bracket_stop_loss, bracket_take_profit, status, exit_price, entry_context, committed_at, closed_at, commitment_id, filled_size, avg_fill_price
# wait, committed_at is None for the rows above. So we'll parse trade_id to get timestamp if possible, or maybe there's another table. Let's check `trade_id` format: `trd_APEX_EDGE_mkt_us_e_1782475751781`. The last part is a timestamp.

df['timestamp_ms'] = df['trade_id'].str.extract(r'_(\d{13})$').astype(float)
df['opened_at'] = pd.to_datetime(df['timestamp_ms'], unit='ms', errors='coerce', utc=True)

df['hold_time_mins'] = (df['closed_at'] - df['opened_at']).dt.total_seconds() / 60.0

# Calculate PnL per unit
def calc_pnl(row):
    if pd.isna(row['exit_price']) or pd.isna(row['entry_price']):
        return 0.0
    if row['direction'] == 'YES':
        return row['exit_price'] - row['entry_price']
    elif row['direction'] == 'NO':
        return row['entry_price'] - row['exit_price']
    return 0.0

df['pnl_per_unit'] = df.apply(calc_pnl, axis=1)
df['total_pnl'] = df['pnl_per_unit'] * df['kelly_size']

print("Total trades:", len(df))
print("Total PnL:", df['total_pnl'].sum())
print("\nTrades by status:")
print(df['status'].value_counts())

print("\nAverage hold time (mins) by status:")
print(df.groupby('status')['hold_time_mins'].mean())

print("\nAverage PnL by status:")
print(df.groupby('status')['total_pnl'].mean())

print("\nWin rate:")
wins = len(df[df['total_pnl'] > 0])
losses = len(df[df['total_pnl'] < 0])
print(f"Wins: {wins}, Losses: {losses}, Win%: {wins/(wins+losses) if wins+losses > 0 else 0:.2%}")

print("\nRecent trades (last 5):")
print(df[['trade_id', 'status', 'direction', 'entry_price', 'exit_price', 'total_pnl', 'hold_time_mins']].tail())

print("\nSTOP_LOSS trades:")
stop_loss_df = df[df['status'] == 'CLOSED_STOP_LOSS']
print(stop_loss_df[['trade_id', 'direction', 'entry_price', 'exit_price', 'kelly_size', 'total_pnl', 'hold_time_mins']])

print("\nAverage kelly size by status:")
print(df.groupby('status')['kelly_size'].mean())
