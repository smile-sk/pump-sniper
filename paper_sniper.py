"""paper_sniper — бумажный снайпер pump.fun. Денег не тратит вообще.

Слушает поток создания токенов, читает bonding curve и записывает ЦЕНУ ВО ВРЕМЕНИ
(и вместе с ней — сколько SOL реально куплено, чтобы когорты считались по моменту
входа, а не по концу окна).

Вход моделируется по цене через N секунд после первого взгляда (столько занимает
реальная транзакция). Выход считается ОДНОЙ стратегией — лестницей §4 из STRATEGY.md
(модуль strategy.py), а не сеткой «а что если»: сетка осталась только разведкой.

Траектория пишется КАЖДЫМ опросом, без прореживания: раньше старше 120 с точки
ложились раз в 10 с, и касание стопа внутри интервала бумага не видела, а живой
стоп сработал бы (нашла Орест). Гранулярность теперь = период опроса, и это
единственная честная граница, которую мы можем назвать.

Запуск:
    python paper_sniper.py --seconds 900 --track 1800 --poll 3 --buy 0.055
    python paper_sniper.py --seconds 300 --track 120 --poll 1 --max-track 60

Пишет data/paper.jsonl (строка на токен) и data/summary.jsonl (строка на прогон).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

import strategy
from sniper_lib import (PUMP_PROGRAM, RPC_HTTP, RPC_WS, curve_for_mint, has_create_event,
                        parse_create_event, parse_curve)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "paper.jsonl"
SUMMARY = DATA / "summary.jsonl"

DELAYS = (0.5, 1.5, 3.0, 5.0)   # задержки входа, которые проверяем на одной траектории


def log(text: str) -> None:
    print(text, flush=True)


class Paper:
    def __init__(self, args):
        self.a = args
        self.client = httpx.AsyncClient(timeout=20)
        self.seen = 0
        self.no_curve = 0
        self.errors = 0
        self.tracked = {}
        self.sigs = set()
        self.dropped = 0          # сколько токенов не влезло в max-track (смещение выборки)
        self.results = []
        self.started = time.time()
        DATA.mkdir(parents=True, exist_ok=True)

    # ---------------- rpc ----------------
    async def rpc(self, method: str, params):
        for attempt in range(3):
            try:
                r = await self.client.post(RPC_HTTP, json={"jsonrpc": "2.0", "id": 1,
                                                           "method": method, "params": params})
                if r.status_code == 429:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                r.raise_for_status()
                d = r.json()
                if d.get("error"):
                    raise RuntimeError(str(d["error"])[:160])
                return d.get("result")
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.4)
        return None

    async def accounts(self, addrs):
        """getMultipleAccounts пачками по 50 — публичный узел не любит длинные запросы."""
        out = []
        for i in range(0, len(addrs), 50):
            chunk = addrs[i:i + 50]
            try:
                res = await self.rpc("getMultipleAccounts",
                                     [chunk, {"encoding": "base64", "commitment": "processed"}])
            except Exception:
                self.errors += 1
                out.extend([None] * len(chunk))
                continue
            for item in (res or {}).get("value") or []:
                out.append(None if not item else base64.b64decode(item["data"][0]))
            while len(out) < i + len(chunk):
                out.append(None)
        return out

    # ---------------- discovery ----------------
    async def on_logs(self, value):
        logs = value.get("logs") or []
        if not has_create_event(logs):
            return
        sig = value.get("signature") or ""
        if sig in self.sigs:
            return
        if len(self.tracked) >= self.a.max_track:
            # молча терять нельзя: иначе выборка каждого цикла смещается к его началу
            self.dropped += 1
            return
        self.sigs.add(sig)
        ev = parse_create_event(logs) or {}
        mint = ev.get("mint")
        if not mint:
            return
        self.seen += 1
        self.tracked[mint] = {
            "mint": mint, "curve": curve_for_mint(mint), "symbol": ev.get("symbol"),
            "name": ev.get("name"), "t0": time.time(), "sig": sig,
            "path": [], "first": None, "graduate_at": None, "last_kept": -99.0,
        }

    # ---------------- tracking ----------------
    async def track_once(self):
        now = time.time()
        for mint in list(self.tracked):
            if now - self.tracked[mint]["t0"] > self.a.track:
                self.finish(mint, "window")
        if not self.tracked:
            return
        mints = list(self.tracked)[: self.a.max_track]
        blobs = await self.accounts([self.tracked[m]["curve"] for m in mints])
        for mint, blob in zip(mints, blobs):
            t = self.tracked.get(mint)
            if not t:
                continue
            dt = now - t["t0"]
            if not blob:
                if dt > 8 and t["first"] is None:   # транзакция не удалась: кривой нет вовсе
                    self.no_curve += 1
                    self.tracked.pop(mint, None)
                continue
            st = parse_curve(blob)
            if not st:
                continue
            price = st["price_sol"]
            if t["first"] is None:
                t["first"] = price
            # пишем КАЖДЫЙ опрос: прореживание до 10 с после 120 с скрывало касание стопа
            # внутри интервала, то есть делало бумагу оптимистичнее жизни (нашла Орест).
            # рядом с ценой real_sol: без него когорта «сколько дев купил» считалась
            # по конечным резервам, то есть по самому исходу.
            t["path"].append([round(dt, 2), price, round(st["real_sol"], 4)])
            t["last_kept"] = dt
            t["complete"] = st["complete"]
            if st["complete"] and t["graduate_at"] is None:
                t["graduate_at"] = round(dt, 1)

    def finish(self, mint: str, reason: str):
        t = self.tracked.pop(mint, None)
        if not t or not t["path"]:
            return
        entry = self.a.entry_delay
        rec = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t["t0"])),
            "mint": mint, "symbol": t.get("symbol"), "name": t.get("name"),
            "first_price": t["first"], "points": len(t["path"]),
            "graduate_at": t.get("graduate_at"),
            # сол на момент ВХОДА, а не на конец окна
            "sol_entry": round(strategy.sol_at(t["path"], entry) or 0.0, 4),
            "sol_first": round(strategy.sol_at(t["path"], entry) or 0.0, 4),
            "reason": reason, "path": t["path"],
        }
        self.results.append(rec)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------------- отчёты ----------------
    def peaks(self, delay=None):
        d = self.a.entry_delay if delay is None else delay
        out = []
        for r in self.results:
            pk = strategy.peak_after_entry(r["path"], d)
            if pk:
                out.append(pk)
        return sorted(out)

    def _ladder(self, **kw):
        # значения по умолчанию — в словарь: вызов с fee_tx=... иначе дублирует ключ
        opts = {"timeout": min(strategy.TIMEOUT_S, self.a.track),
                "discount": self.a.exit_discount, "fee_tx": self.a.fee_tx,
                "entry_premium": self.a.entry_premium}
        opts.update(kw)
        return strategy.ladder_series(self.results, self.a.entry_delay, self.a.buy, **opts)

    def report(self):
        log("\n=== БУМАЖНЫЙ ПРОГОН: ИТОГ ===")
        log("окно сбора %.0f с, наблюдение %.0f с, ставка %.3f SOL"
            % (self.a.seconds, self.a.track, self.a.buy))
        log("новых токенов услышали: %d" % self.seen)
        log("довели до результата: %d  (без кривой: %d, не влезло в лимит: %d, ошибок RPC: %d)"
            % (len(self.results), self.no_curve, self.dropped, self.errors))
        if not self.results:
            log("НЕ ПРОВЕРЕНО: слишком короткое окно")
            return
        pk = self.peaks()
        log("пик ПОСЛЕ входа (x): медиана %.2f, мин %.2f, макс %.2f"
            % (statistics.median(pk), pk[0], pk[-1]))
        for lvl in (1.2, 1.5, 2.0, 3.0, 5.0):
            log("  дошли до x%.1f: %.0f%%" % (lvl, sum(1 for p in pk if p >= lvl) / len(pk) * 100))

        # ---- РЕШЕНИЕ: пре-регистрированная лестница, все комиссии и скидка на выход
        _sims, ladder_sum = self._ladder()
        log("\nЛЕСТНИЦА §4 (50%% x1.5 / 25%% x2 / 25%% x3, стоп x0.7, таймаут %.0f мин,"
            " скидка на выход %.1f%%, премия входа %.1f%%, чаевые %.4f SOL/tx,"
            " аренда ATA, 1 покупка + продажи + закрытие):"
            % (min(strategy.TIMEOUT_S, self.a.track) / 60.0, self.a.exit_discount * 100,
               self.a.entry_premium * 100, self.a.fee_tx))
        if ladder_sum.get("trades"):
            log("  сделок %d, вплюс %d (%.0f%%), стопов %d, таймаутов %d, транзакций на сделку %.1f"
                % (ladder_sum["trades"], ladder_sum["wins"], ladder_sum["winrate"] * 100,
                   ladder_sum["stops"], ladder_sum["timeouts"], ladder_sum["avg_txs"]))
            log("  PnL %.6f SOL, на сделку %.6f SOL (%.2f%% ставки)"
                % (ladder_sum["pnl_sol"], ladder_sum["per_trade_sol"], ladder_sum["per_trade_pct"]))
            ci = ladder_sum.get("ci95")
            if ci:
                log("  95%% ДИ (бутстрэп, %d итераций): от %+.2f%% до %+.2f%% на сделку"
                    % (ci["iters"], ci["lo"], ci["hi"]))
            verdict = ("ПЛЮС — есть смысл идти живым тестом" if ladder_sum["per_trade_pct"] >= 8
                       else ("около нуля — преимущества нет" if -8 < ladder_sum["per_trade_pct"] < 8
                             else "МИНУС — живые деньги не заводим"))
            log("  вердикт по §7: %s" % verdict)

        # ---- чувствительность: всё, что бумага не знает, играет ПРОТИВ нас
        log("\nЧУВСТВИТЕЛЬНОСТЬ (насколько результат держится на допущениях):")
        for ft in (0.0001, 0.0005, 0.001):
            _s, ss = self._ladder(fee_tx=ft)
            if ss.get("trades"):
                log("  чаевые %.4f SOL/tx → %+.2f%% на сделку" % (ft, ss["per_trade_pct"]))
        for ep in (0.0, 0.01, 0.02):
            _s, ss = self._ladder(entry_premium=ep)
            if ss.get("trades"):
                log("  премия входа %.0f%% → %+.2f%% на сделку" % (ep * 100, ss["per_trade_pct"]))

        # ---- разведка: сетка задержек и all-in пар. Решение по ней НЕ принимается.
        log("\nРАЗВЕДКА (all-in, не критерий решения):")
        log("  задержка  TP/SL        сделок  вплюс  на сделку %")
        explore = []
        for delay in DELAYS:
            for tp, sl in ((1.5, 0.7), (2.0, 0.7), (3.0, 0.6)):
                sims = [strategy.simulate_all_in(r["path"], delay, tp, sl, self.a.buy,
                                                 timeout=min(strategy.TIMEOUT_S, self.a.track),
                                                 discount=self.a.exit_discount,
                                                 fee_tx=self.a.fee_tx,
                                                 entry_premium=self.a.entry_premium)
                        for r in self.results]
                sims = [s for s in sims if s]
                if not sims:
                    continue
                pnl = sum(s["net_sol"] for s in sims)
                wins = sum(1 for s in sims if s["net_sol"] > 0)
                pct = pnl / len(sims) / self.a.buy * 100
                log("  %4.1fs  +%3.0f%%/-%2.0f%%  %5d  %5d  %9.2f"
                    % (delay, (tp - 1) * 100, (1 - sl) * 100, len(sims), wins, pct))
                explore.append({"delay": delay, "tp": tp, "sl": sl, "trades": len(sims),
                                "wins": wins, "per_trade_pct": round(pct, 3)})

        run_summary = dict(ladder_sum)          # копия: иначе словарь сошлётся сам на себя
        run_summary.update({
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "seconds": self.a.seconds, "track": self.a.track, "buy": self.a.buy,
            "entry_delay": self.a.entry_delay, "exit_discount": self.a.exit_discount,
            "fee_tx": self.a.fee_tx, "entry_premium": self.a.entry_premium,
            "seen": self.seen, "done": len(self.results), "no_curve": self.no_curve,
            "dropped": self.dropped, "errors": self.errors,
            "peak_median": round(statistics.median(pk), 3),
            "share_1_2": round(sum(1 for p in pk if p >= 1.2) / len(pk), 3),
            "share_1_5": round(sum(1 for p in pk if p >= 1.5) / len(pk), 3),
            "share_2_0": round(sum(1 for p in pk if p >= 2.0) / len(pk), 3),
            "share_3_0": round(sum(1 for p in pk if p >= 3.0) / len(pk), 3),
            "graduated": sum(1 for r in self.results if r.get("graduate_at")),
            "ladder": dict(ladder_sum),  # решение принимается ТОЛЬКО по этому блоку
            "explore": explore,        # разведка; best_pnl как критерий УБРАН (data snooping)
        })
        with SUMMARY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run_summary, ensure_ascii=False) + "\n")
        log("\nлог: %s" % OUT)

    # ---------------- run ----------------
    async def run(self):
        end = time.time() + self.a.seconds
        ticker = asyncio.create_task(self._ticker())
        while time.time() < end:
            try:
                async with websockets.connect(RPC_WS, ping_interval=20, open_timeout=20,
                                              max_queue=16384) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [PUMP_PROGRAM]},
                                                         {"commitment": "processed"}]}))
                    await asyncio.wait_for(ws.recv(), 20)
                    reader = asyncio.create_task(self._reader(ws))
                    while time.time() < end and not reader.done():
                        await asyncio.sleep(0.5)
                    if reader.done() and not reader.cancelled():
                        try:
                            reader.result()
                        except Exception as e:
                            log("подписка оборвалась (%s) — переподключаюсь" % type(e).__name__)
                    reader.cancel()
            except Exception as e:
                log("подключение не поднялось: %s: %s" % (type(e).__name__, str(e)[:120]))
                await asyncio.sleep(1.5)
        ticker.cancel()
        try:
            await ticker
        except BaseException:
            pass
        tail_end = time.time() + self.a.track + 2
        while self.tracked and time.time() < tail_end:
            await self.track_once()
            await asyncio.sleep(self._poll_delay())
        for mint in list(self.tracked):
            self.finish(mint, "cut")

    def _poll_delay(self) -> float:
        """Молодые токены опрашиваем часто: при poll=3 лестница задержек 0.5/1.5/3 с
        брала цену из одной точки (нашла Орест). Взрослые — как задано."""
        if self.a.young_poll <= 0:
            return self.a.poll
        now = time.time()
        for t in self.tracked.values():
            if now - t["t0"] < self.a.young_window:
                return min(self.a.young_poll, self.a.poll)
        return self.a.poll

    async def _reader(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            v = (((msg.get("params") or {}).get("result") or {}).get("value") or {})
            if "logs" in v:
                await self.on_logs(v)

    async def _ticker(self):
        last = time.time()
        while True:
            try:
                await self.track_once()
            except Exception as e:
                self.errors += 1
                log("опрос кривых упал: %s: %s" % (type(e).__name__, str(e)[:120]))
            if time.time() - last > 60:
                last = time.time()
                log("… живём: поймано %d, в наблюдении %d, готовых %d, без кривой %d, "
                    "не влезло %d, ошибок %d"
                    % (self.seen, len(self.tracked), len(self.results), self.no_curve,
                       self.dropped, self.errors))
            await asyncio.sleep(self._poll_delay())

    async def close(self):
        await self.client.aclose()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300, help="сколько слушать новые токены")
    ap.add_argument("--track", type=float, default=1800, help="сколько следить за ценой")
    ap.add_argument("--poll", type=float, default=3.0, help="период опроса кривой, с")
    ap.add_argument("--young-poll", type=float, default=0.5,
                    help="период опроса для токенов младше young-window (0 = не ускорять)")
    ap.add_argument("--young-window", type=float, default=10.0,
                    help="сколько секунд жизни токена опрашивать часто")
    ap.add_argument("--entry-delay", type=float, default=1.5, help="базовая задержка входа")
    ap.add_argument("--buy", type=float, default=0.055, help="размер входа, SOL")
    ap.add_argument("--exit-discount", type=float, default=strategy.EXIT_DISCOUNT,
                    help="скидка на выход (продажа двигает кривую против нас)")
    ap.add_argument("--entry-premium", type=float, default=strategy.ENTRY_PREMIUM,
                    help="премия на вход (покупка двигает кривую вверх); 0 = как в §4")
    ap.add_argument("--fee-tx", type=float, default=strategy.FEE_TX_SOL,
                    help="чаевые валидатору на транзакцию, SOL (в горячий час бывает 0.001)")
    ap.add_argument("--max-track", type=int, default=150)
    a = ap.parse_args()

    async def go():
        p = Paper(a)
        try:
            await p.run()
        finally:
            p.report()
            await p.close()

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    sys.exit(main())
