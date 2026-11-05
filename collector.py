import os, time, json, sqlite3, threading, logging
from datetime import datetime
from contextlib import asynccontextmanager
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

logging.basicConfig(level=logging.INFO)
BASE='https://external-api.kalshi.com/trade-api/v2'
DB=os.getenv('DB_PATH','/data/kalshi.sqlite3')
TOKEN=os.getenv('READ_TOKEN','')
WRITE_TOKEN=os.getenv('WRITE_TOKEN','')
INTERVAL=max(10,int(os.getenv('SAMPLE_SECONDS','10')))
SESSION=requests.Session(); SESSION.headers.update({'User-Agent':'BTC15ResearchCollector/1.2'})

def get(url,params=None):
    r=SESSION.get(url,params=params,timeout=8); r.raise_for_status(); return r.json()

def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB)),exist_ok=True)
    c=sqlite3.connect(DB,timeout=15)
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    return c

def init():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS observations (ts INTEGER NOT NULL,ticker TEXT NOT NULL,seconds_left REAL,target REAL,btc_proxy REAL,yes_ask REAL,no_ask REAL,yes_bid REAL,no_bid REAL,quote_source TEXT,PRIMARY KEY(ts,ticker))')
        c.execute('CREATE TABLE IF NOT EXISTS outcomes (ticker TEXT PRIMARY KEY,result TEXT,status TEXT,checked_at INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS heartbeats (ts INTEGER PRIMARY KEY,ok INTEGER,details TEXT)')
        c.execute('CREATE INDEX IF NOT EXISTS obs_ticker ON observations(ticker,ts)')
        c.execute('''CREATE TABLE IF NOT EXISTS predictions (
            ticker TEXT PRIMARY KEY, ts INTEGER NOT NULL, side TEXT NOT NULL,
            agreement INTEGER NOT NULL, votes_json TEXT NOT NULL,
            seconds_left REAL NOT NULL, target REAL NOT NULL, btc_proxy REAL NOT NULL,
            ask REAL NOT NULL, fee REAL NOT NULL, sigma REAL,
            model_probability REAL, source TEXT NOT NULL)''')

def as_cents(x):
    try:
        v=float(x); return round(v*100,4) if v<=1 else v
    except (ValueError,TypeError): return None

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
    market_list=get(BASE+'/markets',{'series_ticker':'KXBTC15M','status':'open','limit':100}).get('markets',[])
    active=[]
    for m in market_list:
        try:
            left=datetime.fromisoformat(m['close_time'].replace('Z','+00:00')).timestamp()-now
            if 0<left<1000:active.append((left,m))
        except (ValueError,KeyError):continue
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
    with connect() as c:
        tickers=[x[0] for x in c.execute('''SELECT ticker FROM (SELECT ticker FROM observations UNION SELECT ticker FROM predictions) t
            LEFT JOIN outcomes r USING(ticker) WHERE r.result IS NULL LIMIT 120''')]
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
        time.sleep(max(.2,INTERVAL-(time.monotonic()-started)))

@asynccontextmanager
async def lifespan(app):
    init()
    threading.Thread(target=loop,daemon=True,name='collector').start()
    yield

app=FastAPI(title='Kalshi BTC Research Collector',lifespan=lifespan)

def auth(token):
    if not TOKEN or token!=TOKEN:raise HTTPException(401,'Invalid read token')

def write_auth(token):
    if not WRITE_TOKEN or token!=WRITE_TOKEN:raise HTTPException(401,'Invalid write token')

class Prediction(BaseModel):
    ticker:str=Field(min_length=5,max_length=100)
    side:Literal['YES','NO']
    agreement:int=Field(ge=5,le=7)
    votes:dict[str,int]
    seconds_left:float=Field(ge=180,le=300)
    target:float=Field(gt=0)
    btc_proxy:float=Field(gt=0)
    ask:float=Field(gt=0,le=.70)
    fee:float=Field(ge=0,le=.5)
    sigma:float|None=None
    model_probability:float|None=Field(default=None,ge=0,le=1)

@app.post('/predictions')
def record_prediction(p:Prediction,x_write_token:str|None=Header(default=None)):
    write_auth(x_write_token)
    if not p.ticker.startswith('KXBTC15M-'):
        raise HTTPException(422,'Unexpected ticker series')
    if len(p.votes)!=7 or any(v not in (-1,0,1) for v in p.votes.values()):
        raise HTTPException(422,'Expected exactly seven indicator votes')
    matching=sum(v==(1 if p.side=='YES' else -1) for v in p.votes.values())
    if matching!=p.agreement:raise HTTPException(422,'Agreement does not match votes')
    # One immutable first eligible snapshot per ticker; reruns cannot overwrite it.
    with connect() as c:
        cur=c.execute('''INSERT OR IGNORE INTO predictions
          (ticker,ts,side,agreement,votes_json,seconds_left,target,btc_proxy,ask,fee,sigma,model_probability,source)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (p.ticker,int(time.time()),p.side,p.agreement,json.dumps(p.votes,sort_keys=True),p.seconds_left,
           p.target,p.btc_proxy,p.ask,p.fee,p.sigma,p.model_probability,'Streamlit live indicator snapshot'))
        return {'recorded':cur.rowcount==1,'ticker':p.ticker}

@app.get('/predictions')
def predictions(limit:int=1000,x_read_token:str|None=Header(default=None)):
    auth(x_read_token)
    with connect() as c:
        c.row_factory=sqlite3.Row
        rows=c.execute('''SELECT p.*,o.result,o.status FROM predictions p
          LEFT JOIN outcomes o ON p.ticker=o.ticker ORDER BY p.ts DESC LIMIT ?''',
          (max(1,min(limit,5000)),)).fetchall()
    return {'predictions':[dict(row) for row in rows],'server_ts':int(time.time())}

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
        predictions_count=c.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
    return {'observations':count,'settled_markets':settled,'first_observation_ts':first,
            'recorded_predictions':predictions_count,'validated_strategy_win_rate':None}
