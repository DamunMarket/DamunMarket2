"""네이버 뉴스·블로그 검색 API + 데이터랩 검색어트렌드 API로 '오늘의 검색어 랭킹' Top10을 만든다.

네이버는 2021년에 공식 실시간 검색순위 API 서비스를 종료했다. 그래서 이 스크립트는:
  1) 뉴스 검색 API로 오늘자 여러 분야 뉴스 제목/요약에서 자주 등장하는 키워드 후보를 다수 뽑고,
  2) 데이터랩 검색어트렌드 API로 각 후보 키워드의 최근 검색량 추이를 조회해서
  3) 상승세(최근 대비 증가폭)가 뚜렷한 상위 10개를 뽑는다.
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

# 오늘자 화제성 키워드 후보를 폭넓게 발굴하기 위한 분야별 시드 쿼리
SEED_QUERIES = ["정치", "경제", "사회", "생활문화", "IT 과학", "세계", "연예", "스포츠"]
CANDIDATES_PER_SEED = 100
MIN_CANDIDATE_FREQ = 4
MAX_CANDIDATES = 100
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
}
SEED_STOPWORDS = {q.replace(" ", "") for q in SEED_QUERIES} | {"정치적", "경제적", "사회적", "과학기술정보통신부"}
JOSA_SUFFIXES = sorted([
    "으로서", "로서", "이라며", "라며", "이라고", "라고", "에서는", "에서", "에게",
    "으로", "로써", "로는", "로도", "로", "까지", "부터", "보다", "처럼", "마저", "조차",
    "이다", "이며", "이나", "이니", "은", "는", "이", "가", "을", "를", "의", "도", "만", "와", "과",
], key=len, reverse=True)

TAG_RE = re.compile(r"</?b>")
CLEAN_TOKEN_RE = re.compile(r"^[가-힣0-9]+$")
PURE_NUMBER_RE = re.compile(r"^[0-9,.%]+$")


def clean_text(raw):
    text = html.unescape(raw)
    text = TAG_RE.sub("", text)
    return text


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


def discover_candidates(client_id, client_secret):
    bigram_counter = Counter()
    unigram_counter = Counter()
    for query in SEED_QUERIES:
        try:
            result = naver_get(
                "search/news.json",
                {"query": query, "display": CANDIDATES_PER_SEED, "sort": "date"},
                client_id, client_secret,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"[WARN] 뉴스 검색 실패 ({query}): {e}")
            continue
        for item in result.get("items", []):
            text = clean_text(item.get("title", "")) + " " + clean_text(item.get("description", ""))
            tokens = tokenize(text)
            bigram_counter.update(bigrams(tokens))
            unigram_counter.update(tokens)

    # 단일 고유명사(인물/기관/지명)는 실제로 검색되는 경우가 많아 우선순위를 높게 두고,
    # "손흥민 부상"처럼 사건을 특정하는 두 단어 조합은 보조 후보로 함께 넣는다.
    # (문장 내에서 우연히 붙어있는 조각 구문은 대부분 데이터랩에 검색량이 없어 자동으로 걸러진다)
    unigram_candidates = [word for word, freq in unigram_counter.most_common() if freq >= MIN_CANDIDATE_FREQ]
    bigram_candidates = [phrase for phrase, freq in bigram_counter.most_common() if freq >= 2]

    candidates = []
    seen = set()
    for word in unigram_candidates + bigram_candidates:
        if word in seen:
            continue
        seen.add(word)
        candidates.append(word)
    return candidates[:MAX_CANDIDATES]


def filter_by_specificity(candidates, client_id, client_secret):
    """뉴스 전체 건수로 후보를 거른다.

    "한국", "행사", "오는"처럼 지나치게 흔한 일반 단어는 뉴스 전체 건수가
    수천만 건에 달해 특정 화제라 보기 어렵다. 반대로 너무 적으면(오탈자 등)
    화제성이 없다고 보고 제외한다. 특정 사건/인물을 가리키는 키워드는 보통
    이 사이 어딘가에 들어온다는 것을 실제 호출로 확인해 임계값을 정했다.
    """
    kept = []
    news_counts = {}
    for keyword in candidates:
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
    return kept, news_counts


def fetch_trend_scores(candidates, client_id, client_secret):
    end_date = datetime.now(KST).date()
    start_date = end_date - timedelta(days=TREND_LOOKBACK_DAYS - 1)
    scores = {}
    for i in range(0, len(candidates), 5):
        batch = candidates[i:i + 5]
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
                continue
            ratios = [p["ratio"] for p in data_points]
            recent_avg = sum(ratios[-2:]) / 2
            earlier_avg = sum(ratios[:2]) / 2
            growth = recent_avg - earlier_avg
            scores[group["title"]] = {
                "growth": growth,
                "recentRatio": ratios[-1],
                "series": ratios,
            }
    return scores


def fetch_blog_counts(keywords, client_id, client_secret):
    blog_counts = {}
    for keyword in keywords:
        try:
            blog_result = naver_get("search/blog.json", {"query": keyword, "display": 1}, client_id, client_secret)
            blog_counts[keyword] = blog_result.get("total", 0)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            blog_counts[keyword] = 0
    return blog_counts


def main():
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    candidates = discover_candidates(client_id, client_secret)
    if not candidates:
        raise SystemExit("키워드 후보를 하나도 발굴하지 못했습니다.")
    print(f"후보 키워드 {len(candidates)}개 발굴")

    specific_candidates, news_counts = filter_by_specificity(candidates, client_id, client_secret)
    print(f"화제성 후보로 압축: {len(specific_candidates)}개 {specific_candidates}")
    if not specific_candidates:
        raise SystemExit("특정 화제로 볼 만한 후보가 남지 않았습니다.")

    scores = fetch_trend_scores(specific_candidates, client_id, client_secret)
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["growth"], reverse=True)
    top_keywords = [kw for kw, _ in ranked[:TOP_N]]
    if not top_keywords:
        raise SystemExit("데이터랩 트렌드 점수를 하나도 계산하지 못했습니다.")

    blog_counts = fetch_blog_counts(top_keywords, client_id, client_secret)

    items = []
    for rank, keyword in enumerate(top_keywords, start=1):
        score = scores[keyword]
        items.append({
            "rank": rank,
            "keyword": keyword,
            "growthScore": round(score["growth"], 2),
            "recentRatio": score["recentRatio"],
            "newsCount": news_counts.get(keyword, 0),
            "blogCount": blog_counts.get(keyword, 0),
        })

    output = {
        "updatedAt": datetime.now(KST).strftime("%Y-%m-%d"),
        "updatedAtIso": datetime.now(KST).isoformat(),
        "source": "네이버 뉴스·블로그·데이터랩 기준 (네이버 공식 실시간 검색순위가 아닙니다)",
        "method": "오늘자 뉴스 제목·요약에서 키워드 후보를 추출한 뒤, 데이터랩 검색어트렌드로 최근 상승폭이 큰 순으로 정렬했습니다.",
        "items": items,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUTPUT_PATH} ({len(items)}개 키워드)")


if __name__ == "__main__":
    main()
