import math, re, time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st

API='https://api.elections.kalshi.com/trade-api/v2'
SERIES='KXBTC15M'
S=requests.Session(); S.headers.update({'User-Agent':'KalshiBTCResearchDashboard/1.0'})

def get(url,params=None):
    r=S.get(url,params=params,timeout=15); r.raise_for_status(); return r.json()

def spot_history(hours=24):
    # Coinbase exchange candles are OHLCV observations, not Kalshi's settlement index.
    end=datetime.now(timezone.utc); start=end-timedelta(hours=hours)
    frames=[]
    for i in range(0,hours,4):
        a=start+timedelta(hours=i); b=min(a+timedelta(hours=4),end)
        data=get('https://api.exchange.coinbase.com/products/BTC-USD/candles',{'start':a.isoformat(),'end':b.isoformat(),'granularity':60})
        frames.extend(data)
    if not frames: return pd.DataFrame()
    df=pd.DataFrame(frames,columns=['ts','low','high','open','close','volume']).drop_duplicates('ts').sort_values('ts')
    df['time']=pd.to_datetime(df.ts,unit='s',utc=True)
    return df.reset_index(drop=True)

def markets(status='open',limit=200):
    data=get(API+'/markets',{'series_ticker':SERIES,'status':status,'limit':limit})
    return data.get('markets',[])

def num(m,k):
    for key in (k+'_dollars',k):
        if key in m and m[key] not in (None,''):
            v=float(m[key]); return v if key.endswith('_dollars') else v/100
    return None

def ask(m,side):
    if side=='YES':
        return num(m,'yes_ask')
    return num(m,'no_ask')

def extract_strike(m):
    for key in ('floor_strike','strike_price','strike'):
        if m.get(key) is not None:
            return float(m[key])
    return None

def seconds_left(m):
    for key in ('close_time','expiration_time'):
        if m.get(key):
            try:return max(0,(datetime.fromisoformat(m[key].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
            except ValueError:pass
    return None

def vol_per_min(df):
    r=np.log(df.close.astype(float)).diff().dropna()
    return float(r.tail(120).std()) if len(r)>10 else None

def normal_cdf(x):return .5*(1+math.erf(x/math.sqrt(2)))

def probability_up(spot,strike,seconds,sigma):
    if not sigma or sigma<=0 or seconds is None or seconds<=0:return None
    # Illustrative lognormal diffusion. Settlement-index basis and final-minute averaging are NOT modeled.
    z=math.log(spot/strike)/(sigma*math.sqrt(max(seconds/60,0.05)))
    return normal_cdf(z)

def fee_estimate(price,contracts=1):
    # Default illustrative fee only; actual fee schedule can differ.
    return 0.0

st.set_page_config(page_title='Kalshi BTC 15m Research',layout='wide')
st.title('BTC 15-minute | Kalshi Research Dashboard')
st.caption('Read-only research • Coinbase BTC/USD is NOT the CF Benchmarks settlement index • No order execution')
with st.sidebar:
    st.header('Settings')
    refresh=st.button('Refresh market data')
    min_edge=st.slider('Minimum estimated edge (percentage points)',1,25,8)
    fee=st.number_input('Assumed all-in fees/slippage per contract ($)',min_value=0.,max_value=.50,value=.02,step=.01)
    st.caption('Confirm Kalshi’s current contract-specific fee schedule. This input is an assumption.')

live,history=st.tabs(['Live signal scanner','Historical research / backtest'])
with live:
    try:
        candles=spot_history(8)
        active=markets()
        if candles.empty:st.warning('No Coinbase candles returned.'); st.stop()
        spot=float(candles.close.iloc[-1]); sig=vol_per_min(candles)
        a,b,c=st.columns(3);a.metric('Coinbase BTC/USD',f'${spot:,.2f}');b.metric('1-min log-return volatility',f'{sig*100:.4f}%' if sig else 'N/A');c.metric('Open Kalshi series markets',len(active))
        st.line_chart(candles.set_index('time')['close'])
        rows=[]
        for m in active:
            strike=extract_strike(m); left=seconds_left(m)
            if not strike or not left or not sig:continue
            up=probability_up(spot,strike,left,sig)
            for side in ('YES','NO'):
                quote=ask(m,side)
                if quote is None or not 0<quote<1:continue
                prob=up if side=='YES' else 1-up
                edge=prob-quote-fee
                rows.append({'Ticker':m.get('ticker'),'Side':side,'Target':strike,'Seconds left':int(left),'Ask $':round(quote,3),'Model probability':round(prob,3),'Cost with friction $':round(quote+fee,3),'Estimated edge pp':round(edge*100,2),'Research flag':'REVIEW' if edge*100>=min_edge else 'SKIP'})
        if rows:
            result=pd.DataFrame(rows).sort_values('Estimated edge pp',ascending=False)
            st.dataframe(result,use_container_width=True,hide_index=True)
            st.download_button('Export current snapshot CSV',result.to_csv(index=False),'live_snapshot.csv','text/csv')
        else:st.info('No active contracts with usable strike, time and ask data. No signal is generated. Try refreshing.')
        st.warning('Model probabilities are exploratory, not calibrated. The target may use an index different from Coinbase, and final-minute averaging is not represented. A REVIEW flag is not a recommendation to trade.')
    except Exception as exc:st.error(f'Live market retrieval failed: {exc}')

with history:
    st.subheader('Backtest with your recorded contract observations')
    st.write('Upload a CSV containing real historical snapshots and final settlement outcomes. This avoids inventing past bid/ask prices or treating Coinbase candles as Kalshi fills.')
    st.code('timestamp,ticker,side,ask,model_probability,fee,won\n2026-10-01T12:01:00Z,EXAMPLE,YES,0.55,0.65,0.02,1',language='text')
    upload=st.file_uploader('Historical observations CSV',type='csv')
    if upload:
        try:
            d=pd.read_csv(upload)
            required={'timestamp','ticker','side','ask','model_probability','fee','won'}
            if not required.issubset(d.columns):raise ValueError('Missing columns: '+', '.join(sorted(required-set(d.columns))))
            d['timestamp']=pd.to_datetime(d.timestamp,utc=True)
            for col in ('ask','model_probability','fee','won'):d[col]=pd.to_numeric(d[col],errors='raise')
            d=d.sort_values('timestamp').drop_duplicates(['ticker','side'],keep='first')
            d=d[(d.ask.between(0,1))&(d.model_probability.between(0,1))&(d.won.isin([0,1]))&(d.fee>=0)]
            d['edge']=d.model_probability-d.ask-d.fee
            selected=d[d.edge>=min_edge/100].copy()
            if selected.empty:st.info('No observations pass the edge filter.')
            else:
                selected['pnl']=selected.won-selected.ask-selected.fee
                selected['equity']=selected.pnl.cumsum()
                selected['peak']=selected.equity.cummax().clip(lower=0)
                selected['drawdown']=selected.equity-selected.peak
                c1,c2,c3,c4=st.columns(4)
                c1.metric('Trades',len(selected));c2.metric('Win rate',f'{selected.won.mean()*100:.1f}%');c3.metric('P/L per 1 contract',f'${selected.pnl.sum():.2f}');c4.metric('Max drawdown',f'${selected.drawdown.min():.2f}')
                st.line_chart(selected.set_index('timestamp')['equity'])
                st.dataframe(selected,use_container_width=True)
                st.download_button('Download backtest trades',selected.to_csv(index=False),'backtest.csv')
                st.caption('One contract per observation, settlement held to expiration. Excludes rejected/unfilled orders, latency and market impact. Fees are supplied by your dataset.')
        except Exception as exc:st.error(f'Invalid backtest input: {exc}')
    else:
        st.info('A credible historical P/L backtest requires recorded historical contract quotes and outcomes. No historical profitability is claimed without those observations.')
    st.markdown('**Research protocol:** Capture ask quotes at a fixed cadence, record the model probability *as it was at the time*, and append the official settlement outcome after expiration. Evaluate out-of-sample periods, probability calibration, fill assumptions and sensitivity to fees. Do not calculate signals from future candles.')
