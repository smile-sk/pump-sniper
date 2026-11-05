"""sniper_lib — всё, что нужно знать про pump.fun на уровне блокчейна.

Содержит то, что мы выяснили замерами 03.11.2026, а не по документации:
  * создание токена ловится по ДИСКРИМИНАТОРУ события, а не по тексту «Instruction: Create»
    (текст даёт ложные срабатывания чужих программ: ATA, Jupiter);
  * адрес bonding curve = PDA по правилу Solana (только распаковываемость точки),
    а не по правилу libsodium (оно отбрасывает ~43% валидных адресов);
  * цена читается из аккаунта кривой при commitment=processed, иначе свежий аккаунт
    ещё не виден и кажется, что токена нет.

Зависимостей нет, кроме стандартной библиотеки: чистый python 3.8+.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import sys

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com"

CREATE_EVENT_DISC = "1b72a94ddeeb6376"
# дискриминатор АККАУНТА BondingCurve = sha256("account:BondingCurve")[:8]
# сверено с официальным IDL pump.fun (Орест, 03.11). НЕ путать с 23b872048f42e4f1
CURVE_ACCOUNT_DISC = "17b7f83760d8ac60"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    """base58 -> 32 байта. Паддинг считается ОДИН раз.

    Было: n.to_bytes(32) уже содержал ведущие нули, и сверху добавлялись ещё pad
    нулей — адрес с ведущим 0x00 (≈1/256 монет) давал 33 байта, PDA считался
    неверно и монета молча уходила в no_curve (нашла Орест 03.11).
    """
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    pad = len(text) - len(text.lstrip("1"))
    body = 32 - pad
    if body <= 0:
        return b"\x00" * 32
    if n == 0:
        return b"\x00" * 32
    raw = n.to_bytes(body, "big") if body else b""
    return b"\x00" * pad + raw


def crypto_core_ed25519_is_valid_point(point: bytes) -> bool:
    """Правило Solana: точка распаковывается. НЕ правило libsodium (оно строже)."""
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = (y * y) % _P
    x2 = (y2 - 1) * pow((_D * y2 + 1) % _P, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def find_pda(seeds, program: bytes) -> bytes:
    for bump in range(255, -1, -1):
        payload = b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress"
        digest = hashlib.sha256(payload).digest()
        if not crypto_core_ed25519_is_valid_point(digest):
            return digest
    raise RuntimeError("PDA не найден")


def has_create_event(logs) -> bool:
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except Exception:
            continue
        if len(raw) >= 8 and raw[:8].hex() == CREATE_EVENT_DISC:
            return True
    return False


def parse_create_event(logs):
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except Exception:
            continue
        if len(raw) < 8 or raw[:8].hex() != CREATE_EVENT_DISC:
            continue
        pos = 8
        fields = {}
        ok = True
        for key in ("name", "symbol", "uri"):
            if pos + 4 > len(raw):
                ok = False
                break
            (ln,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            if ln > 400 or pos + ln > len(raw):
                ok = False
                break
            fields[key] = raw[pos:pos + ln].decode("utf-8", "replace")
            pos += ln
        if not ok or pos + 32 > len(raw):
            continue
        fields["mint"] = b58encode(raw[pos:pos + 32])
        return fields
    return None


def parse_curve(blob):
    """Состояние bonding curve: виртуальные и реальные резервы, цена, процент кривой."""
    if not blob or len(blob) < 49:
        return None
    if blob[:8].hex() != CURVE_ACCOUNT_DISC:
        # чужой аккаунт на том же адресе не должен становиться «ценой»
        return None
    v_tok, v_sol, r_tok, r_sol, supply = struct.unpack_from("<QQQQQ", blob, 8)
    complete = bool(blob[48])
    if v_tok == 0:
        return None
    price_sol = (v_sol / 1e9) / (v_tok / 1e6)
    return {
        "price_sol": price_sol,
        "market_cap_sol": price_sol * (supply / 1e6),
        "real_sol": r_sol / 1e9,
        "real_tok": r_tok / 1e6,
        "complete": complete,
        "curve_pct": round(r_sol / 85e9 * 100, 3),
    }


def curve_for_mint(mint: str) -> str:
    return b58encode(find_pda([b"bonding-curve", b58decode(mint)], b58decode(PUMP_PROGRAM)))


if __name__ == "__main__":
    # самопроверка без сети: считаем адрес кривой для известного mint
    if len(sys.argv) > 1:
        print(curve_for_mint(sys.argv[1]))
