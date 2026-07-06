"""쿠팡 파트너스 Open API(bestcategories)로 '오늘의 쿠팡 랭킹' Top10을 만들어 data/coupang-ranking.json에 저장한다.

- 담은마켓은 생활용품 큐레이션 브랜드이므로 생활용품(1014) / 주방용품(1013) / 홈인테리어(1015)
  세 카테고리의 베스트 상품을 라운드로빈으로 섞어 Top10을 구성한다.
  (1014 단독 조회 시 출산/유아 상품이 다수 섞여 나와 브랜드 톤과 맞지 않는 것을 실제 호출로 확인했음)
- 필요한 환경변수: COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOMAIN = "https://api-gateway.coupang.com"
BEST_CATEGORY_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/bestcategories/{category_id}"

# 생활용품 계열 카테고리 (실제 호출로 확인한 categoryName 기준)
CATEGORIES = [
    (1014, "생활용품"),
    (1013, "주방용품"),
    (1015, "홈인테리어"),
]
PER_CATEGORY_LIMIT = 10
TOP_N = 10

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "coupang-ranking.json"
KST = timezone(timedelta(hours=9))


def generate_authorization(access_key, secret_key, method, path_with_query):
    path, _, query = path_with_query.partition("?")
    signed_date = time.strftime("%y%m%dT%H%M%SZ", time.gmtime())
    message = signed_date + method + path + query
    signature = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={signed_date}, signature={signature}"
    )


def fetch_best_category(access_key, secret_key, category_id, limit):
    path_with_query = f"{BEST_CATEGORY_PATH.format(category_id=category_id)}?limit={limit}"
    authorization = generate_authorization(access_key, secret_key, "GET", path_with_query)
    req = urllib.request.Request(DOMAIN + path_with_query, method="GET")
    req.add_header("Authorization", authorization)
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", [])


def build_top10(access_key, secret_key):
    per_category = []
    for category_id, category_label in CATEGORIES:
        try:
            items = fetch_best_category(access_key, secret_key, category_id, PER_CATEGORY_LIMIT)
        except urllib.error.HTTPError as e:
            print(f"[WARN] category {category_id}({category_label}) 호출 실패: {e.code} {e.read().decode('utf-8', 'ignore')}")
            items = []
        per_category.append(items)

    merged = []
    seen_product_ids = set()
    max_len = max((len(lst) for lst in per_category), default=0)
    for i in range(max_len):
        for items in per_category:
            if i >= len(items):
                continue
            item = items[i]
            product_id = item.get("productId")
            if product_id in seen_product_ids:
                continue
            seen_product_ids.add(product_id)
            merged.append(item)
            if len(merged) >= TOP_N:
                break
        if len(merged) >= TOP_N:
            break

    result = []
    for rank, item in enumerate(merged[:TOP_N], start=1):
        result.append({
            "rank": rank,
            "productId": item.get("productId"),
            "productName": item.get("productName"),
            "productImage": item.get("productImage"),
            "productPrice": item.get("productPrice"),
            "productUrl": item.get("productUrl"),
            "categoryName": item.get("categoryName"),
            "isRocket": item.get("isRocket", False),
        })
    return result


def main():
    access_key = os.environ["COUPANG_ACCESS_KEY"]
    secret_key = os.environ["COUPANG_SECRET_KEY"]

    items = build_top10(access_key, secret_key)
    if not items:
        raise SystemExit("쿠팡 랭킹을 하나도 가져오지 못했습니다. API 키/네트워크를 확인하세요.")

    output = {
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d"),
        "updatedAtIso": datetime.now(KST).isoformat(),
        "source": "쿠팡 파트너스 Open API (bestcategories: 생활용품/주방용품/홈인테리어)",
        "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
        "items": items,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUTPUT_PATH} ({len(items)}개 상품)")


if __name__ == "__main__":
    main()
