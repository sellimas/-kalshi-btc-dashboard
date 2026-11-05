import math, re, time
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from kalshi_signal_upgrade import indicators
# Railway BTC collector connection
RAILWAY_URL = "https://web-production-5676b.up.railway.app"

def railway_data(endpoint):
    token = st.secrets.get("RAILWAY_READ_TOKEN", "")
    if not token:
        return None

    try:
        response = requests.get(
            f"{RAILWAY_URL}/{endpoint}",
            headers={"x-read-token": token},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None
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
    close_time = m.get("close_time")

    if not close_time:
        return None

    try:
        close = datetime.fromisoformat(
            close_time.replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        return max(0, (close - now).total_seconds())
    except (ValueError, TypeError):
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

def fee_estimate(price, contracts=1):
    # Conservative user-adjustable cost assumption.
    return fee * contracts


st.set_page_config(page_title='Kalshi BTC 15m Research',layout='wide')
st.title('BTC 15-minute | Kalshi Research Dashboard')
st.caption('Read-only research • Coinbase BTC/USD is NOT the CF Benchmarks settlement index • No order execution')
with st.sidebar:
    st.header('Settings')
    refresh=st.button('Refresh market data')
    min_edge=st.slider('Minimum estimated edge (percentage points)',1,25,8)
    fee=st.number_input('Assumed all-in fees/slippage per contract ($)',min_value=0.,max_value=.50,value=.02,step=.01)
    st.caption('Confirm Kalshi’s current contract-specific fee schedule. This input is an assumption.')

live,history,signals=st.tabs(['Live signal scanner','Historical research / backtest','10-Second Trade Signals'])
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


with signals:
    st_autorefresh(
        interval=10_000,
        limit=None,
        key="btc_signal_refresh"
    )    
    st.header("BTC 10-Second Trade Signals")
    st.caption("Read-only research | Manual trading only")

    st.subheader("Seven-Indicator Analysis")

    try:
        signal_candles = spot_history(4)

        current_minute = pd.Timestamp.now(tz="UTC").floor("min")
        signal_candles = signal_candles[
            signal_candles["time"] < current_minute
        ]

        analysis = indicators(signal_candles)

        if analysis is None:
            st.warning("Not enough valid candle data.")
        else:
            st.metric("Bullish Indicators", f"{analysis['up']}/7")
            st.metric("Bearish Indicators", f"{analysis['down']}/7")

            st.dataframe(
                pd.DataFrame([
                    {
                        "Indicator": name,
                        "Direction": (
                            "BULLISH" if vote == 1
                            else "BEARISH" if vote == -1
                            else "NEUTRAL"
                        )
                    }
                    for name, vote in analysis["votes"].items()
                ]),
                hide_index=True,
                use_container_width=True
            )

            st.write(
                "Volume ratio:",
                round(analysis["volume_ratio"], 2)
            )

    except Exception as exc:
        st.error(f"Indicator calculation failed: {exc}")

    
    data = railway_data("summary")

    if data is None:
        st.error("Railway connection unavailable.")
    else:
        st.success("Connected to Railway collector")
        st.metric(
            "Recorded Observations",
            data.get("observations", 0)
        )
        st.metric(
            "Settled Markets",
            data.get("settled_markets", 0)
        )


    # Display the seven-indicator agreement
    bullish = analysis["up"] if analysis is not None else 0
    bearish = analysis["down"] if analysis is not None else 0

    if bullish >= 5:
        direction = "YES"
        agreement = f"{bullish}/7 BULLISH"
    elif bearish >= 5:
        direction = "NO"
        agreement = f"{bearish}/7 BEARISH"
    else:
        direction = None
        agreement = "INSUFFICIENT"


# Preliminary indicator status only
if direction == "YES":
    signal_status = "YES BIAS"
elif direction == "NO":
    signal_status = "NO BIAS"
else:
    signal_status = "NO TRADE"

a, b, c = st.columns(3)

a.metric("Indicator Bias", signal_status)
b.metric("Indicator Agreement", agreement)
c.metric("Entry Window", "3–5 minutes")

    if direction:
        st.info(
            f"Indicator direction: {direction}. "
            "Awaiting market price, time-window, "
            "volatility and fee-adjusted EV checks."
        )

    st.subheader("Live Kalshi Contract")

    try:
        live_markets = markets()

        st.write("Open markets returned:", len(live_markets))

        for m in live_markets[:5]:
            st.write({
                "ticker": m.get("ticker"),
                "close_time": m.get("close_time"),
                "expiration_time": m.get("expiration_time"),
                "seconds_left": seconds_left(m),
                "strike": extract_strike(m)
            })

        
        eligible = []

        for market in live_markets:
            remaining = seconds_left(market)
            strike = extract_strike(market)

            if remaining is None or strike is None:
                continue

            if 180 <= remaining <= 300:
                eligible.append((market, remaining, strike))

        if not eligible:
            st.info("No contract currently in the 3–5 minute entry window.")

        else:
            market, remaining, strike = min(
                eligible,
                key=lambda item: item[1]
            )

            yes_price = ask(market, "YES")
            no_price = ask(market, "NO")

            st.write("Contract:", market.get("ticker"))
            st.metric("Seconds Remaining", int(remaining))
            st.metric("Target BTC Price", f"${strike:,.2f}")

            st.write(
                "YES Ask:",
                f"${yes_price:.2f}" if yes_price is not None else "N/A"
            )
            st.write(
                "NO Ask:",
                f"${no_price:.2f}" if no_price is not None else "N/A"
            )

            if direction:
                selected_price = (
                    yes_price if direction == "YES" else no_price
                )

                if selected_price is None:
                    st.warning("Selected contract price unavailable.")
                elif selected_price > 0.70:
                    st.warning("Contract exceeds $0.70 price limit.")
                else:
                    st.success("Price and entry-window checks passed.")

    except Exception as exc:
        st.error(f"Kalshi contract check failed: {exc}")


    st.subheader("Entry Requirements")

    st.write("Minimum indicator agreement: 5 of 7")
    st.write("Maximum contract price: $0.70")
    st.write("Required time remaining: 180–300 seconds")
    st.write("Data refresh target: 10 seconds")

    st.warning(
        "Signals remain disabled until market data, "
        "indicators, and fee-adjusted expected value "
        "are verified."
    )
