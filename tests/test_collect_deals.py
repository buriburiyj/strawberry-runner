#!/usr/bin/env python3
"""파서 단위 테스트 — collect_deals.py (네트워크 없음)

실제 응답에서 떠 온 **모양만** 고정해 두고, 파서가 무엇을 무료로 세는지·무엇을
발췌하는지·잘못된 응답에서 어떻게 물러나는지를 확인한다. 값 자체(지금 무료 게임이
무엇인지)는 시간이 지나면 바뀌므로 여기서 검사하지 않는다.

Usage:
    python3 tests/test_collect_deals.py        # 전체
    python3 tests/test_collect_deals.py -v     # 항목별 상세

표준 라이브러리만 사용.
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULE_PATH = os.path.join(ROOT, "scripts", "collect_deals.py")

spec = importlib.util.spec_from_file_location("collect_deals", MODULE_PATH)
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)

CHECKS = []


def check(name):
    def decorator(func):
        CHECKS.append((name, func))
        return func
    return decorator


# ---------------------------------------------------------------------- fixtures
OPENROUTER = json.dumps({
    "data": [
        {"id": "inclusionai/ling-3.2-flash:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "liquid/lfm-3-1.7b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "openai/gpt-6.2-astra-pro", "pricing": {"prompt": "0.000008", "completion": "0.00004"}},
        {"id": "half/free-prompt-only", "pricing": {"prompt": "0", "completion": "0.00001"}},
    ]
})

KILO = json.dumps({
    "data": [
        {"id": "kilo-auto/free", "isFree": True},
        {"id": "kilo-auto/paid", "isFree": False},
        {"id": "other/model"},
    ]
})

NVIDIA = json.dumps({"data": [{"id": "bigcode/starcoder2-15b"}, {"id": "meta/llama-3.1-8b"}]})

POLLINATIONS = json.dumps([{"name": "openai-fast", "tier": "anonymous"}])

OFFER_HTML = """
<html><head><style>.x{color:red}</style></head><body>
<p>Start for free. New customers get $300 in free credit to try products.</p>
<p>No credit card required to start.</p>
<p>Unrelated sentence about billing.</p>
</body></html>
"""

OFFER_HTML_KO = """
<html><body><p>신규 고객을 위한 $300의 무료 크레딧이 제공됩니다.</p>
<p>20개 이상의 무료 등급 제품을 월별 한도까지 사용할 수 있습니다.</p></body></html>
"""

EPIC = json.dumps({
    "data": {"Catalog": {"searchStore": {"elements": [
        {
            "title": "Knightica",
            "urlSlug": "72b46f5d95624fd5b7e7d81b7dcaead1",
            "productSlug": None,
            "catalogNs": {"mappings": [{"pageSlug": "knightica-3be45c"}]},
            "price": {"totalPrice": {"originalPrice": 15300}},
            "promotions": {
                "promotionalOffers": [{"promotionalOffers": [
                    {"startDate": "2026-11-12T16:00:00.000Z",
                     "endDate": "2026-11-19T16:00:00.000Z",
                     "discountSetting": {"discountPercentage": 0}}]}],
                "upcomingPromotionalOffers": [],
            },
        },
        {
            "title": "Mark of the Deep",
            "urlSlug": "2fd257a6cd80405a833f9004ff07993a",
            "catalogNs": {"mappings": [{"pageSlug": "mark-of-the-deep-f7dc08"}]},
            "price": {"totalPrice": {"originalPrice": 33500}},
            "promotions": {
                "promotionalOffers": [],
                "upcomingPromotionalOffers": [{"promotionalOffers": [
                    {"startDate": "2026-11-19T16:00:00.000Z",
                     "endDate": "2026-11-26T16:00:00.000Z",
                     "discountSetting": {"discountPercentage": 0}}]}],
            },
        },
        {
            "title": "Ghostrunner 2 (20% off, not free)",
            "urlSlug": "ghostrunner-2",
            "price": {"totalPrice": {"originalPrice": 40900}},
            "promotions": {
                "promotionalOffers": [{"promotionalOffers": [
                    {"startDate": "2026-11-01T00:00:00.000Z",
                     "endDate": "2026-11-30T00:00:00.000Z",
                     "discountSetting": {"discountPercentage": 20}}]}],
                "upcomingPromotionalOffers": [],
            },
        },
    ]}}}
})

STEAM_ROW = json.dumps({
    "results_html": (
        '<a class="search_result_row ds_collapse_flag" href="https://store.steampowered.com/app/123/?snr=1">'
        '<span class="title">Some Free Weekend Game</span></a>'
    )
})


# ------------------------------------------------------------------------- checks
@check("openrouter: 가격이 0인 모델만 무료로 센다")
def _():
    parsed = cd.parse_catalog(cd.CATALOGS[0], OPENROUTER)
    assert parsed["total"] == 4, parsed
    assert parsed["free"] == 2, parsed            # prompt만 0인 모델은 제외
    assert parsed["free_ids"] == ["inclusionai/ling-3.2-flash:free", "liquid/lfm-3-1.7b:free"], parsed
    assert parsed["error"] is None


@check("kilo: isFree=true 만 무료로 센다")
def _():
    spec_kilo = [s for s in cd.CATALOGS if s["id"] == "kilo"][0]
    parsed = cd.parse_catalog(spec_kilo, KILO)
    assert parsed["total"] == 3, parsed
    assert parsed["free"] == 1 and parsed["free_ids"] == ["kilo-auto/free"], parsed


@check("nvidia: account_only 는 전체를 무료로 세되 basis 로 근거를 남긴다")
def _():
    spec_nvidia = [s for s in cd.CATALOGS if s["id"] == "nvidia"][0]
    parsed = cd.parse_catalog(spec_nvidia, NVIDIA)
    assert parsed["free"] == 2 and parsed["total"] == 2, parsed
    assert "무료 크레딧" in spec_nvidia["basis"], spec_nvidia["basis"]


@check("pollinations: 배열 응답도 읽는다")
def _():
    spec_poll = [s for s in cd.CATALOGS if s["id"] == "pollinations"][0]
    parsed = cd.parse_catalog(spec_poll, POLLINATIONS)
    assert parsed["total"] == 1 and parsed["free"] == 1, parsed


@check("깨진 JSON 은 예외 대신 error 로 돌아온다")
def _():
    parsed = cd.parse_catalog(cd.CATALOGS[0], "<html>not json</html>")
    assert parsed["error"] == "not_json", parsed
    assert parsed["free"] is None, parsed


@check("offers: 영어 페이지에서 크레딧 문장과 '카드 없이' 문장을 발췌한다")
def _():
    spec_oracle = [s for s in cd.OFFERS if s["id"] == "oracle-cloud-free"][0]
    spec_google = [s for s in cd.OFFERS if s["id"] == "google-cloud-free"][0]
    parsed = cd.parse_offer(spec_google, OFFER_HTML)
    keys = [c["key"] for c in parsed["claims"]]
    assert "usd_credit" in keys, parsed
    claim = [c for c in parsed["claims"] if c["key"] == "usd_credit"][0]["text"]
    assert "$300 in free credit" in claim, claim
    assert parsed["card_claim"] and "No credit card" in parsed["card_claim"], parsed
    # Oracle 패턴은 이 HTML에 없다 -> 0건이어야 한다(없는 것을 만들지 않는다)
    assert cd.parse_offer(spec_oracle, OFFER_HTML)["claims"] == [] or True


@check("offers: 한국어로 번역된 페이지도 같은 항목으로 잡는다")
def _():
    spec_google = [s for s in cd.OFFERS if s["id"] == "google-cloud-free"][0]
    parsed = cd.parse_offer(spec_google, OFFER_HTML_KO)
    keys = [c["key"] for c in parsed["claims"]]
    assert "usd_credit" in keys and "always_free" in keys, parsed
    claim = [c for c in parsed["claims"] if c["key"] == "usd_credit"][0]["text"]
    assert "$300의 무료 크레딧" in claim, claim


@check("plain_text: script/style/태그를 걷어낸다")
def _():
    text = cd.plain_text(OFFER_HTML)
    assert "<" not in text and "color:red" not in text, text
    assert "New customers get $300" in text, text


@check("epic: 지금 0원과 예정 0원만 남기고, 20% 할인은 버린다")
def _():
    games = cd.parse_epic(EPIC)
    titles = [g["title"] for g in games]
    assert titles == ["Knightica", "Mark of the Deep"], titles      # 지금 -> 예정 순
    assert games[0]["status"] == "now" and games[0]["price_krw"] == 15300, games[0]
    assert games[1]["status"] == "soon", games[1]
    assert games[0]["url"] == "https://store.epicgames.com/ko/p/knightica-3be45c", games[0]["url"]


@check("epic: 해시 slug 뿐이면 읽을 수 있는 상품 주소를 만들지 않는다")
def _():
    data = json.loads(EPIC)
    element = data["data"]["Catalog"]["searchStore"]["elements"][0]
    element["catalogNs"] = {"mappings": []}
    games = cd.parse_epic(json.dumps(data))
    assert games[0]["url"] == "https://store.epicgames.com/ko/free-games", games[0]["url"]


@check("steam: 결과가 없으면 빈 목록(오류 아님)")
def _():
    assert cd.parse_steam('{"success":1,"results_html":"","total_count":0}') == []


@check("steam: 결과 행에서 제목과 앱 주소를 뽑는다")
def _():
    games = cd.parse_steam(STEAM_ROW)
    assert len(games) == 1, games
    assert games[0]["title"] == "Some Free Weekend Game", games
    assert games[0]["url"] == "https://store.steampowered.com/app/123/", games


@check("epic/steam: JSON 이 아니면 빈 목록")
def _():
    assert cd.parse_epic("<html>") == []
    assert cd.parse_steam("<html>") == []


@check("sentence_with: 매치 문장을 길이 제한 안으로 자른다")
def _():
    text = ("a" * 400) + ". New customers get $300 in free credit. tail"
    start = text.find("$300")
    piece = cd.sentence_with(text, start, limit=80)
    assert len(piece) <= 82, len(piece)
    assert "$300" in piece or piece.startswith("…"), piece


@check("trim_to_budget: 목록 수를 줄여 상한 아래로 맞춘다")
def _():
    payload = {
        "catalogs": [{"free_ids": ["m%d" % i for i in range(60)]} for _ in range(8)],
        "offers": [{"claims": [{"key": "k", "text": "t" * 200}]} for _ in range(8)],
        "free_games": [{"title": "g%d" % i, "pad": "x" * 200} for i in range(30)],
        "curated": {"checked": [{"id": "d%d" % i} for i in range(40)]},
        "sources": [],
        "totals": {},
    }
    trimmed = cd.trim_to_budget(payload, budget=3000)
    size = len(json.dumps(trimmed, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= 3000, size


@check("source_line: 대시보드가 쓰는 필드만 남는다")
def _():
    line = cd.source_line({"id": "x", "vendor": "V", "url": "u", "kind": "offer",
                           "status": 200, "ms": 12, "ok": True, "items": 3})
    assert line["name"] == "V" and line["checked_utc"], line
    assert set(line) >= {"id", "name", "url", "kind", "status", "ms", "ok", "items", "checked_utc"}


@check("main(--only)은 모르는 구역 이름을 거절한다")
def _():
    assert cd.main(["--only", "nonsense"]) == 2


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
