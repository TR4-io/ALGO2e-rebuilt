"""Run synthetic integration scenarios. Results do not estimate competition P&L."""
import json
from tests.test_round import exercise

if __name__ == '__main__':
    results = [exercise(seed, regime) for regime in ('balanced', 'crowded', 'trend', 'churn', 'one_sided')
               for seed in (11, 29, 53, 71, 97)]
    print(json.dumps({'warning': 'Stylized execution stress checks, not calibrated performance evidence.',
                      'runs': results}, indent=2))
