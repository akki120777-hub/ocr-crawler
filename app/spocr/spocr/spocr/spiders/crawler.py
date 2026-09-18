import asyncio
import io
import os
import re
import time
from urllib.parse import urljoin, urlparse

import scrapy
from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    # ==========================================================
    # 基本設定
    # ==========================================================

    MODEL_NAME = "gemini-2.5-flash"

    # Gemini Free Tier のRPM対策
    # 14秒間隔 → 約4.3回/分
    GEMINI_INTERVAL = float(
        os.environ.get("GEMINI_INTERVAL", "14")
    )

    # Gemini API最大リトライ回数
    GEMINI_MAX_RETRIES = 3

    # Geminiエラー時の基本待機時間
    DEFAULT_RETRY_SECONDS = 45

    # デフォルトページ数
    DEFAULT_MAX_PAGES = 30

    custom_settings = {
        # ------------------------------------------------------
        # Scrapy通信
        # ------------------------------------------------------

        # 通常のWebページ取得は1つずつ
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,

        # Item処理も1つずつ
        "CONCURRENT_ITEMS": 1,

        "DOWNLOAD_DELAY": 0.2,

        "DOWNLOAD_TIMEOUT": 30,

        "REDIRECT_ENABLED": True,

        # ------------------------------------------------------
        # 不要なScrapy Telnet Consoleを無効化
        # ------------------------------------------------------

        "TELNETCONSOLE_ENABLED": False,

        # ------------------------------------------------------
        # ログ
        # ------------------------------------------------------

        "LOG_LEVEL": "INFO",

        # HTTPキャッシュは使用しない
        "HTTPCACHE_ENABLED": False,

        # 深さ制限
        "DEPTH_LIMIT": 100,

        # Chrome風User-Agent
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
    }

    # ==========================================================
    # 初期化
    # ==========================================================

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
        super().__init__(*args, **kwargs)

        if not url:
            raise ValueError("URLを指定してください")

        # ------------------------------------------------------
        # URL
        # ------------------------------------------------------

        parsed_url = urlparse(url)

        if not parsed_url.scheme or not parsed_url.netloc:
            raise ValueError(
                "正しいURLを指定してください"
            )

        self.start_urls = [url]

        # ------------------------------------------------------
        # キーワード
        # ------------------------------------------------------

        self.keyword = (keyword or "").strip()

        # ------------------------------------------------------
        # 結果件数
        # ------------------------------------------------------

        try:
            self.limit = max(1, int(limit))
        except (ValueError, TypeError):
            self.limit = 5

        # ------------------------------------------------------
        # ページ数
        # ------------------------------------------------------

        try:
            self.max_pages = max(
                1,
                int(max_pages)
            )
        except (ValueError, TypeError):
            self.max_pages = self.DEFAULT_MAX_PAGES

        # ------------------------------------------------------
        # カウンター
        # ------------------------------------------------------

        self.result_count = 0

        self.page_count = 0

        self.image_found_count = 0

        self.image_downloaded_count = 0

        self.ocr_started_count = 0

        self.ocr_completed_count = 0

        self.ocr_result_count = 0

        # ------------------------------------------------------
        # ページ管理
        # ------------------------------------------------------

        self.visited_pages = set()

        # 画像重複防止
        self.seen_images = set()

        # ------------------------------------------------------
        # ドメイン制限
        # ------------------------------------------------------

        if domain is not None:

            domain = domain.strip()

            if domain:
                self.allowed_domains = [
                    domain
                ]
            else:
                self.allowed_domains = []

        else:

            hostname = parsed_url.hostname

            if hostname:
                self.allowed_domains = [
                    hostname
                ]
            else:
                self.allowed_domains = []

        # ------------------------------------------------------
        # Gemini API
        # ------------------------------------------------------

        api_key = os.environ.get(
            "GEMINI_API_KEY"
        )

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY が設定されていません"
            )

        self.gemini_client = client.Client(
            api_key=api_key
        )

        # ------------------------------------------------------
        # Gemini API呼び出し間隔管理
        # ------------------------------------------------------

        self.last_gemini_call = 0.0

        # ------------------------------------------------------
        # ログ
        # ------------------------------------------------------

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            "★★★ Spider開始 ★★★"
        )

        self.logger.info(
            f"URL={url}"
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
            f"Gemini OCR間隔="
            f"{self.GEMINI_INTERVAL}秒"
        )

        self.logger.info(
            "★★★ OCRは1画像ずつ逐次処理 ★★★"
        )

        self.logger.info(
            "===================================="
        )

    # ==========================================================
    # 開始
    # ==========================================================

    def start_requests(self):

        url = self.start_urls[0]

        self.visited_pages.add(
            self.normalize_url(url)
        )

        yield scrapy.Request(
            url=url,
            callback=self.parse,
            errback=self.parse_page_error,
            dont_filter=True,
        )

    # ==========================================================
    # ページ解析
    # ==========================================================

    def parse(self, response):

        # ------------------------------------------------------
        # limit到達
        # ------------------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み → ページ解析終了 ★★★"
            )

            return

        # ------------------------------------------------------
        # ページ数
        # ------------------------------------------------------

        if self.page_count >= self.max_pages:

            self.logger.info(
                "★★★ max_pages到達 → ページ解析終了 ★★★"
            )

            return

        self.page_count += 1

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            f"★★★ ページ解析 "
            f"{self.page_count}/{self.max_pages} ★★★"
        )

        self.logger.info(
            f"URL={response.url}"
        )

        # ------------------------------------------------------
        # Content-Type
        # ------------------------------------------------------

        content_type = response.headers.get(
            "Content-Type",
            b""
        ).decode(
            "utf-8",
            errors="ignore"
        ).lower()

        self.logger.info(
            f"Content-Type={content_type}"
        )

        if "text/html" not in content_type:

            self.logger.info(
                "★★★ HTMLではないためスキップ ★★★"
            )

            return

        # ------------------------------------------------------
        # ページ内画像URLを取得
        # ------------------------------------------------------

        image_urls = self.extract_image_urls(
            response
        )

        self.logger.info(
            f"★★★ ページ内画像候補: "
            f"{len(image_urls)}件 ★★★"
        )

        # ------------------------------------------------------
        # 画像を1枚ずつ処理
        #
        # ★重要★
        #
        # ここでは大量の画像Requestをyieldしない。
        #
        # 最初の1枚だけRequest。
        #
        # その画像のOCR完了後に、
        # parse_image()から次の画像Requestを出す。
        # ------------------------------------------------------

        if image_urls:

            first_image = image_urls[0]

            remaining_images = image_urls[1:]

            self.logger.info(
                "★★★ 画像逐次処理開始 ★★★"
            )

            yield scrapy.Request(
                url=first_image,
                callback=self.parse_image,
                errback=self.parse_image_error,
                meta={
                    "image_queue": remaining_images,
                    "source_page": response.url,
                },
                dont_filter=True,
            )

        else:

            # --------------------------------------------------
            # 画像がなかった場合
            # --------------------------------------------------

            yield from self.next_page_request(
                response
            )

    # ==========================================================
    # 画像解析
    # ==========================================================

    async def parse_image(self, response):

        image_url = response.url

        # ------------------------------------------------------
        # limit確認
        # ------------------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達 → OCRしない ★★★"
            )

            return

        # ------------------------------------------------------
        # HTTPステータス
        # ------------------------------------------------------

        if response.status != 200:

            self.logger.warning(
                "★★★ 画像HTTPエラー ★★★ "
                f"status={response.status} "
                f"{image_url}"
            )

            await self.process_next_image(
                response
            )

            return

        # ------------------------------------------------------
        # Content-Type
        # ------------------------------------------------------

        content_type = response.headers.get(
            "Content-Type",
            b""
        ).decode(
            "utf-8",
            errors="ignore"
        ).lower()

        self.logger.info(
            "★★★ 画像取得 ★★★ "
            f"status={response.status} "
            f"{image_url}"
        )

        self.logger.info(
            "★★★ 画像Content-Type ★★★ "
            f"{content_type}"
        )

        # ------------------------------------------------------
        # 画像ではない
        # ------------------------------------------------------

        if not content_type.startswith("image/"):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★ "
                f"{image_url}"
            )

            await self.process_next_image(
                response
            )

            return

        # ------------------------------------------------------
        # SVG
        # ------------------------------------------------------

        if "svg" in content_type:

            self.logger.info(
                "★★★ SVGのためスキップ ★★★ "
                f"{image_url}"
            )

            await self.process_next_image(
                response
            )

            return

        self.image_downloaded_count += 1

        # ------------------------------------------------------
        # OCR開始
        # ------------------------------------------------------

        self.ocr_started_count += 1

        self.logger.info(
            "★★★ Gemini OCR開始 ★★★ "
            f"{image_url}"
        )

        try:

            # ==================================================
            # ★ここが重要★
            #
            # OCRが完全に終わるまで、
            # この関数は次へ進まない。
            # ==================================================

            text = await asyncio.to_thread(
                self._ocr_bytes,
                response.body
            )

        except Exception as e:

            self.logger.exception(
                "★★★ OCR処理中に予期しないエラー ★★★"
            )

            text = (
                "ERROR_GEMINI:"
                f"{type(e).__name__}: {e}"
            )

        # ------------------------------------------------------
        # OCR完了
        # ------------------------------------------------------

        self.ocr_completed_count += 1

        self.logger.info(
            "★★★ Gemini OCR完了 ★★★ "
            f"{image_url}"
        )

        # ------------------------------------------------------
        # OCRエラー
        # ------------------------------------------------------

        if isinstance(text, str) and text.startswith(
            "ERROR_IMAGE:"
        ):

            self.logger.warning(
                f"画像読み込みエラー: {text}"
            )

            await self.process_next_image(
                response
            )

            return

        if isinstance(text, str) and text.startswith(
            "ERROR_GEMINI:"
        ):

            self.logger.error(
                f"Gemini APIエラー: {text}"
            )

            await self.process_next_image(
                response
            )

            return

        # ------------------------------------------------------
        # NO_TEXT
        # ------------------------------------------------------

        if text == "NO_TEXT":

            self.logger.info(
                "★★★ 日本語テキストなし ★★★ "
                f"{image_url}"
            )

            await self.process_next_image(
                response
            )

            return

        # ------------------------------------------------------
        # OCR結果
        # ------------------------------------------------------

        self.ocr_result_count += 1

        self.logger.info(
            "★★★ OCR結果 ★★★ "
            f"{text[:300]}"
        )

        # ------------------------------------------------------
        # limit再確認
        # ------------------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み "
                "→ 結果追加なし ★★★"
            )

            return

        # ------------------------------------------------------
        # キーワード検索
        # ------------------------------------------------------

        if self.keyword:

            if self.keyword in text:

                self.result_count += 1

                self.logger.info(
                    "★★★ キーワード一致 ★★★ "
                    f"「{self.keyword}」 "
                    f"→ {image_url}"
                )

                self.logger.info(
                    "★★★ 現在の結果件数 ★★★ "
                    f"{self.result_count}/"
                    f"{self.limit}"
                )

                yield {
                    "url": image_url,
                    "text": text,
                }

                # --------------------------------------------------
                # limit到達
                # --------------------------------------------------

                if self.result_count >= self.limit:

                    self.logger.info(
                        "★★★ limit到達 ★★★"
                    )

                    return

        else:

            # --------------------------------------------------
            # keywordが空の場合
            #
            # OCRテキストがある画像を結果とする
            # --------------------------------------------------

            self.result_count += 1

            self.logger.info(
                "★★★ キーワード未指定 "
                "→ OCR結果を採用 ★★★"
            )

            yield {
                "url": image_url,
                "text": text,
            }

            if self.result_count >= self.limit:

                return

        # ------------------------------------------------------
        # ★OCRが完全終了してから次の画像へ
        # ------------------------------------------------------

        await self.process_next_image(
            response
        )

    # ==========================================================
    # 次の画像を処理
    # ==========================================================

    async def process_next_image(self, response):

        # ------------------------------------------------------
        # limit到達
        # ------------------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達 → 次画像なし ★★★"
            )

            return

        # ------------------------------------------------------
        # 残り画像
        # ------------------------------------------------------

        queue = response.meta.get(
            "image_queue",
            []
        )

        source_page = response.meta.get(
            "source_page",
            ""
        )

        # ------------------------------------------------------
        # 次の画像がある
        # ------------------------------------------------------

        if queue:

            next_image = queue[0]

            remaining = queue[1:]

            self.logger.info(
                "★★★ 次の画像へ ★★★ "
                f"{next_image}"
            )

            yield scrapy.Request(
                url=next_image,
                callback=self.parse_image,
                errback=self.parse_image_error,
                meta={
                    "image_queue": remaining,
                    "source_page": source_page,
                },
                dont_filter=True,
            )

            return

        # ------------------------------------------------------
        # ページ内画像が終了
        # ------------------------------------------------------

        self.logger.info(
            "★★★ このページの画像OCR終了 ★★★"
        )

        # ------------------------------------------------------
        # 次ページへ
        # ------------------------------------------------------

        fake_response = response

        for request in self.next_page_request(
            fake_response
        ):

            yield request

    # ==========================================================
    # 次のページ
    # ==========================================================

    def next_page_request(self, response):

        if self.result_count >= self.limit:

            return

        if self.page_count >= self.max_pages:

            self.logger.info(
                "★★★ max_pages到達 ★★★"
            )

            return

        links = response.css(
            "a::attr(href)"
        ).getall()

        self.logger.info(
            f"★★★ ページ内リンク数: "
            f"{len(links)} ★★★"
        )

        for href in links:

            next_url = urljoin(
                response.url,
                href
            )

            next_url = self.normalize_url(
                next_url
            )

            # --------------------------------------------------
            # HTTP以外
            # --------------------------------------------------

            if not next_url.startswith(
                ("http://", "https://")
            ):
                continue

            parsed = urlparse(next_url)

            # --------------------------------------------------
            # 同一ドメイン以外
            # --------------------------------------------------

            if (
                self.allowed_domains
                and parsed.hostname
                not in self.allowed_domains
            ):
                continue

            # --------------------------------------------------
            # 既訪問
            # --------------------------------------------------

            if next_url in self.visited_pages:

                continue

            # --------------------------------------------------
            # PDF等
            # --------------------------------------------------

            lower_url = next_url.lower()

            if lower_url.endswith(
                (
                    ".pdf",
                    ".zip",
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".gif",
                    ".webp",
                    ".svg",
                )
            ):
                continue

            # --------------------------------------------------
            # 新しいページ
            # --------------------------------------------------

            self.visited_pages.add(
                next_url
            )

            self.logger.info(
                "★★★ 次のページ候補 ★★★ "
                f"{next_url}"
            )

            yield scrapy.Request(
                url=next_url,
                callback=self.parse,
                errback=self.parse_page_error,
            )

            # --------------------------------------------------
            # 重要:
            #
            # ページも一度に大量投入しない。
            # --------------------------------------------------

            return

        self.logger.info(
            "★★★ 次に進めるページがありません ★★★"
        )

    # ==========================================================
    # 画像URL抽出
    # ==========================================================

    def extract_image_urls(self, response):

        candidates = []

        # ------------------------------------------------------
        # 通常のsrc
        # ------------------------------------------------------

        candidates.extend(
            response.css(
                "img::attr(src)"
            ).getall()
        )

        # ------------------------------------------------------
        # lazy load
        # ------------------------------------------------------

        for attr in [
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-image",
        ]:

            candidates.extend(
                response.css(
                    f"img::attr({attr})"
                ).getall()
            )

        # ------------------------------------------------------
        # srcset
        # ------------------------------------------------------

        srcsets = response.css(
            "img::attr(srcset)"
        ).getall()

        for srcset in srcsets:

            for item in srcset.split(","):

                item = item.strip()

                if not item:
                    continue

                parts = item.split()

                if parts:
                    candidates.append(
                        parts[0]
                    )

        # ------------------------------------------------------
        # URL変換・重複除去
        # ------------------------------------------------------

        results = []

        local_seen = set()

        for src in candidates:

            if not src:
                continue

            src = src.strip()

            if not src:
                continue

            if src.startswith(
                (
                    "data:",
                    "javascript:",
                    "#"
                )
            ):
                continue

            image_url = urljoin(
                response.url,
                src
            )

            image_url = self.normalize_url(
                image_url
            )

            parsed = urlparse(
                image_url
            )

            if parsed.scheme not in (
                "http",
                "https"
            ):
                continue

            # --------------------------------------------------
            # SVG除外
            # --------------------------------------------------

            if image_url.lower().split("?")[0].endswith(
                ".svg"
            ):
                continue

            if image_url in local_seen:
                continue

            local_seen.add(
                image_url
            )

            # Spider全体でも重複除去
            if image_url in self.seen_images:
                continue

            self.seen_images.add(
                image_url
            )

            self.image_found_count += 1

            self.logger.info(
                "★★★ 画像発見 ★★★ "
                f"{image_url}"
            )

            results.append(
                image_url
            )

        return results

    # ==========================================================
    # 画像取得エラー
    # ==========================================================

    async def parse_image_error(
        self,
        failure
    ):

        request = failure.request

        image_url = request.url

        self.logger.warning(
            "★★★ 画像取得失敗 ★★★ "
            f"{image_url}"
        )

        queue = request.meta.get(
            "image_queue",
            []
        )

        source_page = request.meta.get(
            "source_page",
            ""
        )

        # ------------------------------------------------------
        # 次の画像へ
        # ------------------------------------------------------

        if (
            self.result_count < self.limit
            and queue
        ):

            next_image = queue[0]

            remaining = queue[1:]

            self.logger.info(
                "★★★ 取得失敗 → 次の画像へ ★★★ "
                f"{next_image}"
            )

            yield scrapy.Request(
                url=next_image,
                callback=self.parse_image,
                errback=self.parse_image_error,
                meta={
                    "image_queue": remaining,
                    "source_page": source_page,
                },
                dont_filter=True,
            )

            return

        # ------------------------------------------------------
        # 画像終了 → 次ページ
        # ------------------------------------------------------

        if source_page:

            for req in self.next_page_request_by_url(
                source_page
            ):

                yield req

    # ==========================================================
    # 画像取得失敗時の次ページ処理
    # ==========================================================

    def next_page_request_by_url(
        self,
        source_page
    ):

        # ------------------------------------------------------
        # ここでは元ページのHTMLを再取得しない。
        #
        # 画像取得失敗時は、ページを終了させる。
        # 次ページ探索はCrawlerのページキューに任せる。
        # ------------------------------------------------------

        self.logger.info(
            "★★★ 画像処理終了 ★★★ "
            f"source={source_page}"
        )

        return []

    # ==========================================================
    # ページ取得エラー
    # ==========================================================

    def parse_page_error(
        self,
        failure
    ):

        self.logger.warning(
            "★★★ ページ取得失敗 ★★★ "
            f"{failure.request.url}"
        )

    # ==========================================================
    # Gemini OCR
    # ==========================================================

    def _ocr_bytes(
        self,
        image_bytes
    ):

        # ------------------------------------------------------
        # PILで画像確認
        # ------------------------------------------------------

        try:

            image = Image.open(
                io.BytesIO(image_bytes)
            )

            image.load()

        except Exception as e:

            return (
                "ERROR_IMAGE:"
                f"{type(e).__name__}: {e}"
            )

        # ------------------------------------------------------
        # OCRプロンプト
        # ------------------------------------------------------

        prompt = """
この画像に含まれている文字を読み取ってください。

日本語が含まれている場合は、日本語をできるだけ正確に
そのまま文字起こししてください。

文字が存在しない場合は、
NO_TEXT
とだけ返してください。

画像内の文字だけを対象にしてください。
説明や感想は不要です。
""".strip()

        # ------------------------------------------------------
        # Gemini API
        # ------------------------------------------------------

        for attempt in range(
            1,
            self.GEMINI_MAX_RETRIES + 2
        ):

            try:

                # ==============================================
                # RPM対策
                # ==============================================

                elapsed = (
                    time.monotonic()
                    - self.last_gemini_call
                )

                wait_seconds = (
                    self.GEMINI_INTERVAL
                    - elapsed
                )

                if wait_seconds > 0:

                    self.logger.info(
                        "★★★ Gemini API待機 ★★★ "
                        f"{wait_seconds:.1f}秒"
                    )

                    time.sleep(
                        wait_seconds
                    )

                self.last_gemini_call = (
                    time.monotonic()
                )

                self.logger.info(
                    "★★★ Gemini API呼び出し ★★★ "
                    f"attempt={attempt}/"
                    f"{self.GEMINI_MAX_RETRIES + 1}"
                )

                response = (
                    self.gemini_client.models.generate_content(
                        model=self.MODEL_NAME,
                        contents=[
                            prompt,
                            image,
                        ],
                    )
                )

                self.logger.info(
                    "★★★ Gemini API応答受信 ★★★"
                )

                # --------------------------------------------------
                # テキスト取得
                # --------------------------------------------------

                text = getattr(
                    response,
                    "text",
                    None
                )

                if text is None:

                    return "NO_TEXT"

                text = str(text).strip()

                if not text:

                    return "NO_TEXT"

                # --------------------------------------------------
                # NO_TEXT
                # --------------------------------------------------

                if self._looks_like_no_text(
                    text
                ):

                    return "NO_TEXT"

                return text

            except Exception as e:

                error_text = str(e)

                error_lower = (
                    error_text.lower()
                )

                # --------------------------------------------------
                # リトライ対象
                # --------------------------------------------------

                retryable = any(
                    x in error_lower
                    for x in [
                        "429",
                        "resource_exhausted",
                        "503",
                        "unavailable",
                        "500",
                        "internal",
                        "502",
                        "bad_gateway",
                        "504",
                        "deadline_exceeded",
                        "high demand",
                        "temporarily unavailable",
                        "temporary error",
                    ]
                )

                # --------------------------------------------------
                # リトライ回数終了
                # --------------------------------------------------

                if (
                    not retryable
                    or attempt
                    > self.GEMINI_MAX_RETRIES
                ):

                    self.logger.error(
                        "★★★ Gemini OCR失敗 ★★★ "
                        f"{error_text}"
                    )

                    return (
                        "ERROR_GEMINI:"
                        f"{error_text}"
                    )

                # --------------------------------------------------
                # RetryInfoから待機時間取得
                # --------------------------------------------------

                retry_seconds = (
                    self._extract_retry_seconds(
                        error_text
                    )
                )

                if retry_seconds is None:

                    retry_seconds = (
                        self.DEFAULT_RETRY_SECONDS
                    )

                self.logger.warning(
                    "★★★ Gemini APIリトライ ★★★ "
                    f"{retry_seconds}秒後 "
                    f"attempt={attempt + 1}"
                )

                time.sleep(
                    retry_seconds
                )

        return "ERROR_GEMINI:unknown"

    # ==========================================================
    # NO_TEXT判定
    # ==========================================================

    def _looks_like_no_text(
        self,
        text
    ):

        normalized = (
            text.strip()
            .replace("`", "")
            .replace("*", "")
            .strip()
        )

        return normalized.upper() in [
            "NO_TEXT",
            "NO TEXT",
            "NO TEXT FOUND",
            "文字なし",
            "テキストなし",
        ]

    # ==========================================================
    # Retry秒数抽出
    # ==========================================================

    def _extract_retry_seconds(
        self,
        text
    ):

        patterns = [
            r"retryDelay[\"']?\s*[:=]\s*[\"']?(\d+)s",
            r"retry after\s+(\d+)\s*seconds?",
            r"retry in\s+(\d+)\s*seconds?",
            r"after\s+(\d+)\s*seconds?",
            r"(\d+)\s*s",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE
            )

            if match:

                try:

                    return int(
                        match.group(1)
                    )

                except (
                    ValueError,
                    TypeError
                ):

                    pass

        return None

    # ==========================================================
    # URL正規化
    # ==========================================================

    def normalize_url(
        self,
        url
    ):

        url = url.strip()

        # フラグメント除去
        parsed = urlparse(url)

        return parsed._replace(
            fragment=""
        ).geturl()

    # ==========================================================
    # Spider終了
    # ==========================================================

    def closed(self, reason):

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            "★★★ Spider終了 ★★★"
        )

        self.logger.info(
            f"reason={reason}"
        )

        self.logger.info(
            f"ページ解析={self.page_count}"
        )

        self.logger.info(
            f"画像発見={self.image_found_count}"
        )

        self.logger.info(
            f"画像取得={self.image_downloaded_count}"
        )

        self.logger.info(
            f"OCR開始={self.ocr_started_count}"
        )

        self.logger.info(
            f"OCR完了={self.ocr_completed_count}"
        )

        self.logger.info(
            f"OCR結果={self.ocr_result_count}"
        )

        self.logger.info(
            f"キーワード一致結果={self.result_count}"
        )

        self.logger.info(
            f"limit={self.limit}"
        )

        self.logger.info(
            "===================================="
        )
        
        
