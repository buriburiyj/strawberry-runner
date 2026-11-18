#!/usr/bin/env python3
"""daily_seed.py — NEON CORE '오늘의 시드' 생성기 (표준 라이브러리만)

무엇을 하나
-----------
날짜(기본: 한국 시간 오늘) 하나로 **결정적으로** 하루치 게임 시드를 만든다.
같은 날짜 → 언제 어디서 돌려도 완전히 같은 JSON. 다른 날짜 → 다른 테마·보스·모디파이어.

출력하는 것 (게임이 실제로 쓰는 값만)
  theme       네온 팔레트 6색 + 이름
  arena       아레나 반지름·격자·배경
  boss        코드네임·한글 별칭·행동 패턴(spiral/radial/sweep/summon/hunter)·체력·색
  modifiers   오늘의 모디파이어 2~3개 (각각 게임 수치 효과 포함)
  waves       잡몹 웨이브 수
  target_score 오늘의 목표 점수
  spawn       적 체력·속도·사격·엘리트 확률 배율 (모디파이어 반영)
  music       음계·근음·템포 (Web Audio 합성용)
  quote       오늘의 한 줄

어디에 쓰나
-----------
  * strawberry-runner Actions(python_script / shell_cmd) → stdout JSON
  * buriburiyj.github.io/neon-core/daily.json (공개 사본)
  * 브라우저는 같은 알고리즘을 JS로도 갖고 있어, 서버 시드가 없어도 로컬 계산으로 논다.

쓰는 법
-------
    python3 scripts/daily_seed.py                # 오늘(KST) 시드, stdout JSON 한 덩어리
    python3 scripts/daily_seed.py --pretty
    python3 scripts/daily_seed.py --date 2026-12-25
    python3 scripts/daily_seed.py --out seed.json
    python3 scripts/daily_seed.py --emit-tables  # JS용 테이블(단일 출처 확인용)

지키는 선
---------
  * 비밀값·토큰을 쓰지 않는다. 네트워크도 쓰지 않는다. 순수 함수.
  * 출력은 15KB를 넘지 않는다.
  * 존재하지 않는 날짜(2026-02-30)는 오류로 끝난다(종료 코드 2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

SCHEMA = 1
VERSION = "1.0.0"
KST = timezone(timedelta(hours=9))
SALT = "neon-core|1|"
MAX_OUTPUT_BYTES = 15 * 1024

# --------------------------------------------------------------------- tables
# 시드가 고르는 것들. 순서를 바꾸면 그날의 시드가 바뀐다(과거 시드 재현이 깨짐).
THEMES = [
    {"id": "ultraviolet", "name": "자외선 성운",
     "primary": "#8b5cff", "secondary": "#22e3ff", "accent": "#ff2e93",
     "floor": "#160a2e", "fog": "#08010f", "sky": "#04010a", "boss": "#ff2e93"},
    {"id": "acid-bloom", "name": "산성 개화",
     "primary": "#b8ff2f", "secondary": "#00ffb0", "accent": "#e03bff",
     "floor": "#101c05", "fog": "#040a02", "sky": "#020601", "boss": "#e03bff"},
    {"id": "deep-pulse", "name": "심해 맥동",
     "primary": "#22b8ff", "secondary": "#00ffd5", "accent": "#ff4d7a",
     "floor": "#04162a", "fog": "#010a14", "sky": "#00050b", "boss": "#ff4d7a"},
    {"id": "ember-rain", "name": "홍염의 비",
     "primary": "#ff5a2e", "secondary": "#ffc32e", "accent": "#ff2e6e",
     "floor": "#2a0c05", "fog": "#120401", "sky": "#0a0200", "boss": "#ff2e6e"},
    {"id": "chrome-dawn", "name": "크로뮴 여명",
     "primary": "#cfd8ff", "secondary": "#7a8cff", "accent": "#00ffe1",
     "floor": "#121420", "fog": "#07080f", "sky": "#040409", "boss": "#00ffe1"},
    {"id": "neon-park", "name": "네온 파크",
     "primary": "#ff2ea6", "secondary": "#2effd5", "accent": "#faff00",
     "floor": "#1e0722", "fog": "#0b0210", "sky": "#06010a", "boss": "#faff00"},
    {"id": "void-ice", "name": "공허의 얼음",
     "primary": "#9fe8ff", "secondary": "#5c7cff", "accent": "#e6f9ff",
     "floor": "#0a1822", "fog": "#030b12", "sky": "#01060a", "boss": "#e6f9ff"},
    {"id": "magenta-static", "name": "마젠타 노이즈",
     "primary": "#ff35d0", "secondary": "#38f0ff", "accent": "#fff04a",
     "floor": "#210725", "fog": "#0d0210", "sky": "#07010a", "boss": "#fff04a"},
]

ARENAS = [
    {"id": "ring", "name": "링 아레나", "radius": 27.0, "grid": 2.0, "cover": 0},
    {"id": "cross", "name": "십자 회랑", "radius": 25.0, "grid": 2.5, "cover": 4},
    {"id": "well", "name": "우물", "radius": 24.0, "grid": 1.8, "cover": 2},
    {"id": "plateau", "name": "고원", "radius": 29.0, "grid": 2.2, "cover": 3},
]

# behavior: 게임이 실제로 구현한 5가지 패턴
BOSSES = [
    {"codename": "HELIX", "behavior": "spiral", "desc": "회전하는 두 팔의 탄막. 중심에서 바깥으로 비틀린다."},
    {"codename": "NOVA", "behavior": "radial", "desc": "전 방향 방사. 한 번에 24발, 골이 뚫린다."},
    {"codename": "WARDEN", "behavior": "sweep", "desc": "수평 레이저를 예고선 1.2초 뒤에 긋는다."},
    {"codename": "HIVE", "behavior": "summon", "desc": "드론을 낳는다. 놓아두면 화면이 가득 찬다."},
    {"codename": "RAZOR", "behavior": "hunter", "desc": "플레이어를 직선으로 파고든다. 대시로 흘려야 한다."},
    {"codename": "PULSE", "behavior": "radial", "desc": "고리를 겹쳐 쏜다. 사이로 걸어 나가면 된다."},
    {"codename": "ECLIPSE", "behavior": "spiral", "desc": "시계·반시계를 번갈아 감는다. 리듬을 읽어라."},
    {"codename": "TWIN", "behavior": "summon", "desc": "분신을 남긴다. 본체의 색이 진하다."},
]

BEHAVIOR_NAMES = {
    "spiral": "나선 탄막", "radial": "방사 폭격", "sweep": "레이저 소인",
    "summon": "드론 소환", "hunter": "추적 돌진",
}

EPITHETS = [
    "속삭이는", "타오르는", "얼어붙은", "뒤틀린", "무한한", "조용한",
    "갈라진", "벙어리", "되감기는", "백열의", "잊힌", "새벽의",
]

MODIFIERS = [
    {"id": "meteor-shower", "name": "유성우", "desc": "약 2.6초마다 아레나에 유성이 떨어진다.",
     "effects": {"meteor": 1.0}},
    {"id": "overload", "name": "과부하", "desc": "적 이동 속도 +18%.",
     "effects": {"enemy_speed": 1.18}},
    {"id": "iron-skin", "name": "강철 껍질", "desc": "적 체력 +25%, 대신 점수 +10%.",
     "effects": {"enemy_hp": 1.25, "score_mul": 1.10}},
    {"id": "gravity-well", "name": "중력 우물", "desc": "아레나 중심이 초당 1.4만큼 끌어당긴다.",
     "effects": {"gravity": 1.4}},
    {"id": "dampener", "name": "감쇠 장", "desc": "대시 쿨다운 +35%.",
     "effects": {"dash_cd": 1.35}},
    {"id": "ultrasound", "name": "초음파", "desc": "적 탄속 +30%.",
     "effects": {"enemy_fire": 1.30}},
    {"id": "bounty", "name": "현상금", "desc": "점수 +22%.",
     "effects": {"score_mul": 1.22}},
    {"id": "glass-cannon", "name": "유리 대포", "desc": "플레이어 체력 -25%, 탄속 +25%.",
     "effects": {"player_hp": 0.75, "player_bullet_speed": 1.25}},
    {"id": "swarm", "name": "군체", "desc": "잡몹 +30%, 엘리트 확률 +8%.",
     "effects": {"spawn_mul": 1.30, "elite_add": 0.08}},
    {"id": "regeneration", "name": "재생", "desc": "보스가 8초마다 최대 체력의 0.8%를 회복한다.",
     "effects": {"boss_regen": 0.008}},
    {"id": "sniper", "name": "저격", "desc": "적이 조준 사격을 섞는다(사격 빈도 +20%).",
     "effects": {"enemy_fire": 1.20}},
    {"id": "mirror", "name": "거울", "desc": "플레이어 탄이 벽에서 한 번 튕긴다.",
     "effects": {"bounce": 1}},
]

QUOTES = [
    "코어는 조용하다. 조용할 때가 가장 위험하다.",
    "오늘의 탄막은 오늘만 존재한다.",
    "대시는 공격이 아니라 호흡이다.",
    "점수는 남지만 탄흔은 남지 않는다.",
    "서버가 잠든 새벽, 아레나만 깨어 있다.",
    "한 번의 시드는 한 번의 하루.",
    "빛을 피하지 말고 빛 사이를 걸어라.",
    "기록은 저장되고, 자만은 저장되지 않는다.",
    "탄막의 끝에는 항상 빈틈이 있다.",
    "코어를 지키는 건 손이 아니라 리듬이다.",
    "새벽 4시에 한도는 초기화되고, 아레나는 남는다.",
    "0.1초의 판단이 10초의 생존을 만든다.",
]

SCALES = [
    {"id": "minor", "name": "단조", "steps": [0, 2, 3, 5, 7, 8, 10]},
    {"id": "dorian", "name": "도리안", "steps": [0, 2, 3, 5, 7, 9, 10]},
    {"id": "phrygian", "name": "프리지안", "steps": [0, 1, 3, 5, 7, 8, 10]},
    {"id": "pentatonic", "name": "펜타토닉", "steps": [0, 3, 5, 7, 10]},
]

# JS 쪽(neon-core/game.js)과 테이블을 맞추기 위한 내보내기 순서.
TABLE_EXPORT = {
    "themes": THEMES,
    "arenas": ARENAS,
    "bosses": BOSSES,
    "epithets": EPITHETS,
    "modifiers": MODIFIERS,
    "quotes": QUOTES,
    "scales": SCALES,
    "behaviorNames": BEHAVIOR_NAMES,
}


# --------------------------------------------------------------------- stream
class SeedStream:
    """sha256(문자열 + 호출 번호)의 첫 4바이트를 정수로 쓰는 결정적 난수 흐름.

    같은 날짜면 호출 순서가 같으므로 결과가 항상 같다. 파이썬·JS 양쪽에서
    똑같이 계산되도록 **호출 한 번 = sha256 한 번**으로 단순하게 유지한다.
    """

    def __init__(self, date_key):
        self.date_key = date_key
        self.calls = 0

    def u32(self):
        raw = ("%s%s|%d" % (SALT, self.date_key, self.calls)).encode("utf-8")
        self.calls += 1
        return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big")

    def index(self, size):
        if size <= 0:
            raise ValueError("index size must be positive")
        return self.u32() % size

    def pick(self, seq):
        return seq[self.index(len(seq))]

    def between(self, low, high):
        """low..high (양끝 포함) 정수."""
        if high < low:
            low, high = high, low
        return low + self.u32() % (high - low + 1)

    def shuffle(self, seq):
        items = list(seq)
        for i in range(len(items) - 1, 0, -1):
            j = self.u32() % (i + 1)
            items[i], items[j] = items[j], items[i]
        return items


# ------------------------------------------------------------------- generate
def normalize_date(value=None):
    """'YYYY-MM-DD' → date. 값이 없으면 한국 시간 오늘. 잘못된 날짜는 ValueError."""
    if value:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    return datetime.now(KST).date()


def seed_hex(date_key):
    return hashlib.sha256(("%s%s|seed" % (SALT, date_key)).encode("utf-8")).hexdigest()[:16]


def daily_tag(hex_value):
    """사람이 부르기 좋은 4글자 코드 (예: K7Q2). 공유용."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    number = int(hex_value[:8], 16)
    out = []
    for _ in range(4):
        out.append(alphabet[number % len(alphabet)])
        number //= len(alphabet)
    return "".join(out)


def generate(date_key=None, now=None):
    """하루치 시드 dict. date_key='YYYY-MM-DD'."""
    day = normalize_date(date_key)
    key = day.strftime("%Y-%m-%d")
    rng = SeedStream(key)
    hx = seed_hex(key)

    theme = dict(rng.pick(THEMES))
    arena = dict(rng.pick(ARENAS))
    boss_base = rng.pick(BOSSES)
    epithet = rng.pick(EPITHETS)

    waves = rng.between(5, 7)
    mod_count = 2 + (1 if rng.index(2) == 1 else 0)      # 2 or 3
    pool = [m for m in MODIFIERS if m["id"] != "mirror"] if rng.index(3) == 0 else list(MODIFIERS)
    modifiers = rng.shuffle(pool)[:mod_count]

    # 모디파이어 효과를 하나의 배율 묶음으로 합친다(게임은 이 숫자만 읽는다).
    spawn = {
        "enemy_hp": 1.0, "enemy_speed": 1.0, "enemy_fire": 1.0,
        "player_hp": 1.0, "player_bullet_speed": 1.0, "dash_cd": 1.0,
        "score_mul": 1.0, "spawn_mul": 1.0, "elite_add": 0.0,
        "gravity": 0.0, "meteor": 0.0, "bounce": 0, "boss_regen": 0.0,
    }
    for mod in modifiers:
        for key_name, value in (mod.get("effects") or {}).items():
            if key_name in ("elite_add", "gravity", "meteor", "boss_regen"):
                spawn[key_name] = round(float(spawn[key_name]) + float(value), 3)
            elif key_name == "bounce":
                spawn["bounce"] = int(spawn["bounce"]) + int(value)
            else:
                spawn[key_name] = round(float(spawn[key_name]) * float(value), 4)

    boss_hp = int(round((620 + waves * 90 + rng.between(0, 260)) / 10.0)) * 10
    target = 8000 + waves * 900 + rng.between(0, 2400)
    target = int(round(target * spawn["score_mul"] / 100.0)) * 100

    scale = rng.pick(SCALES)
    music = {
        "scale": scale["id"], "scaleName": scale["name"], "steps": scale["steps"],
        "root": rng.between(92, 146), "tempo": rng.between(112, 140),
        "mode": "major" if scale["id"] == "pentatonic" else "dark",
    }

    boss_name = "%s %s" % (epithet, boss_base["codename"])
    seed_int = int(hx, 16)

    result = {
        "schema": SCHEMA,
        "version": VERSION,
        "engine": "strawberry-runner/scripts/daily_seed.py",
        "date_kst": key,
        "weekday_kr": "월화수목금토일"[day.weekday()],
        "seed_hex": hx,
        "seed_int": seed_int,
        "tag": daily_tag(hx),
        "generated_utc": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": "NEON CORE — %s" % key,
        "theme": theme,
        "arena": arena,
        "boss": {
            "codename": boss_base["codename"],
            "name": boss_name,
            "behavior": boss_base["behavior"],
            "behaviorName": BEHAVIOR_NAMES[boss_base["behavior"]],
            "desc": boss_base["desc"],
            "hp": boss_hp,
            "phases": 3,
            "color": theme["boss"],
            "scale": 1.0 + (seed_int % 5) / 20.0,
        },
        "modifiers": [
            {"id": m["id"], "name": m["name"], "desc": m["desc"], "effects": m["effects"]}
            for m in modifiers
        ],
        "waves": waves,
        "target_score": target,
        "boss_hp": boss_hp,
        "spawn": spawn,
        "music": music,
        "quote": rng.pick(QUOTES),
        "rules": {
            "control": "WASD/방향키 이동 · Space 대시 · 자동 사격",
            "touch": "왼쪽 드래그 이동 · 오른쪽 대시 버튼 · 자동 사격",
            "goal": "웨이브를 버티고 %s를 쓰러뜨린다" % boss_name,
        },
    }
    return result


def dumps(payload, pretty=False):
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(add_help=True, description="NEON CORE daily seed")
    parser.add_argument("--date", help="YYYY-MM-DD (기본: 오늘 KST)")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--out", help="파일로 저장")
    parser.add_argument("--emit-tables", action="store_true", help="JS용 테이블 JSON 출력")
    args = parser.parse_args(argv)

    if args.emit_tables:
        sys.stdout.write(dumps(TABLE_EXPORT, pretty=True))
        return 0

    try:
        payload = generate(args.date)
    except ValueError as exc:
        sys.stderr.write("invalid date: %s\n" % exc)
        return 2

    text = dumps(payload, pretty=args.pretty)
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        sys.stderr.write("output too large\n")
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
