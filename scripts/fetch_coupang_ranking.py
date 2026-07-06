"""쿠팡 파트너스 Open API(bestcategories)로 '카테고리별 오늘의 쿠팡 랭킹 Top10'을 만들어
data/coupang-ranking.json에 저장한다.

- 쿠팡 대분류 15개 각각의 베스트 상품 Top10을 카테고리별로 저장한다.
- 카테고리 ID→이름 매핑은 실제 API 응답(표본 20개 기준 대표 categoryName)으로 확인해 고정했다.
  bestcategories는 인접 카테고리 상품이 일부 섞여 나오므로, 탭 이름은 자동 추론하지 않고 여기서 고정한다.
- 담은마켓(생활용품 큐레이션)에 맞춰 생활용품/주방/홈인테리어를 앞쪽에 배치한다.
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

# (categoryId, 표시이름) — 담은마켓 성격상 생활/주방/인테리어를 앞에 배치
CATEGORIES = [
    (1015, "생활용품"),
    (1013, "주방용품"),
    (1020, "가구/홈인테리어"),
    (1016, "가전디지털"),
    (1010, "뷰티"),
    (1011, "식품"),
    (1012, "로켓프레시"),
    (1014, "출산/유아"),
    (1022, "반려/애완용품"),
    (1021, "문구/사무용품"),
    (1018, "자동차용품"),
    (1003, "패션의류"),
    (1001, "패션잡화"),
    (1002, "스포츠/레저용품"),
    (1019, "도서/음반"),
]
LIMIT = 10

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


def normalize(items):
    result = []
    for rank, item in enumerate(items[:LIMIT], start=1):
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

    categories = []
    for category_id, label in CATEGORIES:
        try:
            raw = fetch_best_category(access_key, secret_key, category_id, LIMIT)
        except urllib.error.HTTPError as e:
            print(f"[WARN] {category_id}({label}) 호출 실패: {e.code} {e.read().decode('utf-8', 'ignore')}")
            raw = []
        except Exception as e:
            print(f"[WARN] {category_id}({label}) 호출 오류: {e}")
            raw = []
        items = normalize(raw)
        if items:
            categories.append({"id": category_id, "label": label, "items": items})
            print(f"{label}({category_id}): {len(items)}개")
        else:
            print(f"[SKIP] {label}({category_id}): 데이터 없음")
        time.sleep(0.3)

    if not categories:
        raise SystemExit("쿠팡 랭킹을 하나도 가져오지 못했습니다. API 키/네트워크를 확인하세요.")

    output = {
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d"),
        "updatedAtIso": datetime.now(KST).isoformat(),
        "source": "쿠팡 파트너스 Open API (bestcategories)",
        "disclosure": "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
        "categories": categories,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUTPUT_PATH} ({len(categories)}개 카테고리)")


if __name__ == "__main__":
    main()
