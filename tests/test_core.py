"""Проверка формулы на известных ответах: если кто-то поменяет алгоритм, тесты упадут.
Запуск: python -m pytest tests  (или просто python tests/test_core.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import give_core as core  # noqa: E402

# настоящий раунд drand quicknet №1000: https://api.drand.sh/52db…e971/public/1000
DRAND_1000 = {"round": 1000,
              "randomness": "fe290beca10872ef2fb164d2aa4442de4566183ec51c56ff3cd603d930e54fdd",
              "signature": "b44679b9a59af2ec876b1a6b1ad52ea9b1615fc3982b19576350f93447cb1125e342b73a8dd2bacbe47e4b6b63ed5e39"}


def test_real_drand_round_passes_and_fake_fails():
    assert core.beacon_ok(DRAND_1000, 1000)
    assert not core.beacon_ok(dict(DRAND_1000, randomness="00" * 32), 1000)
    assert not core.beacon_ok(DRAND_1000, 1001)


def test_round_clock():
    assert core.round_time(1) == core.DRAND_GENESIS
    assert core.round_at(core.round_time(1000)) == 1000
    assert core.round_at(core.round_time(1000) + 1) == 1001


def test_known_answer():
    text = core.list_text((i, "c%03d" % i) for i in range(1, 58))
    v = core.verify("abc123", text, DRAND_1000)
    assert v["digest"] == "40b272318fb82083cb8dc0888a4b9b4f7f9728b1db5e8c73490f473d13ab0b8d"
    assert v["seed"] == "c6218632d5c0c7c7abdd618f5696d1f0335e5785e8367ec20012f76b397b2af0"
    assert v["order"][:10] == [41, 13, 57, 39, 12, 7, 17, 30, 49, 55]
    assert sorted(v["order"]) == list(range(1, 58))          # каждый билет ровно один раз


def test_list_order_does_not_matter():
    a = core.list_text([(2, "bb"), (1, "aa")])
    assert a == "1 aa\n2 bb\n" and core.parse_list(a) == [(1, "aa"), (2, "bb")]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
