"""strategy — честная модель входа и выхода. ОДНА на прибор и на разбор.

Зачем отдельным файлом (ревью Ореста, 03.11.2026): выход считался в двух местах
(paper_sniper.py и analyze.py), и они разошлись — прибор мерил НЕ ту стратегию,
что записана в STRATEGY.md §4, а пики считались по ценам ДО нашего входа.
Один модуль = один закон, и разойтись им больше негде.

Что заложено:
  * лестница §4: 50% на x1.5, 25% на x2.0, 25% до x3.0, стоп по остатку x0.7, таймаут;
  * скидка на выход (по умолчанию 2%): продажа двигает кривую против нас;
  * премия на вход (по умолчанию 0%, параметр): покупка тоже двигает кривую —
    симметрично скидке на выход, но по умолчанию ВЫКЛЮЧЕНА, чтобы не двигать
    замороженную стратегию задним числом; чувствительность печатается отдельно;
  * стоп по гэпу: берём НАБЛЮДЁННУЮ цену после пробоя, а не ровно уровень;
  * тейк по гэпу вверх НЕ улучшаем: считаем по уровню, а не по лучшей цене (консервативно);
  * число транзакций считается по факту выходов (лестница = 3-4 tx, а не 2);
  * ЗАКРЫТИЕ токен-счёта — тоже транзакция и тоже стоит fee_tx (нашла Орест: раньше
    считался tx, а комиссия за него не вычиталась);
  * аренда ATA: платится при покупке, возвращается при закрытии счёта, но транзакция стоит;
  * доверительный интервал считается бутстрэпом, а не «среднее и всё».

Чего модель НЕ умеет (и не делает вид): сэндвич/MEV, провал транзакции, конкуренция за блок,
влияние нашего объёма на кривую сверх фиксированных скидки/премии, дискретность опроса
между точками траектории (стоп может сработать внутри интервала, а бумага его не увидит).
"""
from __future__ import annotations

import random

FEE_PUMP = 0.01        # 1% платформы с каждой стороны (feeBasisPoints=100 в IDL)
FEE_TX_SOL = 0.0001    # базовая комиссия + приоритет, примерно (в горячий час бывает 0.001)
ATA_RENT = 0.00204     # рента за токен-счёт, возвращается при закрытии
ENTRY_PREMIUM = 0.0    # покупка двигает кривую вверх; 0 = замороженный вариант §4

# ПРЕ-РЕГИСТРИРОВАННАЯ лестница (§4). Пороги НЕ подбираются по данным — это условие решения.
RUNGS = ((1.5, 0.50), (2.0, 0.25), (3.0, 0.25))
STOP = 0.7
TIMEOUT_S = 1800.0
EXIT_DISCOUNT = 0.02


# ---------------------------------------------------------------- траектория
def _pt(item):
    """Точка траектории: [t, price] или [t, price, real_sol] (формат с 03.11)."""
    t = item[0]
    p = item[1]
    s = item[2] if len(item) > 2 else None
    return t, p, s


def price_at(path, t):
    """Цена через t секунд после создания (первая точка не раньше t)."""
    if not path:
        return None
    for item in path:
        pt = item[0]
        if pt >= t:
            return item[1]
    return path[-1][1]


def sol_at(path, t):
    """Сколько SOL реально куплено на момент t (для честной когорты «дев-бай»).

    Раньше в записи лежал real_sol ПОСЛЕДНЕГО опроса окна, и признак был
    скоррелирован с исходом: улетевшая монета всегда попадала в «>1 SOL».
    """
    if not path:
        return None
    for item in path:
        pt, _p, s = _pt(item)
        if pt >= t and s is not None:
            return s
    for item in reversed(path):
        _t, _p, s = _pt(item)
        if s is not None:
            return s
    return None


def path_after(path, delay):
    """Точки, которые видит вход: всё, что не раньше задержки входа."""
    return [it for it in path if it[0] >= delay]


def peak_after_entry(path, delay):
    """Пик ПОСЛЕ входа (x). Было: max по всей траектории — вспышка в первую
    секунду засчитывалась как «дошла до x2», хотя войти в неё нельзя."""
    pts = path_after(path, delay)
    entry = price_at(path, delay)
    if not pts or not entry:
        return None
    return max(item[1] for item in pts) / entry


# ---------------------------------------------------------------- модели
def simulate_ladder(path, entry_delay=1.5, stake=0.055, rungs=RUNGS, stop=STOP,
                    timeout=TIMEOUT_S, discount=EXIT_DISCOUNT, fee_pump=FEE_PUMP,
                    fee_tx=FEE_TX_SOL, ata_rent=ATA_RENT, ata_close=True,
                    entry_premium=ENTRY_PREMIUM):
    """Лестница §4 — ЕДИНСТВЕННАЯ стратегия, по которой принимается решение.

    Возвращает dict или None (нет цены входа).
    """
    entry = price_at(path, entry_delay)
    if not entry:
        return None
    entry = entry * (1.0 + entry_premium)      # вошли дороже: покупка двигает кривую
    t_end = entry_delay + timeout
    pts = [(it[0], it[1]) for it in path if entry_delay <= it[0] <= t_end]

    left = 1.0
    gross = 0.0
    fees = 0.0                               # только продажи; покупка считается ниже
    txs = 1
    exits = []
    stopped = False
    ladder = list(rungs)

    for t, price in pts:
        if left <= 1e-9:
            break
        x = price / entry
        if x <= stop:
            fill = min(stop, x) * (1.0 - discount)     # гэп вниз: берём наблюдённую
            gross += stake * left * fill
            fees += stake * left * fill * fee_pump + fee_tx
            txs += 1
            exits.append({"t": round(t, 1), "kind": "stop", "x": round(fill, 4),
                          "frac": round(left, 3)})
            left = 0.0
            stopped = True
            break
        while ladder and x >= ladder[0][0] and left > 1e-9:
            level, frac = ladder.pop(0)
            f = min(frac, left)
            fill = level * (1.0 - discount)            # гэп вверх не улучшаем
            gross += stake * f * fill
            fees += stake * f * fill * fee_pump + fee_tx
            txs += 1
            exits.append({"t": round(t, 1), "kind": "tp", "x": round(fill, 4),
                          "frac": round(f, 3), "level": level})
            left -= f

    timed_out = left > 1e-9
    if timed_out:
        last = pts[-1][1] if pts else entry
        fill = (last / entry) * (1.0 - discount)
        gross += stake * left * fill
        fees += stake * left * fill * fee_pump + fee_tx
        txs += 1
        exits.append({"t": round(pts[-1][0], 1) if pts else round(entry_delay, 1),
                      "kind": "timeout", "x": round(fill, 4), "frac": round(left, 3)})
        left = 0.0

    rent = 0.0
    if ata_close:
        txs += 1
        fees += fee_tx                             # закрытие счёта — своя транзакция
    else:
        rent = -ata_rent                           # счёт брошен — рента потеряна

    # покупка: ставка + 1% платформы + чаевые. Продажи: свои 1% и чаевые (в fees).
    # Раньше комиссия покупки вычиталась дважды — поймал тест 03.11, а не глаз.
    net = gross - stake - (stake * fee_pump + fee_tx) - fees + rent
    return {
        "entry": entry,
        "exits": exits,
        "stopped": stopped,
        "timed_out": timed_out,
        "txs": txs,
        "gross_sol": round(gross, 6),
        "net_sol": round(net, 6),
        "net_pct": round(net / stake * 100.0, 3),
    }


def simulate_all_in(path, entry_delay=1.5, tp=1.5, sl=0.7, stake=0.055,
                    timeout=TIMEOUT_S, discount=EXIT_DISCOUNT, fee_pump=FEE_PUMP,
                    fee_tx=FEE_TX_SOL, entry_premium=ENTRY_PREMIUM):
    """All-in сетка — ТОЛЬКО разведка («а что если»). Решение по ней не принимается:
    максимум из 12-25 ячеек на одной выборке — подгонка (data snooping)."""
    entry = price_at(path, entry_delay)
    if not entry:
        return None
    entry = entry * (1.0 + entry_premium)
    t_end = entry_delay + timeout
    out_x = None
    kind = "timeout"
    for it in path:
        t, price = it[0], it[1]
        if t < entry_delay or t > t_end:
            continue
        x = price / entry
        if x >= tp:
            out_x, kind = tp, "tp"
            break
        if x <= sl:
            out_x, kind = min(sl, x), "sl"          # стоп по гэпу — наблюдённая цена
            break
    if out_x is None:
        last = price_at(path, t_end)
        out_x = (last or entry) / entry
    gross = stake * out_x * (1.0 - discount)
    fees = stake * fee_pump + fee_tx + stake * out_x * (1.0 - discount) * fee_pump + fee_tx
    net = gross - stake - fees
    return {"entry": entry, "out_x": round(out_x, 5), "kind": kind,
            "net_sol": round(net, 6), "net_pct": round(net / stake * 100.0, 3)}


def ladder_series(records, entry_delay=1.5, stake=0.055, **kw):
    """Прогнать лестницу по всем записям. Возвращает (список, сводка)."""
    rows = []
    for r in records:
        sim = simulate_ladder(r.get("path") or [], entry_delay, stake, **kw)
        if sim:
            sim["mint"] = r.get("mint")
            sim["at"] = r.get("at")
            rows.append(sim)
    return rows, summarize_rows(rows, stake)


def bootstrap_ci(rows, stake=0.055, iters=2000, seed=20261103):
    """95% доверительный интервал «процента на сделку», бутстрэпом.

    Зачем: среднее на выборке из 66 монет легко сдвигается одной удачной сделкой.
    Орест показал CI [−17.1, −4.6] на наших данных — целиком ниже нуля, то есть
    «нет преимущества» не шум. Без интервала такое утверждение делать нельзя.
    Генератор с фиксированным зерном: один и тот же файл даёт один и тот же ответ.
    """
    vals = [r.get("net_sol", 0.0) for r in rows]
    n = len(vals)
    if n < 5:
        return None
    rnd = random.Random(seed)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += vals[rnd.randrange(n)]
        means.append(s / n / stake * 100.0)
    means.sort()
    lo = means[max(0, int(round(0.025 * (iters - 1))))]
    hi = means[min(iters - 1, int(round(0.975 * (iters - 1))))]
    return {"lo": round(lo, 3), "hi": round(hi, 3), "iters": iters}


def summarize_rows(rows, stake=0.055):
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = sum(r["net_sol"] for r in rows)
    wins = sum(1 for r in rows if r["net_sol"] > 0)
    return {
        "trades": n,
        "wins": wins,
        "winrate": round(wins / n, 4),
        "pnl_sol": round(pnl, 6),
        "per_trade_sol": round(pnl / n, 6),
        "per_trade_pct": round(pnl / n / stake * 100.0, 3),
        "ci95": bootstrap_ci(rows, stake),
        "stops": sum(1 for r in rows if r["stopped"]),
        "timeouts": sum(1 for r in rows if r["timed_out"]),
        "avg_txs": round(sum(r["txs"] for r in rows) / n, 2),
    }
