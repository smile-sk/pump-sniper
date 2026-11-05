"""run_loop — гоняет paper_sniper циклами, сколько скажут часов.

Каждый цикл — отдельный процесс: если один упадёт, следующий стартует сам.
Так ночью не бывает «бот молча умер в 2:15».

    python run_loop.py --hours 12 --cycle-seconds 900 --track 1800 --poll 3 --buy 0.055

Логи: data/run_loop.log (один и тот же файл, дописывается).
Если stdout уже перенаправлен в этот файл, второй записи не делаем — иначе каждая
строка ложится дважды.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "run_loop.log"


def say(text: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout.isatty():
        print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--cycle-seconds", type=float, default=720)
    ap.add_argument("--track", type=float, default=1800,
                    help="окно наблюдения; 1800 = таймаут лестницы из STRATEGY §4")
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--buy", type=float, default=0.055,
                    help="ставка; 0.02 ≠ живая 0.055 — комиссии дают разный процент (ревью)")
    ap.add_argument("--exit-discount", type=float, default=0.02,
                    help="скидка на выход: продажа двигает кривую против нас")
    ap.add_argument("--young-poll", type=float, default=0.5,
                    help="частый опрос молодых токенов, чтобы задержки 0.5/1.5/3 с не схлопывались")
    ap.add_argument("--max-track", type=int, default=150)
    a = ap.parse_args()

    stop_at = time.time() + a.hours * 3600
    cycle = 0
    say("старт циклами: %.1f ч, цикл %.0f с сбора + %.0f с наблюдения" %
        (a.hours, a.cycle_seconds, a.track))
    while time.time() < stop_at:
        cycle += 1
        say("цикл %d начался" % cycle)
        cmd = [sys.executable, str(ROOT / "paper_sniper.py"),
               "--seconds", str(a.cycle_seconds), "--track", str(a.track),
               "--poll", str(a.poll), "--buy", str(a.buy), "--max-track", str(a.max_track),
               "--exit-discount", str(a.exit_discount), "--young-poll", str(a.young_poll)]
        try:
            # вывод цикла пишем сами: полагаться на перенаправление оболочки нельзя —
            # проверили 03.11, второй '> /dev/null' в строке запуска молча съел весь отчёт
            with LOG.open("a", encoding="utf-8") as fh:
                rc = subprocess.call(cmd, cwd=str(ROOT), stdout=fh, stderr=fh)
            say("цикл %d закончился, код %d" % (cycle, rc))
        except Exception as e:
            say("цикл %d упал: %s: %s" % (cycle, type(e).__name__, str(e)[:200]))
        time.sleep(15)
    say("время вышло, циклов сделано %d" % cycle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
