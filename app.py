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

@st.cache_data(ttl=2, show_spinner=False)
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

@st.cache_data(ttl=2, show_spinner=False)
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
    if refresh:
        spot_history.clear()
        markets.clear()
    min_edge=st.slider('Minimum estimated edge (percentage points)',1,25,8)
    fee=st.number_input('Assumed all-in fees/slippage per contract ($)',min_value=0.,max_value=.50,value=.02,step=.01)
    st.caption('Confirm Kalshi’s current contract-specific fee schedule. This input is an assumption.')

live,history,signals=st.tabs(['Live signal scanner','Historical research / backtest','2-Second Signal Monitor'])
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
    analysis = None
    direction = None
    bullish = 0
    bearish = 0
    agreement = "INSUFFICIENT"
    st_autorefresh(
        interval=2_000,
        limit=None,
        key="btc_signal_refresh"
    )    
    st.header("BTC 2-Second Signal Monitor")
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
    st.write("Data refresh target: 2 seconds")

    st.warning(
        "Signals remain disabled until market data, "
        "indicators, and fee-adjusted expected value "
        "are verified."
    )


# ---------------- Four-stage research validation ----------------
# All stages fail closed when source quality or calibration is unverified.
st.divider()
st.header("Final Trade Signal — Research Checks")

required_agreement = 5
maximum_price = 0.70
minimum_seconds, maximum_seconds = 180, 300
bias = direction
agreement_count = bullish if bias == "YES" else bearish if bias == "NO" else 0
indicator_pass = bias in ("YES", "NO") and agreement_count >= required_agreement

# The same 2-second cached observations are reused across the screen.
validation_candles = pd.DataFrame()
validation_markets = []
market_error = None
try:
    validation_candles = spot_history(4)
    validation_markets = markets()
except Exception as exc:
    market_error = str(exc)

# Exclude the unfinished minute when estimating volatility.
completed = pd.DataFrame()
if not validation_candles.empty:
    cutoff = pd.Timestamp.now(tz="UTC").floor("min")
    completed = validation_candles[validation_candles["time"] < cutoff].copy()
    completed = completed.sort_values("time").drop_duplicates("time")

spot = float(completed["close"].iloc[-1]) if not completed.empty else None
sigma = vol_per_min(completed) if len(completed) >= 30 else None
if sigma is not None and not np.isfinite(sigma):
    sigma = None

valid_contracts = []
for candidate in validation_markets:
    remaining = seconds_left(candidate)
    target = extract_strike(candidate)
    if remaining is not None and remaining > 0 and target is not None and target > 0:
        valid_contracts.append((candidate, remaining, target))

# Show the nearest expiry for monitoring, even before the entry window.
monitor = min(valid_contracts, key=lambda x: x[1]) if valid_contracts else None
eligible = [x for x in valid_contracts if minimum_seconds <= x[1] <= maximum_seconds]
chosen = min(eligible, key=lambda x: x[1]) if eligible else None
contract, seconds_remaining, strike_price = chosen if chosen else (None, None, None)
contract_price = ask(contract, bias) if contract is not None and bias in ("YES", "NO") else None
time_pass = chosen is not None
price_pass = contract_price is not None and 0 < contract_price <= maximum_price

st.write("Indicator agreement:", f"{agreement_count}/7", "PASS" if indicator_pass else "FAIL")
st.write("Entry window:", "PASS" if time_pass else "WAITING")
st.write("Contract ask:", f"${contract_price:.2f}" if contract_price is not None else "UNAVAILABLE",
         "PASS" if price_pass else "NOT PASSED")
if market_error:
    st.error(f"Market data unavailable: {market_error}")

# 1 — Volatility screening. The thresholds are provisional, not validated.
st.subheader("1. Volatility Screening")
volatility_screen_pass = False
if sigma is None:
    st.warning("PENDING — Insufficient completed 1-minute candles.")
else:
    vol_pct = sigma * 100
    volatility_screen_pass = 0.01 <= vol_pct <= 0.15
    st.metric("1-minute return volatility", f"{vol_pct:.4f}%")
    st.write("Provisional 0.01%–0.15% screen:", "PASS" if volatility_screen_pass else "OUTSIDE RANGE")
st.caption("This range is an illustrative filter, not a backtested or independently verified risk threshold.")

# 2 — Distance from the target, expressed in units of estimated 1-sigma movement.
# Coinbase spot is only a proxy; it does not validate the official settlement index.
st.subheader("2. Settlement Distance Analysis")
distance_screen_pass = False
proxy_distance = None
expected_move = None
if monitor is not None and spot is not None and sigma is not None and sigma > 0:
    m, remaining, target = monitor
    proxy_distance = spot - target
    expected_move = spot * sigma * math.sqrt(remaining / 60)
    distance_ratio = abs(proxy_distance) / expected_move if expected_move > 0 else None
    st.write("Monitored contract:", m.get("ticker"))
    st.metric("Seconds remaining", int(remaining))
    st.metric("Coinbase BTC/USD (completed candle)", f"${spot:,.2f}")
    st.metric("Kalshi target", f"${target:,.2f}")
    st.metric("Coinbase-to-target distance", f"${proxy_distance:+,.2f}")
    st.metric("Illustrative 1-sigma move", f"${expected_move:,.2f}")
    if distance_ratio is not None:
        st.metric("Absolute distance / expected move", f"{distance_ratio:.2f}x")
    if remaining > maximum_seconds:
        st.info(f"Entry window opens in approximately {int(remaining - maximum_seconds)} seconds.")
    elif remaining < minimum_seconds:
        st.info("Entry window has closed for this contract.")
    else:
        st.info("Entry window is open; further validation is required.")
else:
    st.warning("PENDING — No complete price, target, countdown, and volatility observation.")
st.warning("UNVERIFIED — Coinbase BTC/USD is not the official Kalshi settlement benchmark. "
           "The distance screen cannot be certified from Coinbase data alone.")

# 3 — Out-of-sample empirical calibration from real prediction/outcome rows.
# Data must be independent of the data used to fit the forecasting model.
st.subheader("3. Historical Probability Calibration")
st.caption("Optional CSV columns: timestamp,ticker,side,model_probability,won. "
           "One independent settled prediction per market; 1=won, 0=lost. "
           "Upload predictions recorded before settlement, not reconstructed afterwards.")
calibration_upload = st.file_uploader("Upload out-of-sample calibration CSV", type="csv", key="calibration_csv")
calibration_valid = False
calibration_df = None
calibration_brier = None
if calibration_upload is not None:
    try:
        cal = pd.read_csv(calibration_upload)
        fields = {"timestamp", "ticker", "side", "model_probability", "won"}
        if not fields.issubset(cal.columns):
            raise ValueError("Missing: " + ", ".join(sorted(fields - set(cal.columns))))
        cal["timestamp"] = pd.to_datetime(cal["timestamp"], utc=True, errors="raise")
        cal["model_probability"] = pd.to_numeric(cal["model_probability"], errors="raise")
        cal["won"] = pd.to_numeric(cal["won"], errors="raise")
        cal = cal.dropna(subset=["timestamp", "ticker", "side", "model_probability", "won"])
        cal = cal[cal["model_probability"].between(0, 1) & cal["won"].isin([0, 1])]
        # One settled observation per market, not YES and NO duplicates.
        cal = cal.sort_values("timestamp").drop_duplicates("ticker", keep="first")
        if len(cal) < 100:
            st.warning(f"PENDING — {len(cal)} unique settled markets; at least 100 required for preliminary evaluation.")
        else:
            calibration_brier = float(np.mean((cal["model_probability"] - cal["won"]) ** 2))
            base_rate = float(cal["won"].mean())
            baseline_brier = float(np.mean((base_rate - cal["won"]) ** 2))
            st.metric("Out-of-sample markets", len(cal))
            st.metric("Brier score (lower is better)", f"{calibration_brier:.4f}")
            st.metric("Constant-rate baseline Brier", f"{baseline_brier:.4f}")
            calibration_valid = calibration_brier < baseline_brier
            calibration_df = cal
            st.write("Preliminary historical calibration test:",
                     "PASS" if calibration_valid else "FAIL — model did not beat constant-rate baseline")
            st.caption("This basic Brier comparison is not proof of calibration, stationarity, "
                       "out-of-sample provenance, or a live trading edge.")
    except Exception as exc:
        st.error(f"Calibration CSV invalid: {exc}")
else:
    st.info("PENDING — Upload timestamped predictions and official settled outcomes to evaluate calibration.")

# 4 — Illustrative expected value; only use calibrated empirical frequency
# from sufficiently populated nearby probability bins for any decision assessment.
st.subheader("4. Fee-Adjusted Expected Value")
model_probability = None
empirical_probability = None
estimated_ev = None
if chosen is not None and spot is not None and sigma is not None and sigma > 0 and bias is not None:
    _, remaining, target = chosen
    up = probability_up(spot, target, remaining, sigma)
    if up is not None and np.isfinite(up):
        model_probability = float(up if bias == "YES" else 1 - up)
        st.metric("Illustrative model probability", f"{model_probability * 100:.1f}%")

if model_probability is not None and calibration_valid and calibration_df is not None:
    # Local empirical hit rate around the current forecast; avoids treating a
    # global Brier pass as sufficient evidence for this specific probability.
    nearby = calibration_df[
        (calibration_df["model_probability"] - model_probability).abs() <= 0.10
    ]
    if len(nearby) >= 30:
        empirical_probability = float(nearby["won"].mean())
        st.write(f"Historical wins near this forecast: {len(nearby)} markets")
        st.metric("Local empirical win frequency", f"{empirical_probability * 100:.1f}%")
    else:
        st.warning(f"PENDING — Only {len(nearby)} historical observations near this forecast; 30 required.")

if empirical_probability is not None and contract_price is not None:
    # $1 payout on a winning contract; price and friction are per contract.
    estimated_ev = empirical_probability - contract_price - fee
    st.metric("Illustrative net EV per contract", f"${estimated_ev:+.3f}")
    st.write("Meets chosen minimum estimated edge:",
             "YES" if estimated_ev >= min_edge / 100 else "NO")
else:
    st.info("PENDING — Needs an eligible live ask and sufficient historical calibration data.")
st.caption("Assumed fee/slippage comes from the sidebar, not a verified Kalshi fee schedule. "
           "The model omits settlement-index basis, late-window behavior, fills, and latency.")

st.divider()
st.subheader("Final BTC Research Status")
# A genuine live settlement-index feed, externally validated calibration and
# confirmed contract-specific fees are NOT supplied by this application.
settlement_benchmark_verified = False
live_probability_verified = False
actual_fee_schedule_verified = False
all_checks_pass = all([
    indicator_pass, time_pass, price_pass, volatility_screen_pass,
    distance_screen_pass, calibration_valid, live_probability_verified,
    settlement_benchmark_verified, actual_fee_schedule_verified,
    estimated_ev is not None and estimated_ev >= min_edge / 100,
])
if all_checks_pass:
    st.success(f"Verified research conditions: {bias}")
else:
    st.error("NO TRADE — Essential checks are missing or unverified")
st.caption("Research-only, manual trading. A provisional screen or uploaded backtest "
           "cannot certify a live Kalshi settlement outcome or profitability.")
