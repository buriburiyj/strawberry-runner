#!/usr/bin/env python3
"""collect_deals.py — 무료 AI · 0원 혜택 실측 수집기 (표준 라이브러리만)

무엇을 하나
-----------
공개 소스를 **실제로 열어서** 파싱한 값만 모아, 표준출력에 JSON **한 덩어리**로 찍는다.
추측·가짜 데이터는 만들지 않는다. 파싱에 실패한 소스는 `sources[]`에 실패로 남고,
값을 못 찾은 항목은 `null` 또는 빈 배열이다. 모든 항목에 출처 URL과 수집 시각이 붙는다.

수집 구역
  catalogs   모델 카탈로그 JSON에서 '무료 모델' 개수·ID를 직접 센다
             (OpenRouter / NVIDIA NIM / Kilo / OVHcloud / Pollinations)
  offers     공식 가격·무료 페이지의 문장을 그대로 발췌해 무료 조건을 남긴다
             (Oracle / Google Cloud / Cloudflare Workers / GitHub Copilot / Kiro)
  free_games 0원 배포 중·예정인 게임 (Epic Games KR / Steam 100% 할인)
  curated    buriburiyj.github.io/free-deals/data.json 의 기존 혜택 생존·마감 확인

어디서 도나
-----------
  * strawberry-runner 의 GitHub Actions 작업(python_script / shell_cmd) — 120초 제한
  * 로컬/예약 작업 — 같은 결과

쓰는 법
-------
    python3 scripts/collect_deals.py                 # stdout에 JSON 1개
    python3 scripts/collect_deals.py --pretty        # 사람이 읽기 좋게
    python3 scripts/collect_deals.py --out deals.json
    python3 scripts/collect_deals.py --only catalogs,free_games
    python3 scripts/collect_deals.py --timeout 15

지키는 선
---------
  * 토큰·키를 쓰지 않는다. 비밀값이 필요 없는 공개 페이지만 본다.
  * stdout은 16KB를 넘지 않는다(strawberry-runner 저장 한도). 목록은 잘라서 넣는다.
  * 실패해도 예외로 죽지 않는다. 소스별 상태를 JSON에 적고 계속 간다.
  * 종료 코드: 0 = 한 구역 이상 성공, 1 = 전부 실패(또는 출력 생성 실패).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

SCHEMA = 1
VERSION = "1.0.0"
KST = timezone(timedelta(hours=9))
UA = (
    "Mozilla/5.0 (compatible; buriburiyj-deals-collector/1.0; "
    "+https://github.com/buriburiyj/strawberry-runner)"
)
MAX_BODY = 3 * 1024 * 1024          # 소스 한 개에서 읽는 최대 바이트
MAX_OUTPUT_BYTES = 15 * 1024        # stdout 상한(러너 저장 한도 16KB보다 여유)
DEFAULT_TIMEOUT = 20
FINISHED_SLACK = 6                  # 요약 로그용 여유

# 기존 큐레이션 목록(사이트가 읽는 파일). 생존·마감 확인에만 쓴다.
CURATED_URL = (
    "https://raw.githubusercontent.com/buriburiyj/buriburiyj.github.io/main/"
    "free-deals/data.json"
)

# ---------------------------------------------------------------- source registry
# free 판정 방식
#   pricing_zero : pricing.prompt == pricing.completion == "0" (실제 0원 가격표)
#   is_free      : 항목의 isFree == true
#   account_only : 카탈로그 전체가 '계정만 있으면 무료' (가격표 필드 없음) — 단정하지 않고 basis에 표기
CATALOGS = [
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/models",
        "free": "pricing_zero",
        "basis": "pricing.prompt·completion = 0 인 모델만 집계",
    },
    {
        "id": "nvidia",
        "name": "NVIDIA NIM (build.nvidia.com)",
        "url": "https://integrate.api.nvidia.com/v1/models",
        "free": "account_only",
        "basis": "카탈로그 모델 전체가 무료 크레딧 계정으로 호출 가능(가격표 필드 없음)",
    },
    {
        "id": "kilo",
        "name": "Kilo AI Gateway",
        "url": "https://ai-gateway.kilo.ai/api/v1/models",
        "free": "is_free",
        "basis": "isFree = true 인 모델만 집계",
    },
    {
        "id": "ovh",
        "name": "OVHcloud AI Endpoints",
        "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models",
        "free": "account_only",
        "basis": "카탈로그 모델 전체가 무료 한도로 호출 가능(가격표 필드 없음)",
    },
    {
        "id": "pollinations",
        "name": "Pollinations",
        "url": "https://text.pollinations.ai/models",
        "free": "account_only",
        "basis": "키 없이(anonymous tier) 호출 가능한 모델 목록",
    },
]

# 공식 페이지에서 '무료' 문장을 그대로 발췌한다. 해석해서 새 문장을 만들지 않는다.
OFFERS = [
    {
        "id": "oracle-cloud-free",
        "vendor": "Oracle Cloud",
        "title": "Oracle Cloud Free Tier",
        "url": "https://www.oracle.com/cloud/free/",
        "patterns": [
            ("usd_credit", r"US\$\s?\d[\d,]*\s+(?:cloud\s+)?credit|US\$\s?\d[\d,]*\s*클라우드\s*크레딧"),
            ("always_free", r"Always Free Services|상시\s*무료\s*서비스"),
        ],
    },
    {
        "id": "google-cloud-free",
        "vendor": "Google Cloud",
        "title": "Google Cloud Free",
        "url": "https://cloud.google.com/free",
        "patterns": [
            ("usd_credit", r"\$\s?\d[\d,]*\s+in\s+free\s+credit|\$\s?\d[\d,]*\s*의\s*무료\s*크레딧"),
            ("always_free", r"\d+\+?\s+always\s+free\s+products|\d+\s*개\s*이상의\s*무료\s*등급\s*제품"),
        ],
    },
    {
        "id": "cloudflare-workers-free",
        "vendor": "Cloudflare Workers",
        "title": "Workers Free 플랜",
        "url": "https://developers.cloudflare.com/workers/platform/pricing/",
        "patterns": [
            ("free_plan", r"Workers Free plan"),
            ("free_requests_day", r"Free\s+100,000 per day"),
        ],
    },
    {
        "id": "github-copilot-free",
        "vendor": "GitHub",
        "title": "GitHub Copilot Free",
        "url": "https://github.com/pricing",
        "patterns": [
            ("copilot_free", r"up to [\d,]+ completions per month|월\s*최대\s*[\d,]+\s*회"),
        ],
    },
    {
        "id": "kiro-free",
        "vendor": "AWS Kiro",
        "title": "Kiro Free 티어",
        "url": "https://kiro.dev/pricing/",
        "patterns": [
            ("free_tier", r"Kiro Free tier, which includes \d+ credits per month|Kiro Free 티어"),
        ],
    },
]

EPIC_FREE_URL = (
    "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    "?locale=ko-KR&country=KR&allowCountries=KR"
)
STEAM_FREE_URL = (
    "https://store.steampowered.com/search/results/"
    "?query&start=0&count=40&maxprice=free&specials=1&infinite=1&cc=kr&l=korean"
)

CARD_PATTERN = re.compile(
    r"no credit card(?: required)?|without a credit card|credit card required"
    r"|no card required",
    re.I,
)


# --------------------------------------------------------------------- utilities
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def kst_label(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT, accept: str = "*/*") -> dict:
    """GET one URL. Never raises: failures come back as {'ok': False, 'error': ...}."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Encoding": "gzip",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BODY)
            if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return {
                "ok": True,
                "status": int(getattr(response, "status", 200)),
                "final_url": response.geturl(),
                "body": raw.decode("utf-8", "replace"),
                "ms": int((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": int(exc.code),
            "final_url": url,
            "body": "",
            "ms": int((time.monotonic() - started) * 1000),
            "error": "HTTP %s" % exc.code,
        }
    except Exception as exc:  # URLError, timeout, ssl, decoding …
        return {
            "ok": False,
            "status": 0,
            "final_url": url,
            "body": "",
            "ms": int((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }


def plain_text(markup: str) -> str:
    """HTML → 문장을 뽑을 수 있는 평문."""
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str):
    """아주 거친 문장 분리 — 발췌용이므로 정확도보다 안정성을 택한다."""
    parts = re.split(r"(?<=[.!?。])\s+", text)
    for part in parts:
        part = part.strip()
        if part:
            yield part


def sentence_with(text: str, match_start: int, limit: int = 220) -> str:
    """매치가 들어 있는 문장을 limit자까지 잘라 돌려준다."""
    before = text.rfind(". ", 0, match_start)
    start = 0 if before < 0 else before + 2
    end = text.find(". ", match_start)
    end = len(text) if end < 0 else end + 1
    piece = text[start:end].strip()
    if len(piece) > limit:
        offset = max(0, match_start - start - limit // 3)
        piece = piece[offset:offset + limit].strip()
        piece = ("…" + piece) if offset > 0 else piece
    return piece


def money(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def short_id(model_id: str) -> str:
    return model_id[-1 * 60:] if len(model_id) > 60 else model_id


# ----------------------------------------------------------------------- catalogs
def parse_catalog(spec: dict, body: str) -> dict:
    """Return {'total': n, 'free': n, 'free_ids': [...], 'error': str|None}."""
    try:
        data = json.loads(body)
    except ValueError:
        return {"total": None, "free": None, "free_ids": [], "error": "not_json"}

    if isinstance(data, list):                      # Pollinations: 모델 dict 배열
        models = data
    elif isinstance(data, dict):
        models = data.get("data") or data.get("models") or []
    else:
        models = []
    if not isinstance(models, list):
        return {"total": None, "free": None, "free_ids": [], "error": "unexpected_shape"}

    total = len(models)
    free_ids = []
    mode = spec["free"]
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or model.get("name") or "")
        if mode == "pricing_zero":
            pricing = model.get("pricing") or {}
            prompt = str(pricing.get("prompt", "")).strip()
            completion = str(pricing.get("completion", "")).strip()
            if prompt not in ("0", "0.0", "0.00") or completion not in ("0", "0.0", "0.00"):
                continue
        elif mode == "is_free":
            if not model.get("isFree"):
                continue
        if model_id:
            free_ids.append(model_id)

    if mode == "account_only":
        free_count = total
        free_ids = [m.get("id") or m.get("name") for m in models if isinstance(m, dict)]
        free_ids = [str(x) for x in free_ids if x]
    else:
        free_count = len(free_ids)

    return {
        "total": total,
        "free": free_count,
        "free_ids": [short_id(x) for x in free_ids[:8]],
        "error": None,
    }


def run_catalog(spec: dict, timeout: float) -> dict:
    result = http_get(spec["url"], timeout=timeout, accept="application/json")
    record = {
        "id": spec["id"],
        "name": spec["name"],
        "url": spec["url"],
        "kind": "catalog",
        "status": result["status"],
        "ms": result["ms"],
        "ok": False,
        "items": 0,
        "basis": spec["basis"],
        "error": result.get("error"),
    }
    if not result["ok"]:
        return record
    parsed = parse_catalog(spec, result["body"])
    if parsed["error"]:
        record["error"] = parsed["error"]
        return record
    record.update(
        {
            "ok": True,
            "total_models": parsed["total"],
            "free_models": parsed["free"],
            "free_ids": parsed["free_ids"],
            "items": parsed["free"] or 0,
        }
    )
    return record


# ------------------------------------------------------------------------- offers
def parse_offer(spec: dict, body: str) -> dict:
    text = plain_text(body)
    claims = []
    for key, pattern in spec["patterns"]:
        match = re.search(pattern, text, re.I)
        if match:
            claims.append({"key": key, "text": sentence_with(text, match.start())})
    card_match = CARD_PATTERN.search(text)
    card_claim = sentence_with(text, card_match.start()) if card_match else None
    return {"claims": claims, "card_claim": card_claim, "chars": len(text)}


def run_offer(spec: dict, timeout: float) -> dict:
    result = http_get(spec["url"], timeout=timeout, accept="text/html")
    record = {
        "id": spec["id"],
        "vendor": spec["vendor"],
        "title": spec["title"],
        "url": spec["url"],
        "kind": "offer",
        "status": result["status"],
        "ms": result["ms"],
        "final_url": result["final_url"],
        "ok": False,
        "items": 0,
        "claims": [],
        "card_claim": None,
        "error": result.get("error"),
    }
    if not result["ok"]:
        return record
    parsed = parse_offer(spec, result["body"])
    record.update(
        {
            "ok": True,
            "claims": parsed["claims"],
            "card_claim": parsed["card_claim"],
            "page_chars": parsed["chars"],
            "items": len(parsed["claims"]),
        }
    )
    if not parsed["claims"]:
        record["note"] = "이번 수집에서 무료 관련 문구를 찾지 못함(페이지 개편 가능)"
    return record


# --------------------------------------------------------------------- free games
def epic_slug(element: dict) -> str | None:
    """상품 페이지 주소에 쓰는 읽을 수 있는 slug. 해시 id는 마지막 수단."""
    catalog = element.get("catalogNs") or {}
    for mapping in catalog.get("mappings") or []:
        slug = mapping.get("pageSlug")
        if slug:
            return str(slug)
    product = element.get("productSlug")
    if product and not re.fullmatch(r"[0-9a-f]{32}", str(product).split("/")[0]):
        return str(product).split("/")[0]
    url_slug = element.get("urlSlug")
    if url_slug and not re.fullmatch(r"[0-9a-f]{32}", str(url_slug)):
        return str(url_slug)
    return None


def discount_windows(groups) -> list:
    windows = []
    for group in groups or []:
        for offer in group.get("promotionalOffers") or []:
            setting = offer.get("discountSetting") or {}
            windows.append(
                {
                    "start": offer.get("startDate"),
                    "end": offer.get("endDate"),
                    "discount": money(setting.get("discountPercentage")),
                }
            )
    return windows


def parse_epic(body: str) -> list:
    try:
        payload = json.loads(body)
        elements = payload["data"]["Catalog"]["searchStore"]["elements"]
    except (ValueError, KeyError, TypeError):
        return []
    games = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        promotions = element.get("promotions") or {}
        now_windows = [w for w in discount_windows(promotions.get("promotionalOffers")) if w["discount"] == 0]
        soon_windows = [w for w in discount_windows(promotions.get("upcomingPromotionalOffers")) if w["discount"] == 0]
        if not now_windows and not soon_windows:
            continue
        price = (element.get("price") or {}).get("totalPrice") or {}
        slug = epic_slug(element)
        status = "now" if now_windows else "soon"
        window = (now_windows or soon_windows)[0]
        games.append(
            {
                "title": str(element.get("title") or "")[:80],
                "store": "Epic Games",
                "status": status,
                "price_krw": money(price.get("originalPrice")),
                "free_from_utc": window["start"],
                "free_until_utc": window["end"],
                "url": ("https://store.epicgames.com/ko/p/" + slug) if slug else "https://store.epicgames.com/ko/free-games",
            }
        )
    games.sort(key=lambda g: (g["status"] != "now", g["free_until_utc"] or ""))
    return games


def parse_steam(body: str) -> list:
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    results = payload.get("results_html") or ""
    rows = re.findall(r'<a[^>]+class="search_result_row[^"]*"[^>]*href="([^"]+)"(.*?)</a>', results, re.S)
    games = []
    for href, chunk in rows:
        title_match = re.search(r'<span class="title">(.*?)</span>', chunk, re.S)
        if not title_match:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        games.append(
            {
                "title": title[:80],
                "store": "Steam",
                "status": "now",
                "price_krw": None,
                "free_from_utc": None,
                "free_until_utc": None,
                "url": html.unescape(href).split("?")[0],
            }
        )
    return games[:10]


def run_free_games(timeout: float) -> tuple:
    epic = http_get(EPIC_FREE_URL, timeout=timeout, accept="application/json")
    steam = http_get(STEAM_FREE_URL, timeout=timeout, accept="application/json")

    games = []
    sources = []

    sources.append(
        {
            "id": "epic-free",
            "name": "Epic Games 무료 배포 (KR)",
            "url": EPIC_FREE_URL.split("?")[0],
            "kind": "free_games",
            "status": epic["status"],
            "ms": epic["ms"],
            "ok": bool(epic["ok"]),
            "items": 0,
            "error": epic.get("error"),
        }
    )
    if epic["ok"]:
        parsed = parse_epic(epic["body"])
        sources[-1]["items"] = len(parsed)
        if not parsed:
            sources[-1]["note"] = "지금·예정 0원 배포 없음"
        games.extend(parsed)

    sources.append(
        {
            "id": "steam-free",
            "name": "Steam 100% 할인 (KR 스토어)",
            "url": STEAM_FREE_URL.split("?")[0],
            "kind": "free_games",
            "status": steam["status"],
            "ms": steam["ms"],
            "ok": bool(steam["ok"]),
            "items": 0,
            "error": steam.get("error"),
        }
    )
    if steam["ok"]:
        parsed = parse_steam(steam["body"])
        sources[-1]["items"] = len(parsed)
        if not parsed:
            sources[-1]["note"] = "지금 100% 할인 항목 없음"
        games.extend(parsed)

    return games[:14], sources


# ------------------------------------------------------------------------ curated
def check_one_deal(deal: dict, timeout: float) -> dict:
    sources = deal.get("src") or []
    url = (sources[0] or {}).get("url") if sources else None
    record = {
        "id": deal.get("id"),
        "title": (deal.get("title") or "")[:70],
        "tier": deal.get("tier"),
        "card": deal.get("card"),
        "korea": deal.get("korea"),
        "deadline": deal.get("deadline"),
        "status": 0,
        "ok": None,
        "final_url": None,
    }
    if not url:
        record["error"] = "no_source_url"
        return record
    result = http_get(url, timeout=timeout, accept="text/html")
    record["status"] = result["status"]
    record["ok"] = bool(result["ok"])
    record["final_url"] = result["final_url"] if result["final_url"] != url else None
    if not result["ok"]:
        record["error"] = result.get("error")
    return record


def run_curated(timeout: float) -> tuple:
    """기존 큐레이션 22건의 출처 링크 생존과 마감 경과만 본다(내용은 건드리지 않는다)."""
    fetched = http_get(CURATED_URL, timeout=timeout, accept="application/json")
    source = {
        "id": "curated-deals",
        "name": "buriburiyj.github.io/free-deals 데이터",
        "url": CURATED_URL.split("/main/")[0] + "/main/free-deals/data.json",
        "kind": "curated",
        "status": fetched["status"],
        "ms": fetched["ms"],
        "ok": bool(fetched["ok"]),
        "items": 0,
        "error": fetched.get("error"),
    }
    if not fetched["ok"]:
        return {"total": 0, "checked": [], "summary": None}, [source]
    try:
        data = json.loads(fetched["body"])
    except ValueError:
        source["error"] = "not_json"
        return {"total": 0, "checked": [], "summary": None}, [source]

    deals = [d for d in (data.get("deals") or []) if isinstance(d, dict)]
    today = now_utc().astimezone(KST).date()
    checked = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(check_one_deal, deal, min(timeout, 12)) for deal in deals]
        for future in concurrent.futures.as_completed(futures):
            try:
                checked.append(future.result())
            except Exception:                       # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
                continue
    checked.sort(key=lambda r: str(r.get("id")))

    alive = [r for r in checked if r.get("ok")]
    broken = [r for r in checked if r.get("ok") is False]
    expired = []
    for record in checked:
        deadline = record.get("deadline")
        if not deadline or record.get("tier") == "ended":
            continue
        try:
            if datetime.fromisoformat(deadline).date() < today:
                expired.append(record["id"])
        except ValueError:
            continue

    source["items"] = len(checked)
    return (
        {
            "total": len(checked),
            "alive": len(alive),
            "broken": [{"id": r["id"], "title": r["title"], "error": r.get("error") or r.get("status")} for r in broken][:8],
            "expired": expired[:10],
            "checked": [
                {
                    "id": r["id"],
                    "tier": r["tier"],
                    "status": r["status"],
                    "ok": r["ok"],
                    "checked_utc": iso(fetched_at_global),
                }
                for r in checked
            ],
        },
        [source],
    )


fetched_at_global = now_utc()   # run_curated가 참조하는 수집 시각(gather에서 갱신)


# --------------------------------------------------------------------------- main
def source_line(record: dict) -> dict:
    """소스 상태 한 줄 — 대시보드의 '무엇을 언제 확인했나' 표에 그대로 쓴다."""
    line = {
        "id": record.get("id"),
        "name": record.get("name") or record.get("vendor"),
        "url": record.get("url"),
        "kind": record.get("kind"),
        "status": record.get("status"),
        "ms": record.get("ms"),
        "ok": bool(record.get("ok")),
        "items": record.get("items") or 0,
        "checked_utc": iso(fetched_at_global),
    }
    if record.get("error"):
        line["error"] = record["error"]
    if record.get("note"):
        line["note"] = record["note"]
    return line


def gather(timeout: float, only: set | None) -> dict:
    global fetched_at_global
    started = time.monotonic()
    fetched_at_global = now_utc()
    sources = []
    sections = {"catalogs": [], "offers": [], "free_games": [], "curated": None}

    def wanted(name: str) -> bool:
        return only is None or name in only

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {}
        if wanted("catalogs"):
            for spec in CATALOGS:
                future_map[pool.submit(run_catalog, spec, timeout)] = ("catalogs", spec["id"])
        if wanted("offers"):
            for spec in OFFERS:
                future_map[pool.submit(run_offer, spec, timeout)] = ("offers", spec["id"])
        if wanted("free_games"):
            future_map[pool.submit(run_free_games, timeout)] = ("free_games", "games")
        if wanted("curated"):
            future_map[pool.submit(run_curated, timeout)] = ("curated", "curated")
        for future in concurrent.futures.as_completed(future_map):
            bucket, key = future_map[future]
            try:
                results[(bucket, key)] = future.result()
            except Exception as exc:                # noqa: BLE001 — 소스 하나가 죽어도 계속
                results[(bucket, key)] = {"error": type(exc).__name__, "ok": False, "status": 0}

    for bucket, spec_list in (("catalogs", CATALOGS), ("offers", OFFERS)):
        if not wanted(bucket):
            continue
        for spec in spec_list:
            record = results.get((bucket, spec["id"])) or {}
            record.setdefault("id", spec["id"])
            record.setdefault("url", spec["url"])
            record.setdefault("kind", bucket[:-1])
            record.setdefault("status", 0)
            record.setdefault("ms", None)
            record.setdefault("ok", False)
            record.setdefault("items", 0)
            if bucket == "catalogs":
                record.setdefault("name", spec["name"])
                record.setdefault("basis", spec["basis"])
                record.setdefault("free_ids", [])
            else:
                record.setdefault("vendor", spec["vendor"])
                record.setdefault("title", spec["title"])
                record.setdefault("claims", [])
                record.setdefault("card_claim", None)
            sections[bucket].append(record)
            sources.append(source_line(record))

    if wanted("free_games"):
        games_result = results.get(("free_games", "games")) or ([], [])
        games, game_sources = games_result if isinstance(games_result, tuple) else ([], [])
        sections["free_games"] = games
        sources.extend(game_sources)

    if wanted("curated"):
        curated_result = results.get(("curated", "curated")) or (None, [])
        curated, curated_sources = curated_result if isinstance(curated_result, tuple) else (None, [])
        sections["curated"] = curated
        sources.extend(curated_sources)

    sections["catalogs"].sort(key=lambda r: r["id"])
    sections["offers"].sort(key=lambda r: r["id"])
    sources.sort(key=lambda s: (str(s.get("kind")), str(s.get("id"))))

    free_models = sum(int(c.get("free_models") or 0) for c in sections["catalogs"] if c.get("ok"))
    games_now = [g for g in sections["free_games"] if g.get("status") == "now"]
    games_soon = [g for g in sections["free_games"] if g.get("status") == "soon"]
    claim_count = sum(len(o["claims"]) for o in sections["offers"] if o.get("ok"))
    curated = sections["curated"] or {}

    totals = {
        "sources": len(sources),
        "sources_ok": sum(1 for s in sources if s.get("ok")),
        "free_models": free_models,
        "offer_claims": claim_count,
        "games_now": len(games_now),
        "games_soon": len(games_soon),
        "curated_total": curated.get("total") or 0,
        "curated_alive": curated.get("alive") or 0,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }

    sections["totals"] = totals
    sections["sources"] = sources
    return sections


def build_payload(timeout: float, only: set | None) -> dict:
    collected = now_utc()
    sections = gather(timeout, only)
    payload = {
        "schema": SCHEMA,
        "collector": "strawberry-runner/scripts/collect_deals.py",
        "version": VERSION,
        "run_id": "deals-" + collected.strftime("%Y%m%dT%H%MZ"),
        "collected_utc": iso(collected),
        "collected_kst": kst_label(collected) + " KST",
        "kst_date": collected.astimezone(KST).strftime("%Y-%m-%d"),
        "totals": sections["totals"],
        "catalogs": sections["catalogs"],
        "offers": sections["offers"],
        "free_games": sections["free_games"],
        "curated": sections["curated"],
        "sources": sections["sources"],
        "rules": {
            "facts_only": "이 파일의 모든 값은 수집 시점에 실제로 받아온 응답에서만 나온다.",
            "claims": "offers[].claims 는 공식 페이지 문장을 그대로 잘라낸 발췌다(해석·추정 없음).",
            "unknown": "파싱 실패·문구 없음은 null 또는 빈 배열이며, sources[]에 실패로 남는다.",
        },
    }
    return payload


def trim_to_budget(payload: dict, budget: int = MAX_OUTPUT_BYTES) -> dict:
    """stdout 한도에 맞춰 목록 길이를 단계적으로 줄인다.

    값(사실)은 지우지 않고, 목록의 "몇 개를 담을지"와 발췌 길이만 줄인다.
    줄여도 넘치면 마지막 단계에서 상세 목록을 비우고 요약 숫자만 남긴다.
    """
    def size():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def cap_free_ids(limit):
        for catalog in payload.get("catalogs") or []:
            catalog["free_ids"] = (catalog.get("free_ids") or [])[:limit]

    def cap_games(limit):
        payload["free_games"] = (payload.get("free_games") or [])[:limit]

    def cap_curated(limit):
        curated = payload.get("curated")
        if isinstance(curated, dict):
            curated["checked"] = (curated.get("checked") or [])[:limit]

    def cap_claims(chars, per_offer=None):
        for offer in payload.get("offers") or []:
            claims = offer.get("claims") or []
            if per_offer is not None:
                claims = claims[:per_offer]
            for claim in claims:
                claim["text"] = claim["text"][:chars]
            offer["claims"] = claims

    def cap_sources(limit):
        payload["sources"] = (payload.get("sources") or [])[:limit]

    steps = [
        lambda: cap_free_ids(4),
        lambda: cap_claims(180),
        lambda: cap_games(6),
        lambda: cap_curated(0),
        lambda: cap_claims(120, per_offer=1),
        lambda: cap_games(3),
        lambda: cap_free_ids(2),
        lambda: cap_sources(13),
        lambda: cap_claims(80),
        lambda: cap_free_ids(0),
        lambda: payload.pop("curated", None) and None,
        lambda: [catalog.pop("free_ids", None) for catalog in payload.get("catalogs") or []],
        lambda: [offer.pop("claims", None) for offer in payload.get("offers") or []],
    ]
    for step in steps:
        if size() <= budget:
            break
        step()
    return payload


def main(argv) -> int:
    parser = argparse.ArgumentParser(description="무료 AI·0원 혜택 실측 수집기")
    parser.add_argument("--out", help="JSON을 파일로도 저장")
    parser.add_argument("--pretty", action="store_true", help="사람이 읽기 좋게 출력")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="소스별 타임아웃(초)")
    parser.add_argument("--only", help="수집 구역 제한: catalogs,offers,free_games,curated")
    parser.add_argument("--quiet", action="store_true", help="요약 줄을 stderr에 쓰지 않음")
    args = parser.parse_args(argv)

    only = None
    if args.only:
        only = {part.strip() for part in args.only.split(",") if part.strip()}
        unknown = only - {"catalogs", "offers", "free_games", "curated"}
        if unknown:
            sys.stderr.write("unknown section: %s\n" % ", ".join(sorted(unknown)))
            return 2

    payload = build_payload(max(5.0, min(args.timeout, 60.0)), only)
    payload = trim_to_budget(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=1 if args.pretty else None,
                      separators=None if args.pretty else (",", ":"))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    sys.stdout.write(text + "\n")

    totals = payload["totals"]
    if not args.quiet:
        sys.stderr.write(
            "sources %d/%d · free models %d · claims %d · games %d+%d · curated %d/%d · %dms\n"
            % (
                totals["sources_ok"], totals["sources"], totals["free_models"],
                totals["offer_claims"], totals["games_now"], totals["games_soon"],
                totals["curated_alive"], totals["curated_total"], totals["duration_ms"],
            )
        )
    return 0 if totals["sources_ok"] >= 1 else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
