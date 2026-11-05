"""test_strategy — проверки ЧИСЕЛ, а не «выглядит правильно».

Каждая проверка взята из живого ревью Ореста 03.11 или из замера на прогоне;
синтетические пути — его же контрпримеры.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import strategy  # noqa: E402

OK = 0
FAIL = 0


def check(name, got, want, tol=1e-9):
    global OK, FAIL
    good = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) \
        else (got == want)
    if good:
        OK += 1
        print("  ok   %-52s %s" % (name, got))
    else:
        FAIL += 1
        print("  FAIL %-52s получила %s, ждала %s" % (name, got, want))


print("1. Пик ПОСЛЕ входа (контрпример Ореста: вспышка до входа не считается)")
path = [[0.5, 3.0], [2.0, 1.1], [5.0, 1.0]]
check("пик после входа 1.5 с", round(strategy.peak_after_entry(path, 1.5), 4), 1.0)
check("пик по всей траектории (как было)", round(max(p for _t, p in path) / 1.1, 2), 2.73)

print("2. Стоп по гэпу (контрпример Ореста: модель завышала выход в 1.6 раза)")
path = [[0.0, 1.0], [0.9, 1.6], [3.9, 1.4], [6.9, 0.45]]
s = strategy.simulate_all_in(path, 1.5, tp=9.0, sl=0.7, discount=0.0)
check("all-in стоп берёт наблюдённую цену", round(s["out_x"], 4), round(0.45 / 1.4, 4), 1e-4)
check("а не ровно 0.7", s["out_x"] != 0.7, True)

print("3. Лестница §4: три ступени, всё продано, транзакции посчитаны")
path = [[1.0, 1.0], [2.0, 1.6], [3.0, 2.1], [4.0, 3.2], [5.0, 3.0]]
sim = strategy.simulate_ladder(path, 0.5, stake=0.055, discount=0.0, ata_close=True)
check("выходов ровно 3", len(sim["exits"]), 3)
check("доли 0.5/0.25/0.25", [e["frac"] for e in sim["exits"]], [0.5, 0.25, 0.25])
check("уровни выходов", [e["level"] for e in sim["exits"]], [1.5, 2.0, 3.0])
check("транзакций: 1 покупка + 3 продажи + закрытие", sim["txs"], 5)
check("стоп не срабатывал", sim["stopped"], False)
check("таймаута не было", sim["timed_out"], False)
gross = 0.055 * (0.5 * 1.5 + 0.25 * 2.0 + 0.25 * 3.0)
# 1% платформы и чаевые на КАЖДУЮ транзакцию: покупка, 3 продажи, закрытие счёта
fees = (0.055 * 0.01 + 0.0001) + gross * 0.01 + 4 * 0.0001
check("net SOL = руками посчитанный", round(sim["net_sol"], 6), round(gross - 0.055 - fees, 6), 1e-6)

print("4. Стоп закрывает остаток по наблюдённой цене")
path = [[1.0, 1.0], [2.0, 1.6], [3.0, 0.5], [4.0, 0.4]]
sim = strategy.simulate_ladder(path, 0.5, stake=0.055, discount=0.0)
check("сработал стоп", sim["stopped"], True)
check("продано 50% на 1.5 и остаток по 0.5", [e["kind"] for e in sim["exits"]], ["tp", "stop"])
check("цена стопа — наблюдённая 0.5", sim["exits"][-1]["x"], 0.5)

print("5. Таймаут закрывает по рынку со скидкой")
path = [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
sim = strategy.simulate_ladder(path, 0.5, stake=0.055, discount=0.02, timeout=10.0)
check("сработал таймаут", sim["timed_out"], True)
check("выход = 1.0 со скидкой 2%", sim["exits"][-1]["x"], 0.98)
check("на сделке минус (скидка + комиссии)", sim["net_pct"] < 0, True)

print("6. Скидка на выход уменьшает результат ровно на неё")
a = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.6]], 0.5, 0.055, discount=0.0)
b = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.6]], 0.5, 0.055, discount=0.03)
check("скидка 3% бьёт по обеим ступеням", round(a["exits"][0]["x"] - b["exits"][0]["x"], 4), 0.045, 1e-6)

print("7. Аренда ATA: брошенный счёт стоит 0.00204 SOL")
stay = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.0]], 0.5, 0.055, discount=0.0, ata_close=False)
close = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.0]], 0.5, 0.055, discount=0.0, ata_close=True)
# выигрыш закрытия = возвращённая рента минус чаевые за саму транзакцию закрытия
check("без закрытия хуже на ренту минус чаевые",
      round(close["net_sol"] - stay["net_sol"], 6), round(0.00204 - 0.0001, 6), 1e-6)

print("7b. Закрытие счёта — транзакция, и её чаевые тоже вычитаются (нашла Орест)")
stay2 = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.0]], 0.5, 0.055, discount=0.0,
                                 ata_close=False, fee_tx=0.001)
close2 = strategy.simulate_ladder([[1.0, 1.0], [2.0, 1.0]], 0.5, 0.055, discount=0.0,
                                  ata_close=True, fee_tx=0.001)
check("закрытие добавляет транзакцию", close2["txs"] - stay2["txs"], 1)
check("выигрыш закрытия = рента минус чаевые",
      round(close2["net_sol"] - stay2["net_sol"], 6), round(0.00204 - 0.001, 6), 1e-6)

print("8. Цена входа: старая запись из двух полей читается как раньше")
check("price_at по 2-полевой записи", strategy.price_at([[0.0, 2.0], [5.0, 4.0]], 3.0), 4.0)
check("sol_at берёт значение на момент входа",
      strategy.sol_at([[0.5, 1.0, 0.1], [2.0, 1.1, 0.4], [9.0, 3.0, 9.9]], 1.5), 0.4)
check("сол НЕ конечный (было сломано)", strategy.sol_at([[0.5, 1.0, 0.1], [2.0, 1.1, 0.4],
                                                          [9.0, 3.0, 9.9]], 1.5) != 9.9, True)

print("9. Защита от вырожденных данных")
check("пустой путь -> None", strategy.simulate_ladder([], 1.5), None)
check("без точки после входа -> None пик", strategy.peak_after_entry([[0.1, 1.0]], 1.5), None)

print("10. Премия на вход (покупка тоже двигает кривую, симметрично скидке)")
p = [[1.0, 1.0], [2.0, 1.6], [3.0, 2.1], [4.0, 3.2]]
no_prem = strategy.simulate_ladder(p, 0.5, 0.055, discount=0.0, entry_premium=0.0)
prem = strategy.simulate_ladder(p, 0.5, 0.055, discount=0.0, entry_premium=0.02)
check("премия 2% поднимает цену входа", round(prem["entry"], 4), 1.02, 1e-9)
check("по умолчанию премии нет (стратегия §4 не сдвинута)",
      strategy.simulate_ladder(p, 0.5, 0.055)["entry"], no_prem["entry"])
# на пути, который все ступени всё равно пробивает, премия не меняет кратности —
# она бьёт по ТОМУ, ДОСТАЁТ ЛИ цена до ступени. Контрпример: цена 1.52 от входа.
marg = [[1.0, 1.0], [2.0, 1.52]]
m_no = strategy.simulate_ladder(marg, 0.5, 0.055, discount=0.0, entry_premium=0.0)
m_yes = strategy.simulate_ladder(marg, 0.5, 0.055, discount=0.0, entry_premium=0.02)
check("без премии ступень 1.5 достигнута", m_no["exits"][0]["kind"] == "tp", True)
check("с премией 2% — уже нет", any(e["kind"] == "tp" for e in m_yes["exits"]), False)
check("и результат хуже", m_yes["net_sol"] < m_no["net_sol"], True)

print("11. Доверительный интервал бутстрэпом (без него «минус» не доказан)")
rows = [{"net_sol": -0.001} for _ in range(40)]
ci = strategy.bootstrap_ci(rows, stake=0.055)
check("на одинаковых сделках интервал схлопывается", ci["lo"] == ci["hi"], True)
check("и равен среднему в процентах", round(ci["lo"], 3), round(-0.001 / 0.055 * 100, 3), 1e-3)
mixed = [{"net_sol": -0.001}] * 30 + [{"net_sol": 0.004}] * 20
ci2 = strategy.bootstrap_ci(mixed, stake=0.055)
check("на разных интервал не нулевой", ci2["hi"] > ci2["lo"], True)
check("нижняя граница ниже верхней по построению", ci2["lo"] < ci2["hi"], True)
check("мало данных (<5) -> None", strategy.bootstrap_ci([{"net_sol": 0.0}] * 4), None)

print("12. Сводка несёт интервал, а не только среднее")
_r, summ = strategy.ladder_series([{"path": [[1.0, 1.0], [2.0, 1.0]]} for _ in range(10)], 0.5, 0.055)
check("в сводке есть ci95", isinstance(summ.get("ci95"), dict), True)
check("и он ниже нуля (плоская монета = убыток)", summ["ci95"]["hi"] < 0, True)

print("\nИтого: %d ok, %d FAIL" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
