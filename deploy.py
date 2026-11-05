"""deploy — залить прибор на VPS, запустить, посмотреть, остановить, забрать данные.

Доступ НЕ лежит в коде: пароль уровня всей машины (а там payment_api) в репозитории —
это подарок тому, кто первым прочитает файл. Берём извне, по порядку:
  1) переменные окружения PUMP_VPS_HOST / PUMP_VPS_USER / PUMP_VPS_PASS;
  2) файл %USERPROFILE%\\.pump_sniper_vps.json ({"host","user","password","remote"});
  3) SSH-ключ из %USERPROFILE%\\.ssh, если пароля нет вовсе.

    python deploy.py push
    python deploy.py start --hours 12 --cycle 900 --track 1800 --poll 3 --buy 0.055
    python deploy.py status
    python deploy.py stop
    python deploy.py collect
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import paramiko

REMOTE_DEFAULT = "/home/deploy/pump_sniper"
CFG_DEFAULT = Path(os.path.expanduser("~")) / ".pump_sniper_vps.json"
ROOT = Path(__file__).resolve().parent
FILES = ("sniper_lib.py", "strategy.py", "paper_sniper.py", "run_loop.py",
         "analyze.py", "test_strategy.py")


def creds(path: Path | None = None) -> dict:
    """Доступ из окружения или файла вне проекта. В коде секретов нет."""
    cfg = {"host": os.environ.get("PUMP_VPS_HOST", ""), "user": os.environ.get("PUMP_VPS_USER", ""),
           "password": os.environ.get("PUMP_VPS_PASS", ""), "remote": REMOTE_DEFAULT}
    p = Path(path) if path else CFG_DEFAULT
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            for k in ("host", "user", "password", "remote"):
                if not cfg.get(k) and loaded.get(k):
                    cfg[k] = loaded[k]
        except Exception as e:
            raise SystemExit("файл доступа %s не читается: %s" % (p, e))
    if not cfg["host"] or not cfg["user"]:
        raise SystemExit("нет доступа к VPS: задайте PUMP_VPS_HOST/USER/PASS или %s" % p)
    return cfg


def client(cfg=None) -> paramiko.SSHClient:
    cfg = cfg or creds()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"username": cfg["user"], "timeout": 25}
    if cfg.get("password"):
        kw["password"] = cfg["password"]
    c.connect(cfg["host"], **kw)
    return c


def sh(c, cmd: str, timeout: int = 60) -> str:
    _i, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return (out + ("\n[stderr] " + err if err.strip() else "")).strip()


def push(c, remote: str) -> None:
    """Заливка через временное имя и атомарный mv.

    Раньше файл переписывался на месте, а цикл стартует каждые 45 минут: полузалитый
    файл в момент старта — это упавший цикл и дыра в статистике. mv в той же папке
    атомарен, поэтому цикл видит либо старую версию целиком, либо новую целиком.
    """
    sftp = c.open_sftp()
    sh(c, "mkdir -p %s/data" % remote)
    for name in FILES:
        local = ROOT / name
        tmp = "%s/%s.new" % (remote, name)
        sftp.put(str(local), tmp)
        sh(c, "mv -f %s %s/%s" % (tmp, remote, name))
        print("  ↑ %s (%d Б)" % (name, local.stat().st_size))
    sftp.close()


def start(c, remote: str, hours: float, cycle: float, track: float, poll: float,
          buy: float, discount: float, young_poll: float) -> None:
    busy = sh(c, "pgrep -f '[r]un_loop.py' || true")
    if busy:
        print("уже идёт (pid %s) — сначала stop" % busy.split()[0])
        return
    # подпроцесс в подоболочке: иначе exec_command не получает EOF и чтение висит
    # до таймаута (проверено 03.11 — запуск при этом проходит, но доклад ломается)
    cmd = ("( cd %s && setsid nohup nice -n 19 python3 run_loop.py --hours %g "
           "--cycle-seconds %g --track %g --poll %g --buy %g --exit-discount %g --young-poll %g "
           ">> data/run_loop.log 2>&1 < /dev/null & ) ; sleep 2 ; "
           "pgrep -af '[r]un_loop.py' || echo 'не поднялся'"
           % (remote, hours, cycle, track, poll, buy, discount, young_poll))
    try:
        print(sh(c, cmd, timeout=20))
    except Exception as e:
        print("  (вывод запуска не прочитался: %s — проверяю процесс отдельно)" % type(e).__name__)
        print(sh(c, "pgrep -af '[r]un_loop.py' || echo 'не поднялся'", timeout=20))
    time.sleep(4)
    print(sh(c, "pgrep -af '[r]un_loop.py|[p]aper_sniper.py' | head -3"))


def status(c, remote: str) -> None:
    print("процессы:")
    print(sh(c, "pgrep -af '[r]un_loop.py|[p]aper_sniper.py' | head -5 || echo '  (нет)'"))
    print("\nфайлы:")
    print(sh(c, "ls -la %s/data 2>/dev/null | tail -6" % remote))
    print("\nстрок лога и токенов:")
    print(sh(c, "wc -l %s/data/paper.jsonl %s/data/run_loop.log 2>/dev/null" % (remote, remote)))
    print("\nпоследний итог цикла:")
    print(sh(c, "tail -18 %s/data/run_loop.log 2>/dev/null" % remote))
    print("\nдиск и нагрузка:")
    print(sh(c, "df -h / | tail -1; uptime"))


def stop(c) -> None:
    print(sh(c, "pkill -f '[r]un_loop.py'; sleep 1; pkill -f '[p]aper_sniper.py'; sleep 1; "
                "pgrep -af '[r]un_loop.py|[p]aper_sniper.py' || echo 'остановлено'"))


def collect(c, remote: str) -> None:
    sftp = c.open_sftp()
    dest = ROOT / "data"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("paper.jsonl", "summary.jsonl", "run_loop.log"):
        try:
            size = sftp.stat("%s/data/%s" % (remote, name)).st_size
        except IOError:
            print("  нет на VPS: %s" % name)
            continue
        sftp.get("%s/data/%s" % (remote, name), str(dest / name))
        print("  ↓ %s (%d КБ)" % (name, size // 1024))
    sftp.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["push", "start", "status", "stop", "collect", "all"])
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--cycle", type=float, default=900)
    ap.add_argument("--track", type=float, default=1800)
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--buy", type=float, default=0.055)
    ap.add_argument("--exit-discount", type=float, default=0.02)
    ap.add_argument("--young-poll", type=float, default=0.5)
    ap.add_argument("--config", default=None, help="свой файл доступа вместо ~/.pump_sniper_vps.json")
    a = ap.parse_args()

    cfg = creds(Path(a.config) if a.config else None)
    remote = cfg.get("remote") or REMOTE_DEFAULT
    c = client(cfg)
    try:
        if a.cmd in ("push", "all"):
            push(c, remote)
        if a.cmd == "start":
            start(c, remote, a.hours, a.cycle, a.track, a.poll, a.buy, a.exit_discount, a.young_poll)
        elif a.cmd == "status":
            status(c, remote)
        elif a.cmd == "stop":
            stop(c)
        elif a.cmd == "collect":
            collect(c, remote)
        elif a.cmd == "all":
            start(c, remote, a.hours, a.cycle, a.track, a.poll, a.buy, a.exit_discount, a.young_poll)
            status(c, remote)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
