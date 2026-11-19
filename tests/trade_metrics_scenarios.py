"""Run deterministic, offline request-count scenarios; print only fixed labels and counters."""
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_trade_metrics import TradeMetrics

results={}
for scenario in ['cold_and_repeat_unique_currency_consumable_rare',
                 'parallel_identical_evaluations_share_requests',
                 'parallel_429_is_shared_without_retrying',
                 '429_and_retry_after_prevent_followup_request',
                 'league_change_is_a_new_search_not_a_duplicate',
                 'rare_relaxation_budget','reference_and_transport_cache_events']:
    test=TradeMetrics('test_'+scenario)
    test.setUp()
    try:
        getattr(test,'test_'+scenario)()
        results[scenario]={'network':dict(test.sent),'evaluations':test.measurements}
    finally:
        test.doCleanups()
print(json.dumps(results,indent=2,sort_keys=True))
