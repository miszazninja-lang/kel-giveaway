"""Пересчитать итог честного розыгрыша самому.

    python verify.py https://kel-bot.onrender.com/give/<id>
    python verify.py giveaway.json          (сохранённый файл <id>.json)

Нужен только Python 3.8+, без библиотек. Случайное число берётся у drand напрямую, не у бота.
"""

from __future__ import annotations

import json
import sys

import give_core as core


def load(src: str) -> dict:
    if src.startswith(("http://", "https://")):
        url = src.split("?")[0].rstrip("/")
        return core._get_json(url if url.endswith(".json") else url + ".json")
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def main(src: str) -> int:
    d = load(src)
    print(f"Розыгрыш {d['id']}: {d['prize']}  (участников {d['n']}, победителей {d['k']})")
    if d.get("state") != "done":
        print(f"Итога ещё нет, сейчас: {d.get('state')}. Проверь после выхода раунда drand №{d['round']}.")
        return 2
    b = core.fetch_beacon(d["round"])
    if not b:
        print(f"drand не ответил про раунд №{d['round']} — попробуй позже.")
        return 1
    v = core.verify(d["id"], d["list"], b, d["digest"])
    theirs = [w[0] for w in d["winners"]]
    mine = v["order"][:len(theirs)]
    checks = [
        (v["digest_ok"], f"отпечаток списка совпадает: {v['digest']}"),
        (bool(d.get("close_msg_date")) and d["close_msg_date"] < d["round_time"],
         "список зафиксирован раньше, чем вышло число drand"),
        (b["randomness"] == d["randomness"], f"число drand №{d['round']} совпадает: {b['randomness']}"),
        (v["seed"] == d["seed"], f"затравка совпадает: {v['seed']}"),
        (mine == theirs,
         "победители по формуле: " + (", ".join(f"№{n}" for n in mine) or "нет")),
    ]
    for ok, text in checks:
        print(("✅ " if ok else "❌ ") + text)
    good = all(ok for ok, _t in checks)
    print("Итог честный." if good else "Есть расхождение — смотри строки с ❌.")
    return 0 if good else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
