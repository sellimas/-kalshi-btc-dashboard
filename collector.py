import os, time, json, sqlite3, threading, logging, math
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import requests
from fastapi import FastAPI, Header, HTTPException

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
BASE='https://external-api.kalshi.com/trade-api/v2'
DB=os.getenv('DB_PATH','/data/kalshi.sqlite3')
TOKEN=os.getenv('READ_TOKEN','')
INTERVAL=max(10,int(os.getenv('SAMPLE_SECONDS','10')))
SESSION=requests.Session(); SESSION.headers.update({'User-Agent':'BTC15ResearchCollector/1.1'})
lock=threading.Lock()

def get(url, params=None):
    r=SESSION.get(url,params=params,timeout=8);r.raise_for_status();return r.json()

def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB)),exist_ok=True)
    c=sqlite3.connect(DB,timeout=15)
    c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=10000')
    return c

def init():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS observations (ts INTEGER NOT NULL,ticker TEXT NOT NULL,seconds_left REAL,target REAL,btc_proxy REAL,yes_ask REAL,no_ask REAL,yes_bid REAL,no_bid REAL,quote_source TEXT,PRIMARY KEY(ts,ticker))')
        c.execute('CREATE TABLE IF NOT EXISTS outcomes (ticker TEXT PRIMARY KEY,result TEXT,status TEXT,checked_at INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS heartbeats (ts INTEGER PRIMARY KEY,ok INTEGER,details TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS obs_ticker ON observations(ticker,ts)')

def as_cents(x):
    try:
        v=float(x);return round(v*100,4) if v<=1 else v
    except (ValueError,TypeError):return None

def bid(book,side):
    levels=book.get(side+'_dollars') or book.get(side) or []
    vals=[as_cents(x[0]) for x in levels if x and as_cents(x[0]) is not None]
    return max(vals) if vals else None

def target_of(m):
    for k in ('floor_strike','cap_strike','strike_price'):
        try:
            if m.get(k) is not None:return float(m[k])
        except (ValueError,TypeError):pass
    return None

def sample():
    now=time.time();stamp=int(now)
    markets=get(BASE+'/markets',{'series_ticker':'KXBTC15M','status':'open','limit':100}).get('markets',[])
    active=[]
    for m in markets:
        try:
            left=datetime.fromisoformat(m['close_time'].replace('Z','+00:00')).timestamp()-now
            if 0<left<1000:active.append((left,m))
        except (ValueError,KeyError):continue
    # Independent spot proxy, not the official settlement index.
    spot=None
    try:spot=float(get('https://api.kraken.com/0/public/Ticker',{'pair':'XBTUSD'})['result'].popitem()[1]['c'][0])
    except Exception as exc:logging.warning('Spot proxy unavailable: %s',exc)
    rows=[]
    for left,m in active:
        ticker=m['ticker']
        try:
            response=get(BASE+'/markets/'+ticker+'/orderbook',{'depth':20})
            book=response.get('orderbook_fp') or response.get('orderbook') or {}
            y,n=bid(book,'yes'),bid(book,'no')
            rows.append((stamp,ticker,round(left,1),target_of(m),spot,round(100-n,4) if n is not None else None,round(100-y,4) if y is not None else None,y,n,'Kalshi orderbook reciprocal bids'))
        except Exception as exc:logging.warning('Orderbook %s: %s',ticker,exc)
    with connect() as c:
        if rows:c.executemany('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
        c.execute('INSERT OR REPLACE INTO heartbeats VALUES (?,?,?)',(stamp,1,f'open={len(active)} saved={len(rows)}'))
        c.execute('DELETE FROM heartbeats WHERE ts<?',(stamp-86400*30,))
    return len(rows)

def update_outcomes():
    # Never infer winning side from proxy prices; use official Kalshi result only.
    with connect() as c:
        tickers=[x[0] for x in c.execute("SELECT DISTINCT o.ticker FROM observations o LEFT JOIN outcomes r ON o.ticker=r.ticker WHERE r.result IS NULL LIMIT 120")]
    for ticker in tickers:
        try:
            m=get(BASE+'/markets/'+ticker).get('market',{})
            status=str(m.get('status',''))
            result=str(m.get('result','')).lower()
            if result not in ('yes','no'):result=None
            with connect() as c:c.execute('INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?)',(ticker,result,status,int(time.time())))
        except Exception as exc:logging.warning('Settlement lookup %s: %s',ticker,exc)
        time.sleep(.1)

def loop():
    last_settle=0
    while True:
        started=time.monotonic()
        try:sample()
        except Exception as exc:
            logging.exception('Sample failed')
            with connect() as c:c.execute('INSERT OR REPLACE INTO heartbeats VALUES (?,?,?)',(int(time.time()),0,str(exc)[:250]))
        if time.monotonic()-last_settle>=300:
            try:update_outcomes()
            except Exception:logging.exception('Outcome refresh failed')
            last_settle=time.monotonic()
        time.sleep(max(0.2,INTERVAL-(time.monotonic()-started)))

@asynccontextmanager
async def lifespan(app):
    init()
    threading.Thread(target=loop,daemon=True,name='collector').start()
    yield

app=FastAPI(title='Kalshi BTC Research Collector',lifespan=lifespan)

def auth(x_read_token):
    if not TOKEN or x_read_token!=TOKEN:raise HTTPException(401,'Invalid read token')

@app.get('/health')
def health():
    with connect() as c:
        last=c.execute('SELECT ts,ok,details FROM heartbeats ORDER BY ts DESC LIMIT 1').fetchone()
        count=c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
    age=int(time.time())-last[0] if last else None
    return {'collector_alive':age is not None and age<45 and bool(last[1]),'seconds_since_heartbeat':age,'last_cycle':last[2] if last else None,'observations':count}

@app.get('/latest')
def latest(x_read_token:str|None=Header(default=None)):
    auth(x_read_token)
    with connect() as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT * FROM observations WHERE ts >= ? ORDER BY ts DESC LIMIT 30',(int(time.time())-120,)).fetchall()
    seen=set();result=[]
    for row in rows:
        if row['ticker'] not in seen:result.append(dict(row));seen.add(row['ticker'])
    return {'observations':result,'server_ts':int(time.time())}

@app.get('/history')
def history(limit:int=300,x_read_token:str|None=Header(default=None)):
    auth(x_read_token)
    with connect() as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('SELECT o.*,r.result,r.status FROM observations o LEFT JOIN outcomes r ON o.ticker=r.ticker ORDER BY o.ts DESC LIMIT ?', (max(1,min(limit,2000)),)).fetchall()
    return {'observations':[dict(x) for x in rows]}

@app.get('/summary')
def summary(x_read_token:str|None=Header(default=None)):
    auth(x_read_token)
    with connect() as c:
        count=c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        settled=c.execute("SELECT COUNT(*) FROM outcomes WHERE result IN ('yes','no')").fetchone()[0]
        first=c.execute('SELECT MIN(ts) FROM observations').fetchone()[0]
    return {'observations':count,'settled_markets':settled,'first_observation_ts':first,'validated_strategy_win_rate':None}
