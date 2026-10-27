"""Честный розыгрыш — ядро: как выбираются победители и как это проверить (26.10.2026).

Код открытый, без зависимостей (только стандартная библиотека Python), чтобы итог мог пересчитать кто угодно.

Почему нельзя подкрутить:
1. Пока идёт приём, никто не знает, кто выиграет: победителей решает случайное число drand
   (https://drand.love, сеть «quicknet», новое число каждые 3 секунды). Его выпускает League of Entropy —
   независимые организации (Cloudflare, Protocol Labs, университеты), и заранее его не знает никто.
2. Номер раунда drand объявляется в посте СРАЗУ при запуске. Этот раунд выходит через 2 минуты
   после конца приёма.
3. В конце приёма бот публикует отпечаток SHA-256 списка участников. Это происходит ДО выхода раунда,
   поэтому список нельзя подогнать под случайное число: добавить или убрать участника потом нельзя,
   иначе отпечаток не сойдётся.
4. Победители считаются из списка и случайного числа по формуле ниже. Пересчитать её может любой:
   страница проверки в браузере (сама берёт число у drand), `python verify.py` из открытого репозитория
   или вручную.
5. Условие «подписка» проверяется у всех при закрытии приёма, ДО отпечатка: не выполнившие в список
   не попадают (они публикуются отдельно). После выхода числа drand никого не перепроверяют —
   организатор не может выгнать неугодного победителя.

Формула (схема kel-give-v1):
    список    = строки «<номер> <код>\\n» по возрастанию номера (номер билета и его случайный код)
    отпечаток = SHA256(список)
    затравка  = SHA256("kel-give-v1|<id розыгрыша>|<отпечаток>|<randomness раунда drand>")
    шаг i (0, 1, 2…): x = SHA256("<затравка>|<i>") как число; из оставшихся билетов (по номеру)
                      берём билет с индексом x mod (сколько осталось) и вычёркиваем его.
Первые по порядку шагов — победители (в списке только те, кто выполнил условие на момент закрытия).
"""

from __future__ import annotations

import hashlib
import json
import urllib.request

SCHEME = "kel-give-v1"
# drand quicknet: https://api.drand.sh/<chain>/info — genesis 1692803367, период 3 с, схема bls-unchained-g1-rfc9380
DRAND_CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
DRAND_GENESIS = 1692803367
DRAND_PERIOD = 3
DRAND_RELAYS = ("https://api.drand.sh", "https://api2.drand.sh", "https://api3.drand.sh",
                "https://drand.cloudflare.com")
ROUND_DELAY = 120           # раунд — через столько секунд после конца приёма


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def round_time(r: int) -> int:
    """Когда выходит раунд r (unix-секунды)."""
    return DRAND_GENESIS + (int(r) - 1) * DRAND_PERIOD


def round_at(ts: float) -> int:
    """Первый раунд, который выходит не раньше ts."""
    r = int((ts - DRAND_GENESIS) // DRAND_PERIOD) + 1
    while round_time(r) < ts:
        r += 1
    return max(1, r)


def list_text(entries) -> str:
    """Список участников для отпечатка: (номер, код) → «номер код\\n» по возрастанию номера."""
    return "".join(f"{int(n)} {code}\n" for n, code in sorted((int(n), str(c)) for n, c in entries))


def parse_list(text: str) -> list:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            n, code = line.split()
            out.append((int(n), code))
    return out


def beacon_ok(b: dict, r: int) -> bool:
    """Число drand выглядит честным: тот раунд, и randomness = SHA256(подписи) (так у quicknet).
    Саму BLS-подпись можно сверить на drand.love или любой их программой — здесь не проверяем."""
    try:
        sig = bytes.fromhex(b["signature"])
        return int(b["round"]) == int(r) and hashlib.sha256(sig).hexdigest() == str(b["randomness"]).lower()
    except Exception:  # noqa: BLE001
        return False


def _get_json(url: str, timeout: float = 10) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kel-give/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 — адреса свои, https
        return json.loads(r.read().decode("utf-8"))


def fetch_beacon(r: int, get_json=None):
    """Число раунда r с любого из зеркал drand; None — раунд ещё не вышел или сеть недоступна."""
    get_json = get_json or _get_json
    for base in DRAND_RELAYS:
        try:
            b = get_json(f"{base}/{DRAND_CHAIN}/public/{int(r)}")
        except Exception:  # noqa: BLE001 — следующее зеркало
            continue
        if isinstance(b, dict) and beacon_ok(b, r):
            return {"round": int(b["round"]), "randomness": str(b["randomness"]).lower(),
                    "signature": str(b["signature"]).lower()}
    return None


def seed_of(gid: str, digest: str, randomness: str) -> str:
    return sha256(f"{SCHEME}|{gid}|{digest}|{randomness}")


def iter_draw(seed: str, numbers):
    """Билеты по порядку вытягивания: шаг i берёт индекс SHA256(«затравка|i») mod (сколько осталось)."""
    left = sorted(int(n) for n in numbers)
    i = 0
    while left:
        x = int(sha256(f"{seed}|{i}"), 16)
        yield left.pop(x % len(left))
        i += 1


def draw_order(seed: str, numbers, limit=None) -> list:
    out = []
    for n in iter_draw(seed, numbers):
        if limit is not None and len(out) >= limit:
            break
        out.append(n)
    return out


def verify(gid: str, text: str, beacon: dict, digest: str = "") -> dict:
    """Пересчёт снаружи: список + число drand → отпечаток, затравка и порядок всех билетов."""
    d = sha256(text)
    seed = seed_of(gid, d, beacon["randomness"])
    return {"digest": d, "digest_ok": (not digest) or d == digest, "seed": seed,
            "order": draw_order(seed, [n for n, _c in parse_list(text)])}
