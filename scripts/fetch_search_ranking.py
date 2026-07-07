"""네이버 뉴스·블로그 검색 API + 데이터랩 검색어트렌드 API로 '분야별 오늘의 검색어 랭킹'을 만든다.

네이버는 2021년에 공식 실시간 검색순위 API 서비스를 종료했다. 그래서 이 스크립트는:
  1) 분야별 뉴스 검색으로 오늘자 화제 키워드 후보를 뽑고,
  2) 데이터랩 검색어트렌드 API로 각 후보의 최근 검색량 추이를 조회해서
  3) 상승세(최근 대비 증가폭)가 뚜렷한 상위 10개를 분야별 Top10으로 만든다.
'전체' 탭은 모든 분야 후보를 합쳐 상승폭 기준 Top10으로 구성한다.
절대 네이버 공식 실시간 검색순위가 아니며, 결과 JSON에 근거(source)를 명시한다.

필요한 환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://openapi.naver.com/v1"
KST = timezone(timedelta(hours=9))
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "search-ranking.json"

# 담은마켓(생활용품 큐레이션) 성격에 맞는 뉴스 분야만 탭으로 구성한다.
# (seedQuery = 뉴스 검색어, label = 화면 탭 이름)
SECTIONS = [
    ("생활문화", "생활·문화"),
    ("연예", "연예"),
    ("스포츠", "스포츠"),
    ("IT 과학", "IT·과학"),
]
NEWS_PER_SECTION = 100
MIN_CANDIDATE_FREQ = 3
MAX_CANDIDATES = 60
MIN_NEWS_COUNT = 200
MAX_NEWS_COUNT = 2_000_000
TREND_LOOKBACK_DAYS = 8
TOP_N = 10

STOPWORDS = {
    "오늘", "관련", "기자", "뉴스", "사진", "영상", "이번", "지난", "당시", "현재", "이후",
    "위해", "대한", "통해", "따르면", "밝혔다", "말했다", "전했다", "있다", "없다", "한다",
    "했다", "된다", "됐다", "한편", "하지만", "그러나", "그리고", "이날", "지역", "발표",
    "예정", "논란", "우려", "확인", "진행", "계획", "결과", "관계자", "이라며", "라며",
    "라고", "밝혔", "전망", "예상", "가능성", "필요", "국내", "해외", "정부", "시장",
    "있는", "없는", "하는", "되는", "같은", "대해", "따라", "보다", "그는", "그녀는",
    "가장", "함께", "모두", "다시", "이제", "정말", "매우", "너무", "많은", "여러",
    "사업", "분야", "대표", "위한", "모델", "지원", "운영", "참여", "기반", "이어",
    "글로벌", "6일", "7일", "8일", "9일", "5일", "4일", "3일", "2일", "1일",
    "10일", "11일", "12일", "13일", "14일", "15일", "16일", "17일", "18일", "19일",
    "20일", "21일", "22일", "23일", "24일", "25일", "26일", "27일", "28일", "29일",
    "30일", "31일", "올해", "내년", "작년", "최근", "일부", "전체", "관련해", "가운데",
    # 분야별 뉴스에서 자주 튀어나오는 일반어 노이즈 (특정 화제가 아님)
    "연예인", "주연", "최애", "본질", "과학적", "피지컬", "실증", "사명",
    "전반기", "후반기", "남자친구", "여자친구",
}
SEED_STOPWORDS = {"생활문화", "생활", "문화", "연예", "스포츠", "과학", "IT", "정치", "경제", "사회", "세계"}
JOSA_SUFFIXES = sorted([
    "으로서", "로서", "이라며", "라며", "이라고", "라고", "에서는", "에서", "에게",
    "으로", "로써", "로는", "로도", "로", "까지", "부터", "보다", "처럼", "마저", "조차",
    "이다", "이며", "이나", "이니", "은", "는", "이", "가", "을", "를", "의", "도", "만", "와", "과",
], key=len, reverse=True)

TAG_RE = re.compile(r"</?b>")
CLEAN_TOKEN_RE = re.compile(r"^[가-힣0-9]+$")
PURE_NUMBER_RE = re.compile(r"^[0-9,.%]+$")


def clean_text(raw):
    return TAG_RE.sub("", html.unescape(raw))


def strip_josa(word):
    for suffix in JOSA_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)]
    return word


def tokenize(text):
    tokens = []
    for raw_word in text.split():
        word = strip_josa(raw_word.strip("\"'.,·:;()[]{}<>…“”‘’"))
        if len(word) < 2 or len(word) > 12:
            continue
        if not CLEAN_TOKEN_RE.match(word):
            continue
        if PURE_NUMBER_RE.match(word):
            continue
        if word in STOPWORDS or word in SEED_STOPWORDS:
            continue
        tokens.append(word)
    return tokens


def bigrams(tokens):
    return [
        f"{tokens[i]} {tokens[i + 1]}"
        for i in range(len(tokens) - 1)
        if tokens[i] != tokens[i + 1]
    ]


def _request_with_retry(req, retries=3):
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_error = e
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            last_error = e
            time.sleep(1)
    raise last_error


def naver_get(path, params, client_id, client_secret):
    url = f"{API_BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("X-Naver-Client-Id", client_id)
    req.add_header("X-Naver-Client-Secret", client_secret)
    return _request_with_retry(req)


def naver_post(path, body, client_id, client_secret):
    url = f"{API_BASE}/{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("X-Naver-Client-Id", client_id)
    req.add_header("X-Naver-Client-Secret", client_secret)
    req.add_header("Content-Type", "application/json")
    return _request_with_retry(req)


def discover_candidates(seed_query, client_id, client_secret):
    bigram_counter = Counter()
    unigram_counter = Counter()
    try:
        result = naver_get(
            "search/news.json",
            {"query": seed_query, "display": NEWS_PER_SECTION, "sort": "date"},
            client_id, client_secret,
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[WARN] 뉴스 검색 실패 ({seed_query}): {e}")
        return []
    for item in result.get("items", []):
        text = clean_text(item.get("title", "")) + " " + clean_text(item.get("description", ""))
        tokens = tokenize(text)
        bigram_counter.update(bigrams(tokens))
        unigram_counter.update(tokens)

    unigram_candidates = [w for w, f in unigram_counter.most_common() if f >= MIN_CANDIDATE_FREQ]
    bigram_candidates = [p for p, f in bigram_counter.most_common() if f >= 2]

    candidates, seen = [], set()
    for word in unigram_candidates + bigram_candidates:
        if word not in seen:
            seen.add(word)
            candidates.append(word)
    return candidates[:MAX_CANDIDATES]


def filter_by_specificity(candidates, client_id, client_secret, news_counts):
    kept = []
    for keyword in candidates:
        if keyword in news_counts:
            total = news_counts[keyword]
        else:
            time.sleep(0.15)
            try:
                result = naver_get("search/news.json", {"query": keyword, "display": 1}, client_id, client_secret)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                print(f"[WARN] 뉴스 건수 조회 실패 ({keyword}): {e}")
                continue
            total = result.get("total", 0)
            news_counts[keyword] = total
        if MIN_NEWS_COUNT <= total <= MAX_NEWS_COUNT:
            kept.append(keyword)
    return kept


def fetch_trend_scores(candidates, client_id, client_secret, cache):
    end_date = datetime.now(KST).date()
    start_date = end_date - timedelta(days=TREND_LOOKBACK_DAYS - 1)
    scores = {}
    pending = [c for c in candidates if c not in cache]
    for i in range(0, len(pending), 5):
        batch = pending[i:i + 5]
        keyword_groups = [{"groupName": kw, "keywords": [kw]} for kw in batch]
        try:
            result = naver_post(
                "datalab/search",
                {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "timeUnit": "date",
                    "keywordGroups": keyword_groups,
                },
                client_id, client_secret,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"[WARN] 데이터랩 조회 실패 (batch {batch}): {e}")
            continue
        for group in result.get("results", []):
            data_points = group.get("data", [])
            if len(data_points) < 4:
                cache[group["title"]] = None
                continue
            ratios = [p["ratio"] for p in data_points]
            cache[group["title"]] = {
                "growth": sum(ratios[-2:]) / 2 - sum(ratios[:2]) / 2,
                "recentRatio": ratios[-1],
                "series": ratios,
            }
    for kw in candidates:
        if cache.get(kw):
            scores[kw] = cache[kw]
    return scores


def fetch_blog_count(keyword, client_id, client_secret, cache):
    if keyword in cache:
        return cache[keyword]
    try:
        result = naver_get("search/blog.json", {"query": keyword, "display": 1}, client_id, client_secret)
        cache[keyword] = result.get("total", 0)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        cache[keyword] = 0
    return cache[keyword]


def build_items(top_keywords, scores, news_counts, blog_cache, client_id, client_secret):
    items = []
    for rank, keyword in enumerate(top_keywords, start=1):
        score = scores[keyword]
        items.append({
            "rank": rank,
            "keyword": keyword,
            "growthScore": round(score["growth"], 2),
            "recentRatio": score["recentRatio"],
            "newsCount": news_counts.get(keyword, 0),
            "blogCount": fetch_blog_count(keyword, client_id, client_secret, blog_cache),
        })
    return items


def main():
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    news_counts = {}      # keyword -> 뉴스 총 건수 (분야 간 공유 캐시)
    trend_cache = {}      # keyword -> 트렌드 점수 or None
    blog_cache = {}       # keyword -> 블로그 건수

    section_results = []          # [(key, label, items)]
    all_scores = {}               # 전체 탭용: keyword -> score
    all_keyword_pool = []

    for seed_query, label in SECTIONS:
        candidates = discover_candidates(seed_query, client_id, client_secret)
        specific = filter_by_specificity(candidates, client_id, client_secret, news_counts)
        scores = fetch_trend_scores(specific, client_id, client_secret, trend_cache)
        ranked = sorted(scores.items(), key=lambda kv: kv[1]["growth"], reverse=True)
        top = [kw for kw, _ in ranked[:TOP_N]]
        items = build_items(top, scores, news_counts, blog_cache, client_id, client_secret)
        section_results.append((seed_query, label, items))
        print(f"[{label}] 후보 {len(candidates)} → 화제 {len(specific)} → Top {len(items)}")
        for kw, sc in scores.items():
            all_scores[kw] = sc
            all_keyword_pool.append(kw)

    # 전체 탭: 모든 분야 후보를 합쳐 상승폭 기준 Top10
    seen = set()
    all_ranked = sorted(all_scores.items(), key=lambda kv: kv[1]["growth"], reverse=True)
    all_top = []
    for kw, _ in all_ranked:
        if kw not in seen:
            seen.add(kw)
            all_top.append(kw)
        if len(all_top) >= TOP_N:
            break
    all_items = build_items(all_top, all_scores, news_counts, blog_cache, client_id, client_secret)

    sections = [{"key": "all", "label": "전체", "items": all_items}]
    for key, label, items in section_results:
        if items:
            sections.append({"key": key, "label": label, "items": items})

    if not all_items:
        raise SystemExit("검색어 랭킹을 하나도 만들지 못했습니다. API 키/쿼터를 확인하세요.")

    output = {
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d"),
        "updatedAtIso": datetime.now(KST).isoformat(),
        "source": "네이버 뉴스·블로그·데이터랩 기준 (네이버 공식 실시간 검색순위가 아닙니다)",
        "method": "분야별 오늘자 뉴스에서 키워드 후보를 추출한 뒤, 데이터랩 검색어트렌드로 최근 상승폭이 큰 순으로 정렬했습니다.",
        "sections": sections,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUTPUT_PATH} ({len(sections)}개 분야)")


if __name__ == "__main__":
    main()
