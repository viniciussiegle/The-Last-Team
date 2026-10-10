import time

from ..platform import seconds_left


def deadlines(run, settings):
    duration = min(float(settings.run_budget_s), seconds_left(run) - 10)
    started = time.monotonic()
    end = started + max(0, duration)
    freeze = end - settings.submit_margin_s
    return max(started, freeze), end
