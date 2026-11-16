"""Causal triple-barrier samples built from local MT5 history Parquets."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import numpy as np
import pandas as pd

from .memory import situation_vector
from .training import label_trade, split_name
from .. import parquet_compat


def _as_utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    result=frame.sort_index().copy()
    index=pd.DatetimeIndex(result.index)
    result.index=index.tz_localize('UTC') if index.tz is None else index.tz_convert('UTC')
    return result


def _context(m5: pd.DataFrame, m15: pd.DataFrame) -> dict:
    """Calculate scale-free context from bars already closed at the sample time."""
    def scaled(frame, bars):
        close=frame.close.astype(float)
        atr=float((frame.high.astype(float)-frame.low.astype(float)).tail(14).mean())
        price=float(close.iloc[-1])
        if not np.isfinite(atr) or atr<=0 or price<=0: return 0.0
        return float(np.clip((price-float(close.iloc[-bars-1]))/atr,-5,5)) if len(close)>bars else 0.0
    return {'trend':scaled(m15,8),'momentum':scaled(m5,5),
            'volatility':float(np.clip((m15.high-m15.low).tail(14).mean()/max(float(m15.close.iloc[-1]),1e-12)*10000,0,5)),
            'support_distance_atr':0.0,'resistance_distance_atr':0.0,
            'minutes_to_next_event':None}


def build_labeled_outcomes_slow(history_dir: str|Path, symbols: list[str], specs: dict,
                                split: dict, config: dict, sample_minutes: int=30) -> pd.DataFrame:
    """Reference implementation (row by row). Kept to prove the fast version gives the same rows."""
    history=Path(history_dir); rows=[]
    focus=set(config['universe'].get('focus_symbols',[]))
    weekend_symbols=set(config['universe'].get('weekend_symbols',[]))
    aliases=config['universe'].get('symbol_aliases',{})
    logical_for_actual: dict[str, str] = {}
    for logical in (*focus,*weekend_symbols):
        for pattern in aliases.get(logical,[logical]):
            logical_for_actual.setdefault(str(pattern).upper(),logical)
    weekend_actual=set()
    for symbol in symbols:
        actual=symbol.upper()
        if any(pattern in actual for name in weekend_symbols for pattern in aliases.get(name,[name])):
            weekend_actual.add(symbol)
    risk=config['risk']; backtest=config.get('backtest',{})
    forex_path=history/'EURUSD_M1.parquet'
    forex_index=None
    if forex_path.is_file():
        forex_frame=_as_utc_index(parquet_compat.read_parquet(forex_path,columns=['close']))
        forex_index=forex_frame.index
    def forex_open_at(stamp):
        if stamp.weekday()<5: return True
        if forex_index is None: return False
        pos=int(forex_index.searchsorted(stamp,side='right'))-1
        return pos>=0 and stamp-forex_index[pos]<=pd.Timedelta(minutes=2)
    for symbol in symbols:
        paths={tf:history/f'{symbol}_{tf}.parquet' for tf in ('M1','M5','M15')}
        if not all(path.is_file() for path in paths.values()): continue
        frames={tf:_as_utc_index(parquet_compat.read_parquet(path)) for tf,path in paths.items()}
        m1,m5,m15=frames['M1'],frames['M5'],frames['M15']
        spec=specs.get(symbol,{})
        group=str(spec.get('asset_group','other'))
        weekend=symbol in weekend_actual
        target_horizon=int(config.get('weekend',{}).get('move_horizon_minutes',60) if weekend else risk.get('move_horizon_minutes',10))
        timeout=int(config.get('weekend',{}).get('max_hold_extended_minutes',120) if weekend else risk.get('max_hold_extended_minutes',risk.get('max_hold_minutes',20)))
        atr=(m1['high'].astype(float)-m1['low'].astype(float)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        sample_ids=np.flatnonzero((m1.index.minute.to_numpy()%sample_minutes)==0)
        for decision_idx in sample_ids:
            entry_index=int(decision_idx)-1
            if entry_index<1 or entry_index+timeout>=len(m1): continue
            stamp=m1.index[decision_idx]
            active_weekend=stamp.weekday()>=5 and not forex_open_at(stamp)
            if weekend_actual and symbol in weekend_actual and not active_weekend: continue
            if symbol not in weekend_actual and active_weekend: continue
            # Use only completed M5/M15 bars whose close time is no later than the decision time.
            i5=int(m5.index.searchsorted(stamp-pd.Timedelta(minutes=5),side='right'))-1
            i15=int(m15.index.searchsorted(stamp-pd.Timedelta(minutes=15),side='right'))-1
            if i5<49 or i15<33: continue
            sample_m5=m5.iloc[:i5+1]; sample_m15=m15.iloc[:i15+1]
            context=_context(sample_m5,sample_m15)
            for direction in ('buy','sell'):
                vector=situation_vector({'M5':sample_m5,'M15':sample_m15},context,direction)
                atr_value=float(atr.iloc[entry_index])
                if not np.isfinite(atr_value) or atr_value<=0: continue
                label=label_trade(m1,entry_index,direction,atr_value,spec,
                    horizon_minutes=target_horizon,sl_mult=float(risk['sl_move_mult']),
                    tp_mult=float(risk['tp_move_mult']),slippage_points=float(backtest.get('slippage_points',0)),
                    timeout_minutes=timeout,
                    commission_per_lot_per_side=float(backtest.get('commission_per_lot_per_side',0)))
                if label is None: continue
                rows.append({'time':stamp.isoformat(),'symbol':symbol,'asset_group':group,
                    'weekend':weekend,'direction':direction,'vector':vector.tolist(),
                    'win':int(label['win']),'net_r':float(label['net_r']),'reason':label['reason'],
                    'entry':float(label['entry']),'exit':float(label['exit']),
                    'risk_distance':float(label['risk_distance']),'cost_distance':float(label['cost_distance']),
                    'split':split_name(stamp.to_pydatetime(),split)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# Fast version: the same rows as build_labeled_outcomes_slow, computed with NumPy for all
# samples of a symbol at once. Vectors are float32 rows of one matrix (no Python lists), so
# 16 years of M1 history fit in well under 1 GB.

def _utc_lean(frame: pd.DataFrame) -> pd.DataFrame:
    """_as_utc_index without copies when the file is already sorted UTC (the usual case)."""
    index=pd.DatetimeIndex(frame.index)
    if not index.is_monotonic_increasing: frame=frame.sort_index(); index=pd.DatetimeIndex(frame.index)
    if index.tz is None: frame.index=index.tz_localize('UTC')
    elif str(index.tz)!='UTC': frame.index=index.tz_convert('UTC')
    return frame


def _window_mean(values: np.ndarray, ends: np.ndarray, width: int) -> np.ndarray:
    """Mean of values[end-width+1 : end+1] for every end (end >= width-1)."""
    from numpy.lib.stride_tricks import sliding_window_view
    return sliding_window_view(values,width)[ends-width+1].mean(axis=1)


def _scaled_fast(close: np.ndarray, hl: np.ndarray, ends: np.ndarray, bars: int) -> np.ndarray:
    atr=_window_mean(hl,ends,14); price=close[ends]
    with np.errstate(divide='ignore',invalid='ignore'):
        value=np.clip((price-close[ends-bars])/atr,-5,5)
    ok=np.isfinite(atr)&(atr>0)&(price>0)
    return np.where(ok,value,0.0)


def _returns_fast(close: np.ndarray, hl: np.ndarray, ends: np.ndarray, count: int) -> np.ndarray:
    from numpy.lib.stride_tricks import sliding_window_view
    logs=np.log(np.maximum(close,1e-12)); steps=np.diff(logs)
    returns=sliding_window_view(steps,count)[ends-count]                 # steps[end-count : end]
    atr=_window_mean(hl,ends,14); price=close[ends]
    scale=np.maximum(atr/np.maximum(price,1e-12),1e-8)
    return np.clip(returns/scale[:,None],-5,5)


def _labels_fast(m1: pd.DataFrame, entry_index: np.ndarray, buy: bool, atr_value: np.ndarray, spec: dict,
                 horizon: int, sl_mult: float, tp_mult: float, slippage_points: float, timeout: int, commission: float):
    """Vectorised label_trade for many entries of one direction. Returns a dict of arrays (+ valid mask)."""
    point=float(spec.get('point_size',spec.get('point',spec.get('trade_tick_size',1e-5))) or 1e-5)
    tick_size=float(spec.get('trade_tick_size',spec.get('tick_size',point)) or point)
    n=len(entry_index); first=entry_index+1
    opens=m1['open'].to_numpy(float); highs=m1['high'].to_numpy(float); lows=m1['low'].to_numpy(float); closes=m1['close'].to_numpy(float)
    spread_points=m1['spread'].to_numpy(float)[first] if 'spread' in m1 else np.zeros(n)
    spread=np.maximum(0.0,spread_points)*point; slip=max(0.0,float(slippage_points))*point
    sign=1.0 if buy else -1.0
    entry=opens[first]+(spread if buy else 0.0)+slip*sign
    root=np.sqrt(max(1.0,float(horizon)))
    risk=atr_value*root*float(sl_mult); reward=atr_value*root*float(tp_mult)+spread
    valid=(atr_value>0)&(risk>0)&(tick_size>0)&(first<len(m1))
    stop=entry-sign*risk; target=entry+sign*reward
    limit=int(max(1,timeout))
    times=m1.index.tz_convert(None).to_numpy().astype('datetime64[ns]').astype(np.int64)
    deadline=times[first]+np.int64(limit)*60_000_000_000
    stop_index=np.minimum(np.searchsorted(times,deadline,side='right'),len(m1))
    stop_index=np.maximum(stop_index,first)
    width=int((stop_index-first).max()) if n else 0
    exit_price=np.empty(n); reason=np.empty(n,dtype=object)
    chunk=20_000
    for start in range(0,n,chunk):
        part=slice(start,min(n,start+chunk))
        f=first[part]; e=stop_index[part]
        offsets=np.arange(width)
        positions=f[:,None]+offsets[None,:]
        inside=positions<e[:,None]
        safe=np.minimum(positions,len(m1)-1)
        lo=lows[safe]; hi=highs[safe]; sp=spread[part][:,None]
        if buy:
            sl_hit=(lo<=stop[part][:,None])&inside; tp_hit=(hi>=target[part][:,None])&inside
        else:
            sl_hit=(hi+sp>=stop[part][:,None])&inside; tp_hit=(lo+sp<=target[part][:,None])&inside
        big=width+1
        first_sl=np.where(sl_hit.any(axis=1),sl_hit.argmax(axis=1),big)
        first_tp=np.where(tp_hit.any(axis=1),tp_hit.argmax(axis=1),big)
        is_sl=(first_sl<big)&(first_sl<=first_tp); is_tp=(first_tp<big)&~is_sl
        last_close=closes[np.maximum(e-1,f)]
        timed=last_close+(0.0 if buy else 1.0)*spread[part]
        ex=np.where(is_sl,stop[part],np.where(is_tp,target[part],timed))
        exit_price[part]=ex
        reason[part]=np.where(is_sl,'sl',np.where(is_tp,'tp','time'))
    valid&=stop_index>first
    exit_price=exit_price-slip*sign
    net_distance=(exit_price-entry)*sign
    tick_value=float(spec.get('trade_tick_value_loss',spec.get('trade_tick_value',spec.get('tick_value',0))) or 0)
    if commission<0 or (commission and (tick_value<=0 or tick_size<=0)): valid&=False
    commission_distance=(2*commission/(tick_value/tick_size)) if commission and tick_value>0 and tick_size>0 else 0.0
    net_distance=net_distance-commission_distance
    with np.errstate(divide='ignore',invalid='ignore'):
        net_r=net_distance/risk
    return {'valid':valid,'win':(net_r>0).astype(int),'net_r':net_r,'reason':reason,'entry':entry,'exit':exit_price,
            'risk_distance':risk,'cost_distance':np.full(n,float(2*slip+commission_distance))}


def _labeled_parts(history_dir, symbols, specs, split, config, sample_minutes):
    """Yield (rows without 'vector', float32 vectors) per symbol, in the reference row order."""
    history=Path(history_dir)
    focus=set(config['universe'].get('focus_symbols',[]))
    weekend_symbols=set(config['universe'].get('weekend_symbols',[]))
    aliases=config['universe'].get('symbol_aliases',{})
    weekend_actual=set()
    for symbol in symbols:
        actual=symbol.upper()
        if any(pattern in actual for name in weekend_symbols for pattern in aliases.get(name,[name])):
            weekend_actual.add(symbol)
    risk=config['risk']; backtest=config.get('backtest',{})
    forex_path=history/'EURUSD_M1.parquet'
    forex_index=None
    if forex_path.is_file():
        forex_index=_as_utc_index(parquet_compat.read_parquet(forex_path,columns=['close'])).index
    train_end=np.datetime64(split['train_end']); val_start=np.datetime64(split['validation_start'])
    val_end=np.datetime64(split['validation_end']); test_start=np.datetime64(split['test_start'])
    for symbol in symbols:
        paths={tf:history/f'{symbol}_{tf}.parquet' for tf in ('M1','M5','M15')}
        if not all(path.is_file() for path in paths.values()): continue
        def load(path,wanted):
            columns=[c for c in wanted if c in parquet_compat.read_parquet_columns(path)]
            return _utc_lean(parquet_compat.read_parquet(path,columns=columns))
        m1=load(paths['M1'],['open','high','low','close','spread'])
        m5=load(paths['M5'],['high','low','close']); m15=load(paths['M15'],['high','low','close'])
        spec=specs.get(symbol,{}); group=str(spec.get('asset_group','other')); weekend=symbol in weekend_actual
        target_horizon=int(config.get('weekend',{}).get('move_horizon_minutes',60) if weekend else risk.get('move_horizon_minutes',10))
        timeout=int(config.get('weekend',{}).get('max_hold_extended_minutes',120) if weekend else risk.get('max_hold_extended_minutes',risk.get('max_hold_minutes',20)))
        atr=(m1['high'].astype(float)-m1['low'].astype(float)).ewm(alpha=1/14,adjust=False,min_periods=14).mean().to_numpy()
        ids=np.flatnonzero((m1.index.minute.to_numpy()%sample_minutes)==0)
        ids=ids[(ids-1>=1)&(ids-1+timeout<len(m1))]
        atr=atr[ids-1]                                   # only the entry bars are needed (frees the rest)
        if not len(ids): continue
        stamps=m1.index[ids]
        weekday=stamps.weekday.to_numpy()>=5
        if weekday.any():
            open_fx=np.zeros(len(ids),dtype=bool)
            if forex_index is not None:
                pos=forex_index.searchsorted(stamps[weekday],side='right')-1
                ok=pos>=0
                gap=np.full(len(pos),np.inf)
                gap[ok]=(stamps[weekday][ok]-forex_index[pos[ok]]).total_seconds()
                open_fx[weekday]=gap<=120
            active_weekend=weekday&~open_fx
        else:
            active_weekend=np.zeros(len(ids),dtype=bool)
        keep=active_weekend if (weekend_actual and weekend) else ~active_weekend
        i5=m5.index.searchsorted(stamps-pd.Timedelta(minutes=5),side='right')-1
        i15=m15.index.searchsorted(stamps-pd.Timedelta(minutes=15),side='right')-1
        keep&=(i5>=49)&(i15>=33)
        ids,stamps,i5,i15=ids[keep],stamps[keep],i5[keep],i15[keep]
        if not len(ids): continue
        c5=m5['close'].to_numpy(float); hl5=m5['high'].to_numpy(float)-m5['low'].to_numpy(float)
        c15=m15['close'].to_numpy(float); hl15=m15['high'].to_numpy(float)-m15['low'].to_numpy(float)
        trend=_scaled_fast(c15,hl15,i15,8); momentum=_scaled_fast(c5,hl5,i5,5)
        with np.errstate(divide='ignore',invalid='ignore'):
            volatility=np.clip(_window_mean(hl15,i15,14)/np.maximum(c15[i15],1e-12)*10000,0,5)
        context=np.zeros((len(ids),8))
        for column,values in ((1,trend),(2,momentum),(3,volatility)):
            context[:,column]=np.where(np.isfinite(values),np.clip(values,-5,5),0.0)
        base=np.hstack([_returns_fast(c5,hl5,i5,48),_returns_fast(c15,hl15,i15,32),context,np.full((len(ids),1),7.0)])
        entry_index=ids-1; atr_value=atr[keep]
        atr_ok=np.isfinite(atr_value)&(atr_value>0)
        texts=np.array([stamp.isoformat() for stamp in stamps],dtype=object)
        naive=stamps.tz_convert(None).to_numpy().astype('datetime64[ns]')
        splits=np.where(naive<=train_end,'train',np.where((naive>=val_start)&(naive<=val_end),'validation',np.where(naive>=test_start,'test','purged')))
        per_direction=[]
        for direction in ('buy','sell'):
            labels=_labels_fast(m1,entry_index,direction=='buy',np.where(atr_ok,atr_value,1.0),spec,target_horizon,
                float(risk['sl_move_mult']),float(risk['tp_move_mult']),float(backtest.get('slippage_points',0)),timeout,
                float(backtest.get('commission_per_lot_per_side',0)))
            labels['valid']&=atr_ok
            per_direction.append(labels)
        # Interleave buy/sell per sample exactly like the row-by-row version.
        valid=np.stack([per_direction[0]['valid'],per_direction[1]['valid']],axis=1)       # (n,2)
        sample_ids,direction_ids=np.nonzero(valid)                                          # row-major: sample, then buy<sell
        if not len(sample_ids): continue
        vectors=np.empty((len(sample_ids),base.shape[1]+1),dtype=np.float32)
        vectors[:,:-1]=base[sample_ids].astype(np.float32); vectors[:,-1]=np.where(direction_ids==0,1.0,-1.0)
        def pick(key):
            both=np.stack([per_direction[0][key],per_direction[1][key]],axis=1)
            return both[sample_ids,direction_ids]
        frame=pd.DataFrame({'time':texts[sample_ids],'symbol':symbol,'asset_group':group,'weekend':weekend,
            'direction':np.where(direction_ids==0,'buy','sell'),
            'win':pick('win').astype(int),'net_r':pick('net_r').astype(float),'reason':pick('reason'),
            'entry':pick('entry').astype(float),'exit':pick('exit').astype(float),
            'risk_distance':pick('risk_distance').astype(float),'cost_distance':pick('cost_distance').astype(float),
            'split':splits[sample_ids]})
        del per_direction,base
        yield frame,vectors


_ROW_COLUMNS=['time','symbol','asset_group','weekend','direction','vector','win','net_r','reason','entry','exit','risk_distance','cost_distance','split']


def build_labeled_outcomes(history_dir: str|Path, symbols: list[str], specs: dict,
                           split: dict, config: dict, sample_minutes: int=30) -> pd.DataFrame:
    """Label both directions at every available sampled timestamp without feature lookahead.

    Same rows, order and values as build_labeled_outcomes_slow; the 'vector' column holds
    float32 NumPy rows (len 90) instead of Python lists. For full history use
    build_labeled_outcomes_to_disk, which keeps the vectors out of memory."""
    parts=[]
    for frame,vectors in _labeled_parts(history_dir,symbols,specs,split,config,sample_minutes):
        frame['vector']=list(vectors); parts.append(frame[_ROW_COLUMNS])
    return pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()


def build_labeled_outcomes_to_disk(history_dir: str|Path, symbols: list[str], specs: dict, split: dict, config: dict,
                                   sample_minutes: int, vectors_file: str|Path) -> tuple[pd.DataFrame, np.ndarray]:
    """Same rows as build_labeled_outcomes, but the vectors go to a .npy file (memory-mapped).

    Returns (rows without 'vector', read-only memory-mapped float32 matrix). Peak memory is
    about one symbol's history, so 16 years of M1 bars fit on a small server."""
    vectors_file=Path(vectors_file); vectors_file.parent.mkdir(parents=True,exist_ok=True)
    frames=[]; pieces=[]
    for number,(frame,vectors) in enumerate(_labeled_parts(history_dir,symbols,specs,split,config,sample_minutes)):
        piece=vectors_file.with_name(f'{vectors_file.stem}.part{number}.npy'); np.save(piece,vectors)
        frames.append(frame); pieces.append((piece,vectors.shape)); del vectors
    total=sum(shape[0] for _,shape in pieces); width=pieces[0][1][1] if pieces else 0
    matrix=np.lib.format.open_memmap(vectors_file,mode='w+',dtype=np.float32,shape=(total,width))
    row=0
    for piece,shape in pieces:
        matrix[row:row+shape[0]]=np.load(piece); row+=shape[0]; piece.unlink()
    matrix.flush(); del matrix
    rows=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    return rows,np.load(vectors_file,mmap_mode='r')
