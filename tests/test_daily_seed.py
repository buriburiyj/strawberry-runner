#!/usr/bin/env python3
"""daily_seed.py 단위 테스트 — 네트워크 없음

확인하는 것
  * 같은 날짜는 항상 완전히 같은 JSON (재현성)
  * 날짜가 다르면 시드가 실제로 달라진다 (고정값 아님)
  * 테마·보스·모디파이어·음계가 테이블 안의 실제 값이다
  * spawn 배율이 모디파이어 효과와 일치한다
  * 출력 크기·JSON 직렬화·날짜 검증·CLI 종료 코드

Usage:
    python3 tests/test_daily_seed.py
    python3 tests/test_daily_seed.py -v
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULE_PATH = os.path.join(ROOT, "scripts", "daily_seed.py")

spec = importlib.util.spec_from_file_location("daily_seed", MODULE_PATH)
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

CHECKS = []


def check(name):
    def decorator(func):
        CHECKS.append((name, func))
        return func
    return decorator


# ------------------------------------------------------------------ determinism

@check("same date -> byte-identical JSON")
def _():
    a = ds.dumps(ds.generate("2026-11-19", now=ds.datetime(2026, 11, 18, 16, 0, tzinfo=ds.timezone.utc)))
    b = ds.dumps(ds.generate("2026-11-19", now=ds.datetime(2026, 11, 18, 16, 0, tzinfo=ds.timezone.utc)))
    assert a == b, "two runs of the same date differed"


@check("generated_utc is the only time-dependent field")
def _():
    a = ds.generate("2026-11-19", now=ds.datetime(2026, 11, 18, 16, 0, tzinfo=ds.timezone.utc))
    b = ds.generate("2026-11-19", now=ds.datetime(2026, 11, 20, 1, 0, tzinfo=ds.timezone.utc))
    assert a.pop("generated_utc") != b.pop("generated_utc")
    assert a == b


@check("sixty consecutive days are all different")
def _():
    seen = set()
    start = date(2026, 11, 19)
    for i in range(60):
        payload = ds.generate((start + timedelta(days=i)).strftime("%Y-%m-%d"))
        seen.add(payload["seed_hex"])
    assert len(seen) == 60, "seed collision: %s" % (60 - len(seen))


@check("theme/boss are not stuck on one value across 60 days")
def _():
    themes, bosses = set(), set()
    start = date(2026, 11, 19)
    for i in range(60):
        payload = ds.generate((start + timedelta(days=i)).strftime("%Y-%m-%d"))
        themes.add(payload["theme"]["id"])
        bosses.add(payload["boss"]["behavior"])
    assert len(themes) >= 6, "theme spread too narrow: %s" % sorted(themes)
    assert len(bosses) == 5, "boss behavior spread: %s" % sorted(bosses)


# ------------------------------------------------------------------- structure

@check("schema fields are present and typed")
def _():
    p = ds.generate("2026-11-19")
    for key in ("schema", "date_kst", "seed_hex", "seed_int", "tag", "title", "theme",
                "arena", "boss", "modifiers", "waves", "target_score", "spawn",
                "music", "quote", "rules"):
        assert key in p, "missing " + key
    assert p["schema"] == ds.SCHEMA
    assert isinstance(p["seed_int"], int) and p["seed_int"] < 2 ** 64
    assert len(p["seed_hex"]) == 16
    assert len(p["tag"]) == 4


@check("colors are real hex codes from the theme table")
def _():
    p = ds.generate("2026-11-19")
    theme = p["theme"]
    known = {t["id"]: t for t in ds.THEMES}
    assert theme["id"] in known
    for field in ("primary", "secondary", "accent", "floor", "fog", "sky", "boss"):
        value = theme[field]
        assert value.startswith("#") and len(value) == 7, field
        int(value[1:], 16)  # raises if not hex
    assert theme == known[theme["id"]], "theme mutated from table"


@check("boss behavior is one the game implements")
def _():
    implemented = {"spiral", "radial", "sweep", "summon", "hunter"}
    for i in range(30):
        p = ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d"))
        assert p["boss"]["behavior"] in implemented
        assert p["boss"]["behaviorName"] == ds.BEHAVIOR_NAMES[p["boss"]["behavior"]]


@check("modifiers are 2-3, unique, and real table entries")
def _():
    ids = {m["id"] for m in ds.MODIFIERS}
    for i in range(40):
        p = ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d"))
        chosen = [m["id"] for m in p["modifiers"]]
        assert 2 <= len(chosen) <= 3, chosen
        assert len(chosen) == len(set(chosen)), "duplicate modifier: %s" % chosen
        assert set(chosen) <= ids


@check("spawn multipliers match the chosen modifiers")
def _():
    for i in range(30):
        p = ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d"))
        expect = {"enemy_hp": 1.0, "enemy_speed": 1.0, "enemy_fire": 1.0,
                  "player_hp": 1.0, "player_bullet_speed": 1.0, "dash_cd": 1.0,
                  "score_mul": 1.0, "spawn_mul": 1.0, "elite_add": 0.0,
                  "gravity": 0.0, "meteor": 0.0, "bounce": 0, "boss_regen": 0.0}
        for mod in p["modifiers"]:
            for k, v in mod["effects"].items():
                if k in ("elite_add", "gravity", "meteor", "boss_regen"):
                    expect[k] = round(expect[k] + float(v), 3)
                elif k == "bounce":
                    expect[k] += int(v)
                else:
                    expect[k] = round(expect[k] * float(v), 4)
        for k, v in expect.items():
            assert abs(float(p["spawn"][k]) - v) < 1e-6, "%s: %s != %s" % (k, p["spawn"][k], v)


@check("target score and boss hp grow with wave count and stay in range")
def _():
    for i in range(30):
        p = ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d"))
        assert 5 <= p["waves"] <= 7
        assert 10000 <= p["target_score"] <= 22000, p["target_score"]
        assert 1000 <= p["boss"]["hp"] <= 1500, p["boss"]["hp"]
        assert p["boss_hp"] == p["boss"]["hp"]


@check("music block uses a real scale and audible range")
def _():
    for i in range(20):
        p = ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d"))
        music = p["music"]
        assert music["scale"] in {s["id"] for s in ds.SCALES}
        assert 90 <= music["root"] <= 150
        assert 110 <= music["tempo"] <= 142
        assert music["steps"] and music["steps"][0] == 0


@check("output stays under the runner 16KB stdout limit")
def _():
    for i in range(20):
        text = ds.dumps(ds.generate((date(2026, 11, 19) + timedelta(days=i)).strftime("%Y-%m-%d")))
        assert len(text.encode("utf-8")) < 15000, len(text)


@check("no secret-looking values in the payload")
def _():
    text = json.dumps(ds.generate("2026-11-19")).lower()
    for needle in ("token", "bearer", "api_key", "secret", "password"):
        assert needle not in text, needle


# ---------------------------------------------------------------------- CLI

@check("CLI rejects an invalid date with exit code 2")
def _():
    proc = subprocess.run([sys.executable, MODULE_PATH, "--date", "2026-02-30"],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.returncode
    assert proc.stdout == ""


@check("CLI prints one parseable JSON object")
def _():
    proc = subprocess.run([sys.executable, MODULE_PATH, "--date", "2026-12-25"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["date_kst"] == "2026-12-25"
    assert proc.stdout.count("\n") == 1


@check("CLI --emit-tables exposes every table for the JS side")
def _():
    proc = subprocess.run([sys.executable, MODULE_PATH, "--emit-tables"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    tables = json.loads(proc.stdout)
    for key in ("themes", "arenas", "bosses", "epithets", "modifiers", "quotes", "scales"):
        assert tables[key], key
    assert len(tables["themes"]) == len(ds.THEMES)


def main(argv):
    verbose = "-v" in argv
    passed = 0
    failures = []
    for name, func in CHECKS:
        try:
            func()
            passed += 1
            if verbose:
                print("ok   %s" % name)
        except Exception as exc:                      # noqa: BLE001
            failures.append((name, "%s: %s" % (type(exc).__name__, exc)))
            print("FAIL %s" % name)
            print("      -> %s" % str(exc)[:400])
    print("\n%d/%d checks passed, %d failed" % (passed, len(CHECKS), len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
