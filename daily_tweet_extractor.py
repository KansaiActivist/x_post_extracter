#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_tweet_extractor.py

X(旧Twitter)の日常系ツイート(学校・家・通学・通勤など)だけを
Yahooリアルタイム検索 または Nitter経由で抽出するスクリプト。

【重要な注意】
- 対象サイトの利用規約・robots.txtを確認し、節度あるアクセス間隔で使用してください。
- 過度な連続アクセスはIPブロックの原因になります(sleepを入れています)。
- Nitterインスタンスは頻繁に停止・URL変更するため、複数候補を用意しています。
- スクレイピングであるため、サイト側のHTML構造変更で動かなくなることがあります。

使い方:
    python daily_tweet_extractor.py --source yahoo --query "今日 学校" --max 50
    python daily_tweet_extractor.py --source nitter --query "通勤" --max 50
"""

import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# 日常判定用キーワード
# ----------------------------------------------------------------------

# 「日常っぽい」と判断するポジティブキーワード
DAILY_LIFE_KEYWORDS = [
    # 学校
    "学校", "授業", "部活", "宿題", "テスト", "先生", "クラスメイト",
    "通学", "教室", "休み時間", "文化祭", "体育祭", "受験", "塾",
    # 家
    "家", "自宅", "帰宅", "実家", "晩ごはん", "夕飯", "朝ごはん",
    "洗濯", "掃除", "家事", "寝る前", "起きた", "寝坊",
    # 通勤・移動
    "通勤", "満員電車", "電車", "バス", "駅", "会社", "出社",
    "在宅勤務", "残業", "定時", "遅刻", "乗り換え", "始発", "終電",
    # 生活全般
    "眠い", "疲れた", "お腹すいた", "コンビニ", "ランチ", "お弁当",
    "天気", "雨", "暑い", "寒い",
]

# ノイズ・宣伝系を除外するネガティブキーワード
EXCLUDE_KEYWORDS = [
    "プレゼント企画", "フォロー&RT", "フォローで応募", "キャンペーン",
    "PR", "広告", "アフィリエイト", "副業", "稼げる", "LINE登録",
    "セール", "クーポン", "今だけ", "無料招待", "URLをクリック",
    "情報商材", "投資", "仮想通貨", "副収入",
]

URL_PATTERN = re.compile(r"https?://\S+")
HASHTAG_PATTERN = re.compile(r"#\S+")


def is_daily_life_tweet(text: str, include_keywords=None, exclude_keywords=None) -> bool:
    """
    テキストが対象キーワードにマッチするかを判定する。

    include_keywords: これらのいずれかを含めば対象とする(未指定時は日常系デフォルト)
    exclude_keywords: これらのいずれかを含めば除外する(未指定時は宣伝系デフォルト)
    """
    if not text:
        return False

    include_keywords = include_keywords if include_keywords is not None else DAILY_LIFE_KEYWORDS
    exclude_keywords = exclude_keywords if exclude_keywords is not None else EXCLUDE_KEYWORDS

    # 除外キーワードを含む場合はNG
    for ng in exclude_keywords:
        if ng in text:
            return False

    # ハッシュタグが多すぎる(宣伝の可能性)は除外
    if len(HASHTAG_PATTERN.findall(text)) >= 4:
        return False

    # URLが2個以上(宣伝・bot投稿の可能性)は除外
    if len(URL_PATTERN.findall(text)) >= 2:
        return False

    # include_keywords が空リストなら「フィルタなし(全件通過)」として扱う
    if not include_keywords:
        return True

    return any(kw in text for kw in include_keywords)


def clean_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


# ----------------------------------------------------------------------
# Yahoo!リアルタイム検索
# ----------------------------------------------------------------------

def fetch_yahoo_realtime(query: str, max_results: int = 50, sleep_sec: float = 1.5,
                          account: str = None, debug: bool = False):
    """
    Yahoo!リアルタイム検索(search.yahoo.co.jp/realtime)から
    ツイートテキストを取得するジェネレータ。

    account を指定すると "id:アカウント名"(Yahooリアルタイム検索の
    投稿者絞り込み演算子)を検索クエリに付加した上で、
    さらに各ツイートのコンテナ要素内にある投稿者リンク(href に
    "twitter.com/アカウント名" や "x.com/アカウント名" を含むもの)を
    実際にチェックし、一致するツイートだけを返す。
    これにより、id: 演算子だけでは弾ききれない
    (引用元・返信先・関連投稿などで他アカウントの本文が
    ページに混在するケース)を防ぐ。

    ※Yahoo側のHTML構造は変わりやすいため、うまく取得できない場合は
      debug=True で保存されるHTMLを確認し、CONTAINER_SELECTORS /
      AUTHOR_LINK_PATTERNS を実際の構造に合わせて調整してください。
    """
    if account:
        account = account.lstrip("@")
        query = f"{query} id:{account}".strip()

    base_url = "https://search.yahoo.co.jp/realtime/search"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    # ツイート1件分をまとめて含んでいるコンテナ(実際のYahooリアルタイム検索の
    # HTML構造: <div class="Tweet_TweetContainer__xxxx ...">)。
    # 注意: ページ内には「トレンド」欄などの <article> タグも別途存在するため、
    # 汎用的な "article" 等では誤ったコンテナを拾ってしまう。必ずこの
    # TweetContainer クラスを使うこと。
    CONTAINER_SELECTOR = "div[class*='TweetContainer']"
    BODY_SELECTOR = "p[class*='Tweet_body']"
    AUTHOR_LINK_SELECTOR = "a[class*='Tweet_authorID']"

    collected = 0
    page = 1
    while collected < max_results:
        params = {"p": query, "ei": "utf-8", "b": (page - 1) * 10 + 1}
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[warn] Yahooリクエスト失敗: {e}", file=sys.stderr)
            break

        if debug:
            dump_path = f"yahoo_debug_page{page}.html"
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
            print(f"[debug] 生HTMLを {dump_path} に保存しました。", file=sys.stderr)

        soup = BeautifulSoup(resp.text, "html.parser")
        containers = soup.select(CONTAINER_SELECTOR)

        if not containers:
            print("[warn] ツイートコンテナ(TweetContainer)が見つかりませんでした。"
                  "Yahoo側のHTML構造が変わった可能性があります。"
                  "--debug オプションで保存されるHTMLを確認し、"
                  "CONTAINER_SELECTOR / BODY_SELECTOR / AUTHOR_LINK_SELECTOR を"
                  "調整してください。", file=sys.stderr)
            break

        found_any = False
        for c in containers:
            body = c.select_one(BODY_SELECTOR)
            text = clean_text(body.get_text()) if body else ""
            if len(text) < 2:
                continue

            if account:
                author_link = c.select_one(AUTHOR_LINK_SELECTOR)
                href = author_link["href"] if author_link else ""
                is_author_match = bool(re.search(
                    rf"(?:twitter|x)\.com/{re.escape(account)}(?:[/?\"']|$)",
                    href, re.IGNORECASE))
                if not is_author_match:
                    continue

            found_any = True
            yield text
            collected += 1
            if collected >= max_results:
                break

        if not found_any:
            # これ以上結果が取れない場合は終了
            break

        page += 1
        time.sleep(sleep_sec + random.random())


# ----------------------------------------------------------------------
# Nitter検索
# ----------------------------------------------------------------------

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacyredirect.com",
]


def fetch_nitter(query: str, max_results: int = 50, sleep_sec: float = 1.5,
                  account: str = None):
    """
    Nitterの検索結果からツイートを取得するジェネレータ。
    複数インスタンスを順番に試す(稼働状況が不安定なため)。

    account を指定すると、そのアカウントのタイムライン内検索
    (/ユーザー名/search)を使い、投稿者をそのアカウントに限定する。
    query が空文字でも account だけで全投稿を取得できる。
    """
    if account:
        account = account.lstrip("@")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for instance in NITTER_INSTANCES:
        collected = 0
        cursor = ""
        print(f"[info] Nitterインスタンス試行中: {instance}", file=sys.stderr)
        while collected < max_results:
            if account:
                # ユーザーのタイムライン内検索。query未指定なら全投稿対象。
                url = f"{instance}/{account}/search"
                params = {"f": "tweets"}
                if query:
                    params["q"] = query
            else:
                url = f"{instance}/search"
                params = {"f": "tweets", "q": query}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=10)
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[warn] {instance} 失敗: {e}", file=sys.stderr)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            tweets = soup.select("div.tweet-content")
            if not tweets:
                break

            for t in tweets:
                text = clean_text(t.get_text())
                if not text:
                    continue
                yield text
                collected += 1
                if collected >= max_results:
                    break

            # 次ページカーソル取得
            next_link = soup.select_one("div.show-more a")
            if next_link and "cursor" in next_link.get("href", ""):
                cursor = re.search(r"cursor=([^&]+)", next_link["href"])
                cursor = cursor.group(1) if cursor else ""
            else:
                cursor = ""

            if not cursor:
                break

            time.sleep(sleep_sec + random.random())

        if collected > 0:
            return  # 成功したインスタンスがあれば終了
    print("[warn] すべてのNitterインスタンスで取得できませんでした。", file=sys.stderr)


# ----------------------------------------------------------------------
# メイン処理
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="日常系ツイート抽出スクリプト")
    parser.add_argument("--source", choices=["yahoo", "nitter"], required=True,
                         help="検索元: yahoo or nitter")
    parser.add_argument("--query", default="",
                         help="検索クエリ (例: '今日 学校', '通勤')。"
                              "--account 指定時は省略可(そのアカウントの全投稿が対象)")
    parser.add_argument("--account", default=None,
                         help="このアカウントの投稿のみに絞り込む (例: @example または example)")
    parser.add_argument("--max", type=int, default=50,
                         help="取得する最大件数(フィルタ前)")
    parser.add_argument("--output", default=None,
                         help="出力CSVファイル名(省略時は自動生成)")
    parser.add_argument("--keywords", default=None,
                         help="絞り込みキーワード(カンマ区切り, 例: '猫,カフェ,旅行')。"
                              "未指定時は日常系デフォルトキーワードを使用。"
                              "空文字 '' を指定するとキーワード絞り込みなし(除外語句のみ適用)")
    parser.add_argument("--exclude-keywords", default=None,
                         help="除外キーワード(カンマ区切り)。未指定時は宣伝系デフォルトを使用")
    parser.add_argument("--keywords-file", default=None,
                         help="絞り込みキーワードを1行1語で書いたテキストファイルのパス"
                              "(--keywords より優先)")
    parser.add_argument("--exclude-keywords-file", default=None,
                         help="除外キーワードを1行1語で書いたテキストファイルのパス"
                              "(--exclude-keywords より優先)")
    parser.add_argument("--debug", action="store_true",
                         help="取得した生HTMLをファイルに保存する"
                              "(セレクタ調整・トラブルシュート用)")
    args = parser.parse_args()

    # --- キーワードリストの決定 ---
    def load_keyword_list(file_arg, inline_arg, default_list):
        if file_arg:
            with open(file_arg, encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        if inline_arg is not None:
            return [w.strip() for w in inline_arg.split(",") if w.strip()]
        return default_list

    include_keywords = load_keyword_list(args.keywords_file, args.keywords, DAILY_LIFE_KEYWORDS)
    exclude_keywords = load_keyword_list(args.exclude_keywords_file, args.exclude_keywords, EXCLUDE_KEYWORDS)

    if not args.query and not args.account:
        parser.error("--query か --account の少なくとも一方を指定してください。")

    if args.source == "yahoo":
        if not args.query and args.account:
            args.query = ""  # from: 演算子だけで検索
        fetcher = fetch_yahoo_realtime(args.query, max_results=args.max, account=args.account,
                                        debug=args.debug)
    else:
        fetcher = fetch_nitter(args.query, max_results=args.max, account=args.account)

    output_file = args.output or f"daily_tweets_{datetime.now():%Y%m%d_%H%M%S}.csv"

    results = []
    for text in fetcher:
        if is_daily_life_tweet(text, include_keywords=include_keywords, exclude_keywords=exclude_keywords):
            results.append(text)

    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["text"])
        for text in results:
            writer.writerow([text])

    print(f"[done] 条件に一致したツイート {len(results)} 件を {output_file} に保存しました。")


if __name__ == "__main__":
    main()
