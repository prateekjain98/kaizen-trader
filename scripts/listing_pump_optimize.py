"""Grid search optimal listing pump parameters on 5m data, filtered by exchange."""
from src.backtesting.data_loader import load_klines
from src.backtesting.listing_loader import load_exchange_listings, get_listing_events_in_range

listings = load_exchange_listings()
events = get_listing_events_in_range(listings, 1577836800000, 1743379200000)

trades = []
for evt in events:
    if 'coinbase' not in evt['exchange'] and 'futures' not in evt['exchange']:
        continue
    sym = evt['symbol']
    if sym.startswith('1000000'): sym = sym[7:]
    elif sym.startswith('1000'): sym = sym[4:]
    lms = int(evt['listing_date_ms'])
    try:
        candles = load_klines(sym, '5m', lms - 3600000, lms + 48 * 3600000)
    except Exception:
        continue
    if not candles or len(candles) < 5:
        continue
    ec = [c for c in candles if c['open_time'] >= lms]
    if not ec or ec[0]['close'] <= 0:
        continue
    trades.append((sym, evt['exchange'], ec))

cb = [(s, e, ec) for s, e, ec in trades if 'coinbase' in e]
fu = [(s, e, ec) for s, e, ec in trades if 'futures' in e]

for label, subset in [('COINBASE', cb), ('FUTURES', fu)]:
    print(f'\n=== {label} ({len(subset)} trades) ===')
    best = (-999, '')
    for stop in [0.03, 0.05, 0.08]:
        for tgt in [0.05, 0.10, 0.15, 0.20, 0.30]:
            for hrs in [1, 3, 6, 12, 24]:
                mc = hrs * 12
                w = l = 0
                tp = 0.0
                for _, _, ec in subset:
                    entry = ec[0]['close']
                    ep = None
                    for c in ec[1:min(mc + 1, len(ec))]:
                        if c['low'] <= entry * (1 - stop):
                            ep = entry * (1 - stop)
                            break
                        if c['high'] >= entry * (1 + tgt):
                            ep = entry * (1 + tgt)
                            break
                    if ep is None:
                        ep = ec[min(mc, len(ec) - 1)]['close']
                    p = (ep - entry) / entry
                    tp += p
                    if p > 0:
                        w += 1
                    else:
                        l += 1
                wr = w / (w + l) * 100 if (w + l) else 0
                if tp > best[0]:
                    best = (tp, f'{stop*100:.0f}/{tgt*100:.0f}/{hrs}h')
                if tp > 0:
                    print(f'  {stop*100:.0f}%stop {tgt*100:.0f}%tgt {hrs}h -> {w+l}t {wr:.0f}%WR {tp*100:+.0f}%')
    print(f'  BEST: {best[1]} -> {best[0]*100:+.1f}%')
