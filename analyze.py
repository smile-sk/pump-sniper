"""analyze — разбор накопленного paper.jsonl: есть ли у отбора признаки.

Считает по фактам, а не по ощущениям. Выход — ОДНОЙ моделью (strategy.py), той же,
что в приборе: раньше здесь жила своя копия, и она разошлась с прибором.

Что важно (правки по ревью Ореста 03.11):
  * пик считается ПОСЛЕ входа, а не по всей траектории;
  * когорта «сколько дев купил» берётся на момент входа (sol_entry), а не на конец окна;
  * решение — по пре-регистрированной лестнице §4; сетка all-in только разведка;
  * когорты проверяются тестом Манна—Уитни и на отложенной половине данных;
  * для редкого хвоста (x5+) считается верхняя граница 95% ДИ, а не «ноль значит ноль»;
  * к среднему прикладывается бутстрэп-интервал: без него «минус» не доказан;
  * печатается чувствительность к чаевым и премии входа — единственным допущениям,
    которые бумага не измеряет и которые играют против нас.

    python analyze.py
    python analyze.py --file data/paper.jsonl --buy 0.055
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import strategy

ROOT = Path(__file__).resolve().parent
DELAYS = (0.5, 1.5, 3.0, 5.0)
# ПРЕ-РЕГИСТРИРОВАННЫЕ границы когорт (§5). Не подбираются по данным после просмотра.
COHORTS = (("0", 0.0, 0.05), ("0.05-0.3", 0.05, 0.3), ("0.3-1", 0.3, 1.0), (">1", 1.0, 1e9))


def load(path):
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return [r for r in rows if r.get("path")]


# ---------------------------------------------------------------- статистика
def _ranks(values):
    """Ранги с усреднением при связках + поправка на связки для сигмы."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    tie_term = 0.0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    return ranks, tie_term


def mann_whitney(x, y):
    """U Манна—Уитни, двусторонний p по нормальному приближению с поправкой на связки."""
    n1, n2 = len(x), len(y)
    if n1 < 3 or n2 < 3:
        return None
    values = list(x) + list(y)
    ranks, tie_term = _ranks(values)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    mu = n1 * n2 / 2.0
    var = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))
    if var <= 0:
        return {"u": u1, "p": 1.0, "n1": n1, "n2": n2}
    z = (u1 - mu) / math.sqrt(var)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return {"u": round(u1, 2), "p": round(min(1.0, p), 4), "n1": n1, "n2": n2}


def tail_ci(k, n):
    """95% верхняя граница доли редкого события (правило трёх при k=0)."""
    if n == 0:
        return None
    if k == 0:
        return round(3.0 / n * 100, 2)
    p = k / n
    se = math.sqrt(max(p * (1 - p) / n, 1e-12))
    return round(min(1.0, p + 1.96 * se) * 100, 2)


def cohort_of(sol):
    for name, lo, hi in COHORTS:
        if lo <= (sol or 0.0) < hi:
            return name
    return ">1"


# ---------------------------------------------------------------- отчёты
def summarize(rows, buy=0.055, entry_delay=1.5, track=1800.0,
              discount=strategy.EXIT_DISCOUNT, fee_tx=strategy.FEE_TX_SOL,
              entry_premium=strategy.ENTRY_PREMIUM):
    n = len(rows)
    print("токенов с траекторией: %d" % n)
    if not n:
        return

    def _ladder(part, **kw):
        # значения по умолчанию кладём в словарь, а не в вызов: иначе вызов с fee_tx=...
        # даёт «got multiple values for keyword argument» (поймала своим же прогоном)
        opts = {"timeout": track, "discount": discount, "fee_tx": fee_tx,
                "entry_premium": entry_premium}
        opts.update(kw)
        return strategy.ladder_series(part, entry_delay, buy, **opts)

    durs = [r["path"][-1][0] for r in rows]
    print("наблюдали (с): медиана %.0f, минимум %.0f, максимум %.0f"
          % (statistics.median(durs), min(durs), max(durs)))
    grad = [r for r in rows if r.get("graduate_at")]
    print("выпустились на DEX: %d (%.0f%%)%s"
          % (len(grad), len(grad) / n * 100,
             ("  через (с): медиана %.0f" % statistics.median([r["graduate_at"] for r in grad]))
             if grad else ""))

    print("\nпик ПОСЛЕ входа (не по всей траектории):")
    for delay in DELAYS:
        xs = [p for p in (strategy.peak_after_entry(r["path"], delay) for r in rows) if p]
        if not xs:
            continue
        xs.sort()
        line = "  %.1f с: медиана x%.2f" % (delay, statistics.median(xs))
        for lvl in (1.1, 1.2, 1.5, 2.0, 5.0):
            line += "  ≥x%.1f %2.0f%%" % (lvl, sum(1 for x in xs if x >= lvl) / len(xs) * 100)
        print(line)

    # ---- РЕШЕНИЕ
    _rows, summary = _ladder(rows)
    print("\nЛЕСТНИЦА §4 — единственный критерий решения (скидка %.1f%%, премия входа %.1f%%,"
          " чаевые %.4f SOL/tx, аренда ATA, все tx):"
          % (discount * 100, entry_premium * 100, fee_tx))
    if summary.get("trades"):
        print("  сделок %d, вплюс %d (%.1f%%), стопов %d, таймаутов %d, tx/сделка %.1f"
              % (summary["trades"], summary["wins"], summary["winrate"] * 100,
                 summary["stops"], summary["timeouts"], summary["avg_txs"]))
        print("  PnL %.6f SOL, на сделку %.6f SOL (%.2f%% ставки)"
              % (summary["pnl_sol"], summary["per_trade_sol"], summary["per_trade_pct"]))
        ci = summary.get("ci95")
        if ci:
            print("  95%% ДИ (бутстрэп, %d итераций): от %+.2f%% до %+.2f%% на сделку%s"
                  % (ci["iters"], ci["lo"], ci["hi"],
                     "  — ЦЕЛИКОМ ниже нуля, «нет преимущества» не шум"
                     if ci["hi"] < 0 else ("  — ЦЕЛИКОМ выше нуля" if ci["lo"] > 0
                                           else "  — интервал пересекает ноль, вывод не доказан")))
        if summary["per_trade_pct"] >= 8:
            print("  ВЕРДИКТ §7: плюс ≥ +8% — можно живьём")
        elif summary["per_trade_pct"] > -8:
            print("  ВЕРДИКТ §7: около нуля = преимущества нет")
        else:
            print("  ВЕРДИКТ §7: минус — живые деньги не заводим")

    # ---- чувствительность
    print("\nчувствительность (бумага этих величин не знает; все они играют против нас):")
    for ft in (0.0001, 0.0005, 0.001):
        _r, ss = _ladder(rows, fee_tx=ft)
        if ss.get("trades"):
            print("  чаевые %.4f SOL/tx → %+.2f%% на сделку" % (ft, ss["per_trade_pct"]))
    for ep in (0.0, 0.01, 0.02):
        _r, ss = _ladder(rows, entry_premium=ep)
        if ss.get("trades"):
            print("  премия входа %.0f%% → %+.2f%% на сделку" % (ep * 100, ss["per_trade_pct"]))

    # ---- хвост
    print("\nхвост (главное, чем живёт лестница):")
    xs = [p for p in (strategy.peak_after_entry(r["path"], entry_delay) for r in rows) if p]
    for lvl in (3.0, 5.0, 10.0):
        k = sum(1 for x in xs if x >= lvl)
        print("  ≥x%.0f: %d из %d (%.1f%%), верхняя граница 95%% ДИ — %.1f%%"
              % (lvl, k, len(xs), k / len(xs) * 100, tail_ci(k, len(xs))))

    # ---- когорты
    print("\nкогорты по SOL, купленному ДО входа (границы зафиксированы заранее):")
    by_cohort = {}
    # только записи НОВОГО формата: у старых sol_first — это резерв на КОНЕЦ окна,
    # то есть величина, скоррелированная с исходом (ревью Ореста, находка 4).
    # Подмешать их в когорты значит вернуть тот самый артефакт через чёрный ход.
    fresh = [r for r in rows if "sol_entry" in r]
    stale = n - len(fresh)
    if stale:
        print("  (в когорты не вошли %d старых записей: у них нет sol_entry)" % stale)
    for r in fresh:
        by_cohort.setdefault(cohort_of(r.get("sol_entry") or 0.0), []).append(r)
    for name, group in by_cohort.items():
        _g, gs = _ladder(group)
        pks = sorted(p for p in (strategy.peak_after_entry(r["path"], entry_delay) for r in group) if p)
        if not pks:
            continue
        print("  %-9s токенов %4d, медиана x%.2f, ≥x1.5 %2.0f%%, лестница %+.2f%% на сделку"
              % (name, len(group), statistics.median(pks),
                 sum(1 for v in pks if v >= 1.5) / len(pks) * 100,
                 gs.get("per_trade_pct", 0.0)))

    # ---- значимость и устойчивость
    base = [p for p in (strategy.peak_after_entry(r["path"], entry_delay) for r in by_cohort.get("0", []) if r) if p]
    print("\nзначимость и устойчивость (без этого «сигнал» = случайность):")
    for name, group in by_cohort.items():
        if name == "0":
            continue
        other = [p for p in (strategy.peak_after_entry(r["path"], entry_delay) for r in group) if p]
        mw = mann_whitney(base, other) if base and other else None
        if mw:
            print("  %-9s против когорты 0: U=%.1f, p=%.4f %s"
                  % (name, mw["u"], mw["p"], "— различимо" if mw["p"] < 0.05 else "— НЕ различимо"))
    half = len(rows) // 2
    for tag, part in (("первая половина", rows[:half]), ("вторая половина", rows[half:])):
        _p, ps = _ladder(part)
        print("  устойчивость, %-15s: сделок %d, на сделку %+.2f%%"
              % (tag, ps.get("trades", 0), ps.get("per_trade_pct", 0.0)))

    print("\nсостав выборки (смещения, а не сделки):")
    print("  записей %d; токенов без кривой за 8 с — см. поле no_curve в summary.jsonl"
          " (их нельзя купить, но в знаменателе «услышали» они остаются)" % n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "data" / "paper.jsonl"))
    ap.add_argument("--buy", type=float, default=0.055)
    ap.add_argument("--entry-delay", type=float, default=1.5)
    ap.add_argument("--track", type=float, default=strategy.TIMEOUT_S)
    ap.add_argument("--discount", type=float, default=strategy.EXIT_DISCOUNT)
    ap.add_argument("--entry-premium", type=float, default=strategy.ENTRY_PREMIUM)
    ap.add_argument("--fee-tx", type=float, default=strategy.FEE_TX_SOL)
    a = ap.parse_args()
    p = Path(a.file)
    if not p.exists():
        print("нет файла %s — сначала соберите прогон (deploy.py collect)" % p)
        return 1
    summarize(load(p), a.buy, a.entry_delay, a.track, a.discount, a.fee_tx, a.entry_premium)
    return 0


if __name__ == "__main__":
    sys.exit(main())
