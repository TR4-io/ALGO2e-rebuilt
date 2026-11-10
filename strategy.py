"""Adaptive ALGO2e market maker. --check reads only; default starts live simulation trading."""
import argparse
from datetime import datetime
from pathlib import Path
import signal
import sys
import time

from config import Config
from engine import Bot, Recorder
from execution import Client, ApiError, HttpError, UnknownWrite, RateLimited


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--config', help='JSON overrides of Config defaults')
    parser.add_argument('--minimum-volume', type=int, help='Override per-run requirement only after instructor confirmation')
    parser.add_argument('--no-taker', action='store_true', help='Disable speculative liquidity-taking for comparison runs')
    args = parser.parse_args()
    cfg = Config.load(args.config)
    from dataclasses import replace
    if args.minimum_volume is not None:
        cfg = replace(cfg, minimum_volume=args.minimum_volume)
    if args.no_taker:
        cfg = replace(cfg, taker_enabled=False)
    cfg.validate()
    client = Client()
    stream = None
    bot = Bot(client, cfg)
    started = False
    previous = None
    try:
        if not args.check:
            Path('logs').mkdir(exist_ok=True)
            stream = open(Path('logs')/f'run-{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl', 'w', encoding='utf-8')
            bot.record = Recorder(stream)
        bot.start()
        started = True
        if args.check:
            print('Read-only check passed; no orders submitted or cancelled.')
            return 0
        def stop(signum, frame):
            if bot.stopping:
                raise KeyboardInterrupt
            bot.stopping = True
            print('Stopping new exposure and closing inventory; Ctrl+C again exits immediately.')
        previous = signal.signal(signal.SIGINT, stop)
        failures = 0
        while True:
            try:
                if not bot.step():
                    break
                failures = 0
            except RateLimited as exc:
                # No replay; a fresh case/book decision follows the server wait.
                print(exc)
                time.sleep(exc.wait)
                continue
            except UnknownWrite:
                raise
            except ApiError as exc:
                failures += 1
                bot.record.emit('error', message=str(exc))
                print(exc)
                if bot.round_changed or isinstance(exc, HttpError) and exc.status in (401, 403) or failures >= 5:
                    raise
                # Remove stale exposure before a retry when the connection allows it.
                bot.cancel_all()
                time.sleep(min(1, .2*failures))
            time.sleep(cfg.poll_seconds)
        return 0
    except (ApiError, KeyError, ValueError, TypeError) as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Immediate exit; inspect remaining positions in RIT.')
        return 130
    finally:
        if started and not args.check:
            bot.cleanup()
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if stream:
            stream.close()
        client.session.close()


if __name__ == '__main__':
    sys.exit(main())
