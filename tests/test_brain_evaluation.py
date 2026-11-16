import pandas as pd
from src.brain.evaluation import evaluate_test_period

def _rows(n=30):
    return pd.DataFrame({'probability':[.7]*n,'actual_win':[1]*n,'net_r':[1.]*n,'symbol':[f'S{i%32}' for i in range(n)],'asset_group':['forex' if i%2 else 'indices' for i in range(n)],'subperiod':[f'p{i%3}' for i in range(n)],'checklist_only':[True]*n,'memory_only':[False]*n,'both':[True]*n,'current_rule':[False]*n})

def test_brain_gate_fails_closed_when_sample_is_too_small():
    result=evaluate_test_period(_rows(),{'acceptance':{'min_trades':500}})
    assert not result['passed'] and result['selected']['trades']==30
    assert 'fewer than required selected test trades' in result['reasons']

def test_brain_gate_requires_same_period_current_rule_baseline():
    rows=_rows(600); rows['current_rule']=False
    result=evaluate_test_period(rows,{'acceptance':{'min_trades':500,'min_symbols':7,'min_profit_factor':1.1,'calibration_error':.05}})
    assert not result['passed']
    assert 'no current rule-based strategy baseline was supplied for the same test period' in result['reasons']

def test_brain_gate_requires_all_test_columns():
    result=evaluate_test_period(pd.DataFrame({'net_r':[1]}),{})
    assert not result['passed'] and 'missing columns' in result['reasons'][0]


def _good(n=600, symbols=5):
    import numpy as np
    rows=_rows(n); rows['symbol']=[f'S{i%symbols}' for i in range(n)]
    rows['probability']=np.tile(np.linspace(.05,.95,10),n//10+1)[:n]; rows['actual_win']=(rows.probability>np.tile(np.linspace(0,1,7),n//7+1)[:n]).astype(int)
    rows['net_r']=np.where(rows.actual_win==1,1.2,-1.0); rows['subperiod']=[str(i%3) for i in range(n)]
    rows['current_rule']=True
    return rows

def test_min_symbols_follows_the_symbols_that_exist():
    rows=_good(600,5)
    result=evaluate_test_period(rows,{'acceptance':{'min_trades':500,'min_symbols':7}})
    assert 'fewer than required distinct symbols' not in result['reasons']
    rows=_good(600,5); rows.loc[rows.both,'symbol']='S0'
    result=evaluate_test_period(rows,{'acceptance':{'min_trades':500,'min_symbols':7}})
    assert 'fewer than required distinct symbols' in result['reasons']

def test_brain_must_beat_rule_baseline():
    rows=_good(600,5)
    rows['both']=rows.index%2==0; rows.loc[rows.both,'net_r']=-0.1                     # brain picks are worse
    result=evaluate_test_period(rows,{'acceptance':{'min_trades':100,'min_symbols':7}})
    assert 'brain selection does not beat the rule-based baseline on the same test period' in result['reasons']
