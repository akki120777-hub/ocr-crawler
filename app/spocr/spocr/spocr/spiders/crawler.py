import scrapy

from urllib.parse import (
    urlparse,
    urldefrag
)

import io
import os
import time
import re
import threading

from twisted.internet import threads
from twisted.internet import defer

from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):

    name = "crawler"

    # ==================================================
    # Gemini設定
    # ==================================================

    MODEL_NAME = "gemini-2.5-flash"

    # Gemini API呼び出しの最低間隔
    #
    # 5 RPM程度を想定して、かなり余裕を持たせる。
    # 変更したい場合はここを変更する。
    GEMINI_INTERVAL = 14

    # 429 / 503等の最大リトライ回数
    GEMINI_MAX_RETRIES = 3

    # リトライ時の最低待機時間
    GEMINI_DEFAULT_RETRY_WAIT = 15

    # ==================================================
    # デフォルト設定
    # ==================================================

    DEFAULT_LIMIT = 5
    DEFAULT_MAX_PAGES = 30

    # ==================================================
    # Scrapy設定
    # ==================================================

    custom_settings = {

        # ------------------------------------------------
        # 重要：
        # Gemini OCRを大量に並列実行しない
        # ------------------------------------------------

        "CONCURRENT_REQUESTS": 1,

        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,

        "CONCURRENT_ITEMS": 1,

        # ------------------------------------------------
        # リクエスト間隔
        # ------------------------------------------------

        "DOWNLOAD_DELAY": 0.2,

        # ------------------------------------------------
        # HTTPタイムアウト
        # ------------------------------------------------

        "DOWNLOAD_TIMEOUT": 30,

        # ------------------------------------------------
        # リダイレクト
        # ------------------------------------------------

        "REDIRECT_ENABLED": True,

        # ------------------------------------------------
        # User-Agent
        # ------------------------------------------------

        "USER_AGENT":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0 Safari/537.36",

        # ------------------------------------------------
        # ログ
        # ------------------------------------------------

        "LOG_LEVEL": "INFO",

        # ------------------------------------------------
        # HTTPキャッシュ無効
        # ------------------------------------------------

        "HTTPCACHE_ENABLED": False,

        # ------------------------------------------------
        # 統計
        # ------------------------------------------------

        "TELNETCONSOLE_ENABLED": False,
    }

    # ==================================================
    # 初期化
    # ==================================================

    def __init__(
        self,
        url=None,
        keyword=None,
        domain=None,
        limit=5,
        max_pages=30,
        *args,
        **kwargs
    ):

        super().__init__(
            *args,
            **kwargs
        )

        # ------------------------------------------------
        # URL
        # ------------------------------------------------

        if not url:
            raise ValueError(
                "URLを指定してください"
            )

        self.start_url = url

        self.start_urls = [
            url
        ]

        # ------------------------------------------------
        # キーワード
        # ------------------------------------------------

        self.keyword = (
            keyword.strip()
            if keyword
            else ""
        )

        # ------------------------------------------------
        # limit
        # ------------------------------------------------

        try:

            self.limit = max(
                1,
                int(limit)
            )

        except (
            ValueError,
            TypeError
        ):

            self.limit = (
                self.DEFAULT_LIMIT
            )

        # ------------------------------------------------
        # max_pages
        # ------------------------------------------------

        try:

            self.max_pages = max(
                1,
                int(max_pages)
            )

        except (
            ValueError,
            TypeError
        ):

            self.max_pages = (
                self.DEFAULT_MAX_PAGES
            )

        # ------------------------------------------------
        # カウンタ
        # ------------------------------------------------

        # 解析したHTMLページ数
        self.page_count = 0

        # OCRを開始した画像数
        self.ocr_started_count = 0

        # OCRが終了した画像数
        self.ocr_completed_count = 0

        # OCR成功数
        self.ocr_success_count = 0

        # OCRエラー数
        self.ocr_error_count = 0

        # 文字なし画像数
        self.no_text_count = 0

        # キーワード一致数
        self.result_count = 0

        # ------------------------------------------------
        # URL管理
        # ------------------------------------------------

        self.visited_pages = set()

        self.seen_images = set()

        # ------------------------------------------------
        # Gemini API制御
        # ------------------------------------------------

        self.last_gemini_call = 0.0

        self.gemini_lock = threading.Lock()

        # ------------------------------------------------
        # APIキー
        # ------------------------------------------------

        api_key = os.environ.get(
            "GEMINI_API_KEY"
        )

        if not api_key:

            raise ValueError(
                "GEMINI_API_KEY が設定されていません"
            )

        self.gemini_client = (
            client.Client(
                api_key=api_key
            )
        )

        # ------------------------------------------------
        # allowed_domains
        # ------------------------------------------------

        if domain is not None:

            if domain == "":

                self.allowed_domains = []

            else:

                self.allowed_domains = [
                    domain
                ]

        else:

            parsed = urlparse(
                url
            )

            if parsed.hostname:

                self.allowed_domains = [
                    parsed.hostname
                ]

            else:

                self.allowed_domains = []

        # ------------------------------------------------
        # 開始ログ
        # ------------------------------------------------

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            "★★★ Spider開始 ★★★"
        )

        self.logger.info(
            f"URL={self.start_url}"
        )

        self.logger.info(
            f"keyword={self.keyword}"
        )

        self.logger.info(
            f"limit={self.limit}"
        )

        self.logger.info(
            f"max_pages={self.max_pages}"
        )

        self.logger.info(
            f"domain={self.allowed_domains}"
        )

        self.logger.info(
            "Gemini OCR間隔="
            f"{self.GEMINI_INTERVAL}秒"
        )

        self.logger.info(
            "===================================="
        )

    # ==================================================
    # Spider開始
    # ==================================================

    def start_requests(self):

        normalized_url = self._normalize_url(
            self.start_url
        )

        self.visited_pages.add(
            normalized_url
        )

        yield scrapy.Request(

            self.start_url,

            callback=self.parse,

            errback=self.parse_page_error,

            meta={
                "page_url":
                    self.start_url
            }
        )

    # ==================================================
    # ページ解析
    # ==================================================

    def parse(
        self,
        response
    ):

        # ------------------------------------------------
        # limit到達確認
        # ------------------------------------------------

        if (
            self.result_count
            >= self.limit
        ):

            self.logger.info(
                "★★★ limit到達済み → ページ解析終了 ★★★"
            )

            return

        # ------------------------------------------------
        # Content-Type
        # ------------------------------------------------

        content_type = (
            response.headers
            .get(
                "Content-Type",
                b""
            )
            .decode(
                "utf-8",
                errors="ignore"
            )
            .lower()
        )

        # ------------------------------------------------
        # HTML確認
        # ------------------------------------------------

        if "text/html" not in content_type:

            self.logger.info(
                "★★★ HTMLではないためスキップ ★★★ "
                f"{response.url}"
            )

            return

        # ------------------------------------------------
        # ページ数カウント
        # ------------------------------------------------

        self.page_count += 1

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            f"★★★ ページ解析 "
            f"{self.page_count}/"
            f"{self.max_pages} ★★★"
        )

        self.logger.info(
            f"URL={response.url}"
        )

        # ------------------------------------------------
        # 画像URL取得
        # ------------------------------------------------

        image_sources = []

        # src
        image_sources.extend(
            response.css(
                "img::attr(src)"
            ).getall()
        )

        # data-src
        image_sources.extend(
            response.css(
                "img::attr(data-src)"
            ).getall()
        )

        # data-lazy-src
        image_sources.extend(
            response.css(
                "img::attr(data-lazy-src)"
            ).getall()
        )

        # data-original
        image_sources.extend(
            response.css(
                "img::attr(data-original)"
            ).getall()
        )

        # ------------------------------------------------
        # srcset
        # ------------------------------------------------

        srcsets = response.css(
            "img::attr(srcset)"
        ).getall()

        for srcset in srcsets:

            for item in srcset.split(","):

                item = item.strip()

                if not item:
                    continue

                image_url = (
                    item.split()[0]
                )

                image_sources.append(
                    image_url
                )

        # ------------------------------------------------
        # 重複除去
        # ------------------------------------------------

        unique_image_sources = []

        local_seen = set()

        for src in image_sources:

            if not src:
                continue

            src = src.strip()

            if not src:
                continue

            if src in local_seen:
                continue

            local_seen.add(src)

            unique_image_sources.append(
                src
            )

        self.logger.info(
            "★★★ ページ内画像候補数: "
            f"{len(unique_image_sources)} ★★★"
        )

        # ------------------------------------------------
        # 画像リクエスト
        # ------------------------------------------------

        for img_src in unique_image_sources:

            # limit到達
            if (
                self.result_count
                >= self.limit
            ):

                self.logger.info(
                    "★★★ limit到達 → "
                    "画像処理終了 ★★★"
                )

                break

            # data URL
            if img_src.startswith(
                "data:"
            ):

                continue

            # URL化
            img_url = response.urljoin(
                img_src
            )

            # フラグメント除去
            img_url = urldefrag(
                img_url
            ).url

            # ------------------------------------------------
            # SVG除外
            # ------------------------------------------------

            clean_url = (
                img_url
                .lower()
                .split("?")[0]
            )

            if clean_url.endswith(
                ".svg"
            ):

                self.logger.info(
                    "★★★ SVG画像をスキップ ★★★ "
                    f"{img_url}"
                )

                continue

            # ------------------------------------------------
            # 画像重複除外
            # ------------------------------------------------

            if img_url in self.seen_images:

                self.logger.info(
                    "★★★ 画像重複 → スキップ ★★★ "
                    f"{img_url}"
                )

                continue

            self.seen_images.add(
                img_url
            )

            self.logger.info(
                "★★★ 画像発見 ★★★ "
                f"{img_url}"
            )

            yield scrapy.Request(

                img_url,

                callback=self.parse_image,

                errback=self.parse_image_error,

                priority=10,

                meta={
                    "img_url":
                        img_url,

                    "page_url":
                        response.url,

                    "handle_httpstatus_all":
                        True
                },

                dont_filter=True
            )

        # ------------------------------------------------
        # ページ上限
        # ------------------------------------------------

        if (
            self.page_count
            >= self.max_pages
        ):

            self.logger.info(
                "★★★ 最大ページ数に到達 ★★★"
            )

            self.crawler.engine.close_spider(
                self,
                reason="max_pages_reached"
            )

            return

        # ------------------------------------------------
        # 次ページ候補
        # ------------------------------------------------

        links = response.css(
            "a::attr(href)"
        ).getall()

        self.logger.info(
            "★★★ ページ内リンク数: "
            f"{len(links)} ★★★"
        )

        next_page_found = False

        for href in links:

            if (
                self.result_count
                >= self.limit
            ):

                break

            if not href:
                continue

            parsed_href = urlparse(
                href
            )

            # tel/mailto等
            if parsed_href.scheme in (
                "tel",
                "mailto",
                "javascript"
            ):

                continue

            if parsed_href.scheme not in (
                "",
                "http",
                "https"
            ):

                continue

            next_url = response.urljoin(
                href
            )

            next_url = urldefrag(
                next_url
            ).url

            # ------------------------------------------------
            # ページURLとして不適切なもの
            # ------------------------------------------------

            lower_url = (
                next_url.lower()
            )

            # 画像・PDF等はページ巡回しない
            excluded_extensions = (
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".svg",
                ".bmp",
                ".ico",
                ".pdf",
                ".zip",
                ".mp4",
                ".mp3"
            )

            if lower_url.endswith(
                excluded_extensions
            ):

                continue

            # ------------------------------------------------
            # 同一URLチェック
            # ------------------------------------------------

            if next_url in self.visited_pages:

                continue

            self.visited_pages.add(
                next_url
            )

            self.logger.info(
                "★★★ 次のページ候補 ★★★ "
                f"{next_url}"
            )

            # ------------------------------------------------
            # ページリクエスト
            #
            # priorityを低くする。
            #
            # 画像(priority=10)を先に処理してから
            # 次のページ(priority=0)へ進む。
            # ------------------------------------------------

            yield scrapy.Request(

                next_url,

                callback=self.parse,

                errback=self.parse_page_error,

                priority=0,

                meta={
                    "page_url":
                        next_url
                }
            )

            next_page_found = True

            # ------------------------------------------------
            # 重要
            #
            # 一度に大量のページをキューへ入れない。
            # 1ページにつき次ページ候補は1件だけ。
            # ------------------------------------------------

            break

        if not next_page_found:

            self.logger.info(
                "★★★ 次に巡回するページがありません ★★★"
            )

    # ==================================================
    # ページエラー
    # ==================================================

    def parse_page_error(
        self,
        failure
    ):

        request = failure.request

        self.logger.warning(
            "★★★ ページ取得エラー ★★★ "
            f"{request.url}"
        )

        self.logger.warning(
            f"{failure.value}"
        )

    # ==================================================
    # 画像取得エラー
    # ==================================================

    def parse_image_error(
        self,
        failure
    ):

        request = failure.request

        img_url = request.meta.get(
            "img_url",
            request.url
        )

        self.logger.warning(
            "★★★ 画像取得エラー ★★★ "
            f"{img_url}"
        )

        self.logger.warning(
            f"{failure.value}"
        )

        # エラーでSpider全体を止めない
        return

    # ==================================================
    # Gemini OCR
    # ==================================================

    def _ocr_bytes(
        self,
        img_bytes
    ):

        # ------------------------------------------------
        # Gemini APIを完全に直列化
        # ------------------------------------------------

        with self.gemini_lock:

            # ==================================================
            # 画像チェック
            # ==================================================

            try:

                image = Image.open(
                    io.BytesIO(img_bytes)
                )

                image.load()

            except Exception as e:

                self.logger.warning(
                    "画像読み込みエラー: "
                    f"{e}"
                )

                return (
                    f"ERROR_IMAGE:{e}"
                )

            # ==================================================
            # OCRプロンプト
            # ==================================================

            prompt_text = """
この画像に含まれる日本語の文字をOCRしてください。

重要：

・画像に日本語の文字がある場合は、
  実際に画像に書かれている文字だけを返してください。

・画像に日本語の文字がない場合は、
  NO_TEXT
  とだけ返してください。

・画像の内容について説明しないでください。

・「この画像には日本語テキストがありません」
  などの説明文を返さないでください。

・推測で文字を作らないでください。

・Markdown、箇条書き、JSON、前置きは不要です。
""".strip()

            # ==================================================
            # Gemini API
            # ==================================================

            for attempt in range(
                self.GEMINI_MAX_RETRIES + 1
            ):

                try:

                    # ------------------------------------------------
                    # API呼び出し間隔
                    # ------------------------------------------------

                    now = time.monotonic()

                    elapsed = (
                        now
                        - self.last_gemini_call
                    )

                    if (
                        elapsed
                        < self.GEMINI_INTERVAL
                    ):

                        wait_time = (
                            self.GEMINI_INTERVAL
                            - elapsed
                        )

                        self.logger.info(
                            "★★★ Gemini API間隔調整 ★★★ "
                            f"{wait_time:.1f}秒待機"
                        )

                        time.sleep(
                            wait_time
                        )

                    # ------------------------------------------------
                    # 呼び出し
                    # ------------------------------------------------

                    self.logger.info(
                        "★★★ Gemini API呼び出し ★★★ "
                        f"attempt={attempt + 1}/"
                        f"{self.GEMINI_MAX_RETRIES + 1}"
                    )

                    self.last_gemini_call = (
                        time.monotonic()
                    )

                    response = (
                        self.gemini_client
                        .models
                        .generate_content(

                            model=self.MODEL_NAME,

                            contents=[
                                prompt_text,
                                image
                            ]
                        )
                    )

                    self.logger.info(
                        "★★★ Gemini API応答受信 ★★★"
                    )

                    # ------------------------------------------------
                    # 応答確認
                    # ------------------------------------------------

                    if (
                        response
                        and response.text
                    ):

                        text = (
                            response.text
                            .strip()
                        )

                        if not text:

                            return "NO_TEXT"

                        if (
                            text.upper()
                            == "NO_TEXT"
                        ):

                            return "NO_TEXT"

                        if self._looks_like_no_text(
                            text
                        ):

                            return "NO_TEXT"

                        return text

                    return "NO_TEXT"

                # ==================================================
                # APIエラー
                # ==================================================

                except Exception as e:

                    error_text = str(e)

                    self.logger.error(
                        "★★★ Gemini APIエラー ★★★ "
                        f"{error_text}"
                    )

                    # ------------------------------------------------
                    # リトライ可能か
                    # ------------------------------------------------

                    retryable = (
                        self._is_retryable_error(
                            error_text
                        )
                    )

                    if not retryable:

                        self.logger.error(
                            "★★★ リトライ対象外のGeminiエラー ★★★"
                        )

                        return (
                            "ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ------------------------------------------------
                    # 最終試行
                    # ------------------------------------------------

                    if (
                        attempt
                        >= self.GEMINI_MAX_RETRIES
                    ):

                        self.logger.error(
                            "★★★ Geminiリトライ上限到達 ★★★"
                        )

                        return (
                            "ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ------------------------------------------------
                    # 待機時間
                    # ------------------------------------------------

                    wait_time = (
                        self._extract_retry_seconds(
                            error_text
                        )
                    )

                    if wait_time is None:

                        # 試行回数に応じて増加
                        wait_time = max(
                            self.GEMINI_DEFAULT_RETRY_WAIT,
                            15 * (
                                attempt + 1
                            )
                        )

                    # 少し余裕
                    wait_time += 2

                    self.logger.warning(
                        "★★★ Gemini一時エラー ★★★ "
                        f"{wait_time:.1f}秒後に再試行"
                    )

                    time.sleep(
                        wait_time
                    )

            return (
                "ERROR_GEMINI:"
                "Unknown error"
            )

    # ==================================================
    # リトライ可能エラー判定
    # ==================================================

    def _is_retryable_error(
        self,
        error_text
    ):

        retry_keywords = [

            "429",
            "RESOURCE_EXHAUSTED",

            "500",
            "INTERNAL",

            "502",
            "BAD_GATEWAY",

            "503",
            "UNAVAILABLE",

            "504",
            "DEADLINE_EXCEEDED",

            "temporarily unavailable",
            "temporary error",
            "high demand"
        ]

        lower_text = (
            error_text.lower()
        )

        for keyword in retry_keywords:

            if keyword.lower() in lower_text:

                return True

        return False

    # ==================================================
    # 「文字なし」判定
    # ==================================================

    def _looks_like_no_text(
        self,
        text
    ):

        normalized = (
            text
            .replace(" ", "")
            .replace("　", "")
            .replace("\n", "")
            .replace("\r", "")
        )

        no_text_patterns = [

            "日本語テキストは含まれていません",
            "日本語テキストが含まれていません",
            "日本語のテキストは含まれていません",
            "日本語の文字は含まれていません",
            "日本語文字は含まれていません",

            "日本語テキストがありません",
            "日本語テキストはありません",

            "日本語の文字がありません",
            "日本語の文字はありません",

            "文字は含まれていません",
            "文字が含まれていません",

            "テキストは含まれていません",
            "テキストが含まれていません",

            "テキストはありません",
            "文字はありません"
        ]

        for pattern in no_text_patterns:

            if pattern in normalized:

                return True

        return False

    # ==================================================
    # Retry-After / retry in 秒数抽出
    # ==================================================

    def _extract_retry_seconds(
        self,
        error_text
    ):

        patterns = [

            r"retry in ([0-9.]+)s",

            r"retryDelay.*?([0-9.]+)s",

            r"Retry-After.*?([0-9.]+)"
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                error_text,
                re.IGNORECASE
            )

            if match:

                try:

                    return float(
                        match.group(1)
                    )

                except (
                    ValueError,
                    TypeError
                ):

                    pass

        return None

    # ==================================================
    # 画像OCR
    # ==================================================

    @defer.inlineCallbacks
    def parse_image(
        self,
        response
    ):

        img_url = response.meta.get(
            "img_url",
            response.url
        )

        page_url = response.meta.get(
            "page_url",
            ""
        )

        # ==================================================
        # HTTPステータス
        # ==================================================

        status = response.status

        self.logger.info(
            "★★★ 画像取得 ★★★ "
            f"status={status} "
            f"{img_url}"
        )

        # ------------------------------------------------
        # Content-Type
        # ------------------------------------------------

        content_type = (
            response.headers
            .get(
                "Content-Type",
                b""
            )
            .decode(
                "utf-8",
                errors="ignore"
            )
            .lower()
        )

        self.logger.info(
            "★★★ 画像Content-Type ★★★ "
            f"{content_type}"
        )

        # ------------------------------------------------
        # HTTPエラー
        # ------------------------------------------------

        if status >= 400:

            self.logger.warning(
                "★★★ 画像HTTPエラー → スキップ ★★★ "
                f"status={status} "
                f"{img_url}"
            )

            return

        # ------------------------------------------------
        # 画像ではない
        # ------------------------------------------------

        if not content_type.startswith(
            "image/"
        ):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★ "
                f"{img_url}"
            )

            return

        # ------------------------------------------------
        # SVG
        # ------------------------------------------------

        if "svg" in content_type:

            self.logger.info(
                "★★★ SVGのためスキップ ★★★ "
                f"{img_url}"
            )

            return

        # ==================================================
        # limit確認
        # ==================================================

        if (
            self.result_count
            >= self.limit
        ):

            self.logger.info(
                "★★★ limit到達済み → OCRしない ★★★"
            )

            return

        # ==================================================
        # OCR開始
        # ==================================================

        self.ocr_started_count += 1

        self.logger.info(
            "★★★ Gemini OCR開始 ★★★ "
            f"{img_url}"
        )

        try:

            text = yield threads.deferToThread(
                self._ocr_bytes,
                response.body
            )

        except Exception as e:

            self.ocr_error_count += 1

            self.logger.error(
                "★★★ OCR処理例外 ★★★ "
                f"{img_url}"
            )

            self.logger.error(
                str(e)
            )

            return

        # ==================================================
        # OCR完了
        # ==================================================

        self.ocr_completed_count += 1

        self.logger.info(
            "★★★ Gemini OCR完了 ★★★ "
            f"{img_url}"
        )

        # ==================================================
        # 画像エラー
        # ==================================================

        if text.startswith(
            "ERROR_IMAGE:"
        ):

            self.ocr_error_count += 1

            self.logger.warning(
                "★★★ 画像読み込みエラー ★★★ "
                f"{text}"
            )

            return

        # ==================================================
        # Geminiエラー
        # ==================================================

        if text.startswith(
            "ERROR_GEMINI:"
        ):

            self.ocr_error_count += 1

            self.logger.error(
                "★★★ Gemini OCRエラー ★★★ "
                f"{text}"
            )

            return

        # ==================================================
        # NO_TEXT
        # ==================================================

        if text == "NO_TEXT":

            self.no_text_count += 1

            self.logger.info(
                "★★★ 日本語テキストなし ★★★ "
                f"{img_url}"
            )

            return

        # ==================================================
        # OCR成功
        # ==================================================

        self.ocr_success_count += 1

        self.logger.info(
            "★★★ OCR結果 ★★★ "
            f"{text[:300]}"
        )

        # ==================================================
        # limit確認
        # ==================================================

        if (
            self.result_count
            >= self.limit
        ):

            self.logger.info(
                "★★★ 別処理ですでにlimit到達 ★★★"
            )

            return

        # ==================================================
        # キーワード検索
        # ==================================================

        if self.keyword:

            if self.keyword not in text:

                self.logger.info(
                    "★★★ キーワード不一致 ★★★ "
                    f"「{self.keyword}」"
                )

                return

            # ------------------------------------------------
            # 一致
            # ------------------------------------------------

            self.result_count += 1

            self.logger.info(
                "★★★ キーワード一致 ★★★ "
                f"「{self.keyword}」 "
                f"→ {img_url}"
            )

        else:

            # ------------------------------------------------
            # キーワードなし
            # ------------------------------------------------

            self.result_count += 1

            self.logger.info(
                "★★★ キーワードなし "
                "→ OCR結果を返す ★★★"
            )

        # ==================================================
        # 結果
        # ==================================================

        item = {

            "url":
                img_url,

            "text":
                text,

            "page_url":
                page_url
        }

        # ==================================================
        # 結果を返す
        # ==================================================

        yield item

        # ==================================================
        # limit到達
        # ==================================================

        if (
            self.result_count
            >= self.limit
        ):

            self.logger.info(
                "===================================="
            )

            self.logger.info(
                "★★★ limit到達 ★★★"
            )

            self.logger.info(
                f"結果={self.result_count}/"
                f"{self.limit}"
            )

            self.logger.info(
                "★★★ Spider終了要求 ★★★"
            )

            self.crawler.engine.close_spider(
                self,
                reason="result_limit_reached"
            )

    # ==================================================
    # Spider終了時の統計
    # ==================================================

    def closed(
        self,
        reason
    ):

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            "★★★ Spider終了 ★★★"
        )

        self.logger.info(
            f"終了理由={reason}"
        )

        self.logger.info(
            f"ページ解析数={self.page_count}"
        )

        self.logger.info(
            f"OCR開始={self.ocr_started_count}"
        )

        self.logger.info(
            f"OCR完了={self.ocr_completed_count}"
        )

        self.logger.info(
            f"OCR成功={self.ocr_success_count}"
        )

        self.logger.info(
            f"OCRエラー={self.ocr_error_count}"
        )

        self.logger.info(
            f"文字なし={self.no_text_count}"
        )

        self.logger.info(
            f"検索結果={self.result_count}/"
            f"{self.limit}"
        )

        self.logger.info(
            "===================================="
        )
        
        
