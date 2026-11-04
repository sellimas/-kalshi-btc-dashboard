import math
import numpy as np
import pandas as pd

def indicators(candles):
    """Seven directional votes from completed Coinbase 1-minute candles."""
    d = candles.copy().sort_values('time')
    if len(d) < 60:
        return None
    close = pd.to_numeric(d['close'], errors='coerce')
    high = pd.to_numeric(d['high'], errors='coerce')
    low = pd.to_numeric(d['low'], errors='coerce')
    volume = pd.to_numeric(d['volume'], errors='coerce')
    if pd.concat([close,high,low,volume],axis=1).tail(60).isna().any().any():
        return None
    ema9 = close.ewm(span=9,adjust=False).mean()
    ema20 = close.ewm(span=20,adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi = 100 - 100/(1+gain/(loss.replace(0,np.nan)))
    macd = close.ewm(span=12,adjust=False).mean()-close.ewm(span=26,adjust=False).mean()
    macd_signal = macd.ewm(span=9,adjust=False).mean()
    typical = (high+low+close)/3
    vwap = (typical.tail(30)*volume.tail(30)).sum()/volume.tail(30).sum() if volume.tail(30).sum()>0 else np.nan
    momentum = close.iloc[-1]-close.iloc[-6]
    vol = np.log(close).diff().tail(30).std()
    volume_ratio = volume.tail(5).mean()/volume.tail(30).mean() if volume.tail(30).mean()>0 else np.nan
    # Directional votes only; volatility and volume are quality gates, not directional predictors.
    votes = {
      'EMA 9/20': 1 if ema9.iloc[-1]>ema20.iloc[-1] else -1,
      'RSI 14': 1 if rsi.iloc[-1]>55 else (-1 if rsi.iloc[-1]<45 else 0),
      'MACD': 1 if macd.iloc[-1]>macd_signal.iloc[-1] else -1,
      '30m VWAP': 1 if close.iloc[-1]>vwap else -1,
      '5m momentum': 1 if momentum>0 else (-1 if momentum<0 else 0),
      '20m price slope': 1 if close.iloc[-1]>close.iloc[-21] else -1,
      '3m price slope': 1 if close.iloc[-1]>close.iloc[-4] else -1,
    }
    if not np.isfinite([vwap,vol,volume_ratio,rsi.iloc[-1]]).all():
        return None
    return {'votes':votes,'up':sum(v==1 for v in votes.values()),'down':sum(v==-1 for v in votes.values()),'vol':float(vol),'volume_ratio':float(volume_ratio)}
