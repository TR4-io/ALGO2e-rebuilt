"""Summarize a recorded run without importing the trading client or connecting to RIT."""
import argparse
from collections import defaultdict
import json


def summarize(path):
    totals = defaultdict(int)
    drift = defaultdict(lambda: [0., 0])
    last_status = end = None
    errors = []
    with open(path, encoding='utf-8') as f:
        for line_number, line in enumerate(f, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f'Malformed log line {line_number}; possibly interrupted write')
                continue
            kind = event['event']
            if kind == 'fill':
                totals[event['ticker']] += event['quantity']
                totals['intent_'+event['intent']] += event['quantity']
            elif kind == 'markout':
                key = f"{event['ticker']} {event['action']} {event['intent']} {event['horizon']}s"
                drift[key][0] += event['post_detection_drift']*event['quantity']
                drift[key][1] += event['quantity']
            elif kind == 'status':
                last_status = event
            elif kind == 'end':
                end = event
            elif kind == 'error':
                errors.append(event['message'])
    return dict(file=str(path), completed=end is not None, final=end, latest_status=last_status,
                confirmed_fill_volume=dict(totals),
                post_detection_drift_per_share={k: v[0]/v[1] for k, v in drift.items() if v[1]},
                errors=errors,
                note='Drift is measured from fill detection, excludes fees, and is not realized P&L. Use official RIT results for grading.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', nargs='+')
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2))
