import scrapy
from urllib.parse import urlparse
import io
import os
import time
import re
import asyncio
import threading

from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    MODEL_NAME = "gemini-2.5-flash"

    # Gemini APIの最低呼び出し間隔
    # Free TierのRPM対策
    GEMINI_INTERVAL = 14

    # Gemini APIの最大リトライ回数
    GEMINI_MAX_RETRIES = 3

    # デフォルト最大ページ数
    MAX_PAGES = 30

    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 0.2,
        "DOWNLOAD_TIMEOUT": 30,
        "TELNETCONSOLE_ENABLED": False,
    }

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

        self.start_urls = [url]

        self.keyword = keyword or ""

        # ----------------------------------------------
        # limit
        # ----------------------------------------------

        try:
            self.limit = max(1, int(limit))
        except (ValueError, TypeError):
            self.limit = 5

        # ----------------------------------------------
        # max_pages
        # ----------------------------------------------

        try:
            self.max_pages = max(1, int(max_pages))
        except (ValueError, TypeError):
            self.max_pages = self.MAX_PAGES

        # ----------------------------------------------
        # カウンタ
        # ----------------------------------------------

        self.result_count = 0
        self.page_count = 0

        self.image_found_count = 0
        self.image_fetched_count = 0
        self.ocr_started_count = 0
        self.ocr_completed_count = 0
        self.ocr_result_count = 0
        self.keyword_match_count = 0

        # ----------------------------------------------
        # Gemini API管理
        # ----------------------------------------------

        self.last_gemini_call = 0.0
        self.gemini_lock = threading.Lock()

        # ----------------------------------------------
        # 重複ページ防止
        # ----------------------------------------------

        self.visited_pages = set()

        # ----------------------------------------------
        # APIキー
        # ----------------------------------------------

        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY が設定されていません"
            )

        self.gemini_client = client.Client(
            api_key=api_key
        )

        # ----------------------------------------------
        # allowed_domains
        # ----------------------------------------------

        if domain is not None:
            if domain == "":
                self.allowed_domains = []
            else:
                self.allowed_domains = [domain]
        else:
            parsed = urlparse(url)

            if parsed.hostname:
                self.allowed_domains = [
                    parsed.hostname
                ]
            else:
                self.allowed_domains = []

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
            f"Gemini OCR間隔={self.GEMINI_INTERVAL}秒"
        )
        self.logger.info(
            "★★★ OCR逐次処理モード ★★★"
        )
        self.logger.info(
            "===================================="
        )

    # ==================================================
    # ページ解析
    # ==================================================

    async def parse(self, response):

        # ----------------------------------------------
        # limit確認
        # ----------------------------------------------

        if self.result_count >= self.limit:
            self.logger.info(
                "★★★ limit到達済み → ページ解析終了 ★★★"
            )
            return

        # ----------------------------------------------
        # ページ数確認
        # ----------------------------------------------

        if self.page_count >= self.max_pages:
            self.logger.info(
                "★★★ max_pages到達 → 終了 ★★★"
            )
            return

        self.page_count += 1

        current_page_url = response.url

        self.visited_pages.add(
            current_page_url.split("#")[0]
        )

        self.logger.info(
            "===================================="
        )
        self.logger.info(
            f"★★★ ページ解析 "
            f"{self.page_count}/{self.max_pages} ★★★"
        )
        self.logger.info(
            f"URL={current_page_url}"
        )

        # ----------------------------------------------
        # Content-Type
        # ----------------------------------------------

        content_type = (
            response.headers
            .get("Content-Type", b"")
            .decode(
                "utf-8",
                errors="ignore"
            )
            .lower()
        )

        self.logger.info(
            f"Content-Type={content_type}"
        )

        if "text/html" not in content_type:

            self.logger.info(
                "★★★ HTMLではないためスキップ ★★★"
            )

            return

        # ==================================================
        # 画像URL収集
        # ==================================================

        image_urls = []

        # img src
        image_sources = response.css(
            "img::attr(src)"
        ).getall()

        # srcset
        srcsets = response.css(
            "img::attr(srcset)"
        ).getall()

        for img_src in image_sources:

            if not img_src:
                continue

            img_url = response.urljoin(
                img_src
            )

            if self._is_valid_image_url(img_url):

                if img_url not in image_urls:

                    image_urls.append(
                        img_url
                    )

        # srcsetからも取得
        for srcset in srcsets:

            if not srcset:
                continue

            for part in srcset.split(","):

                candidate = part.strip()

                if not candidate:
                    continue

                # "url 2x" のような形式
                img_src = candidate.split()[0]

                img_url = response.urljoin(
                    img_src
                )

                if self._is_valid_image_url(img_url):

                    if img_url not in image_urls:

                        image_urls.append(
                            img_url
                        )

        # ----------------------------------------------
        # 画像発見ログ
        # ----------------------------------------------

        for img_url in image_urls:

            self.image_found_count += 1

            self.logger.info(
                f"★★★ 画像発見 ★★★ "
                f"{img_url}"
            )

        self.logger.info(
            f"★★★ ページ内画像候補: "
            f"{len(image_urls)}件 ★★★"
        )

        # ==================================================
        # 次ページ候補を1つだけ決定
        # ==================================================

        next_page_url = None

        if self.page_count < self.max_pages:

            links = response.css(
                "a::attr(href)"
            ).getall()

            self.logger.info(
                f"★★★ ページ内リンク数: "
                f"{len(links)} ★★★"
            )

            for href in links:

                if not href:
                    continue

                parsed_href = urlparse(href)

                # tel / mailto除外
                if parsed_href.scheme in (
                    "tel",
                    "mailto",
                    "javascript"
                ):
                    continue

                next_url = response.urljoin(
                    href
                )

                # fragment除去
                next_url = next_url.split("#")[0]

                if not next_url:
                    continue

                parsed_next = urlparse(
                    next_url
                )

                # http / httpsのみ
                if parsed_next.scheme not in (
                    "http",
                    "https"
                ):
                    continue

                # 外部ドメイン除外
                if self.allowed_domains:

                    if (
                        parsed_next.hostname
                        not in self.allowed_domains
                    ):
                        continue

                # 自分自身
                if next_url == current_page_url.split("#")[0]:
                    continue

                # 重複ページ
                if next_url in self.visited_pages:
                    continue

                next_page_url = next_url

                break

        # ==================================================
        # 画像逐次処理
        # ==================================================

        if image_urls:

            self.logger.info(
                "★★★ 画像逐次処理開始 ★★★"
            )

            # 最初の画像だけをRequestする
            #
            # 残りの画像はparse_image()が
            # OCR完了後に次のRequestを生成する。
            #
            # これにより、
            #
            # 画像1
            # ↓
            # OCR1
            # ↓
            # 画像2
            # ↓
            # OCR2
            #
            # という完全逐次処理になる。

            first_url = image_urls[0]

            yield scrapy.Request(
                first_url,
                callback=self.parse_image,
                meta={
                    "img_url": first_url,
                    "page_url": current_page_url,
                    "image_urls": image_urls,
                    "image_index": 0,
                    "next_page_url": next_page_url,
                },
                dont_filter=True,
            )

            return

        # ==================================================
        # 画像がない場合
        # ==================================================

        self.logger.info(
            "★★★ このページには処理対象画像なし ★★★"
        )

        if next_page_url:

            self.visited_pages.add(
                next_page_url
            )

            self.logger.info(
                f"★★★ 次のページへ ★★★ "
                f"{next_page_url}"
            )

            yield scrapy.Request(
                next_page_url,
                callback=self.parse,
            )

        else:

            self.logger.info(
                "★★★ 次に処理できるページなし ★★★"
            )

    # ==================================================
    # 画像URLチェック
    # ==================================================

    def _is_valid_image_url(self, img_url):

        if not img_url:
            return False

        if img_url.startswith("data:"):
            return False

        parsed = urlparse(img_url)

        if parsed.scheme not in (
            "http",
            "https"
        ):
            return False

        clean_url = (
            img_url
            .lower()
            .split("?")[0]
        )

        if clean_url.endswith(".svg"):
            return False

        return True

    # ==================================================
    # 画像解析
    #
    # ★重要
    #
    # async generatorとして動作する。
    # しかし、この関数自身を await することはしない。
    #
    # OCR完了後、この関数の中から直接
    # 次のRequestをyieldする。
    # ==================================================

    async def parse_image(self, response):

        img_url = response.meta.get(
            "img_url",
            response.url
        )

        page_url = response.meta.get(
            "page_url",
            ""
        )

        image_urls = response.meta.get(
            "image_urls",
            []
        )

        image_index = response.meta.get(
            "image_index",
            0
        )

        next_page_url = response.meta.get(
            "next_page_url"
        )

        # ----------------------------------------------
        # limit確認
        # ----------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み → 画像OCRしない ★★★"
            )

            return

        # ----------------------------------------------
        # Content-Type
        # ----------------------------------------------

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

        self.image_fetched_count += 1

        self.logger.info(
            f"★★★ 画像取得 ★★★ "
            f"status={response.status} "
            f"{img_url}"
        )

        self.logger.info(
            f"★★★ 画像Content-Type ★★★ "
            f"{content_type}"
        )

        # ----------------------------------------------
        # 画像ではない
        # ----------------------------------------------

        if not content_type.startswith("image/"):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★"
            )

            # 次の画像へ
            next_request = self._make_next_image_request(
                image_urls,
                image_index,
                page_url,
                next_page_url,
            )

            if next_request:
                yield next_request
            else:
                yield from self._make_next_page_request(
                    next_page_url
                )

            return

        # ----------------------------------------------
        # SVG
        # ----------------------------------------------

        if "svg" in content_type:

            self.logger.info(
                "★★★ SVGのためスキップ ★★★"
            )

            next_request = self._make_next_image_request(
                image_urls,
                image_index,
                page_url,
                next_page_url,
            )

            if next_request:
                yield next_request
            else:
                yield from self._make_next_page_request(
                    next_page_url
                )

            return

        # ==================================================
        # OCR開始
        # ==================================================

        self.ocr_started_count += 1

        self.logger.info(
            f"★★★ Gemini OCR開始 ★★★ "
            f"{img_url}"
        )

        try:

            # ------------------------------------------
            # 重要
            #
            # Gemini APIは同期処理なので
            # asyncio.to_thread()で別スレッドへ。
            #
            # ただしRequestは次に進めない。
            #
            # OCRが完全終了するまで
            # この関数は待機する。
            # ------------------------------------------

            text = await asyncio.to_thread(
                self._ocr_bytes,
                response.body
            )

        except Exception as e:

            self.logger.error(
                f"★★★ OCR例外 ★★★ "
                f"{e}"
            )

            text = (
                f"ERROR_GEMINI:{e}"
            )

        # ==================================================
        # OCR完了
        # ==================================================

        self.ocr_completed_count += 1

        self.logger.info(
            f"★★★ Gemini OCR完了 ★★★ "
            f"{img_url}"
        )

        # ----------------------------------------------
        # エラー
        # ----------------------------------------------

        if text.startswith(
            "ERROR_IMAGE:"
        ):

            self.logger.warning(
                f"★★★ 画像読み込みエラー ★★★ "
                f"{text}"
            )

        elif text.startswith(
            "ERROR_GEMINI:"
        ):

            self.logger.error(
                f"★★★ Gemini APIエラー ★★★ "
                f"{text}"
            )

        # ----------------------------------------------
        # NO_TEXT
        # ----------------------------------------------

        elif text == "NO_TEXT":

            self.logger.info(
                f"★★★ 日本語テキストなし ★★★ "
                f"{img_url}"
            )

        # ----------------------------------------------
        # OCR結果あり
        # ----------------------------------------------

        else:

            self.ocr_result_count += 1

            self.logger.info(
                f"★★★ OCR結果 ★★★ "
                f"{text[:300]}"
            )

            # ------------------------------------------
            # キーワード判定
            # ------------------------------------------

            if self.keyword:

                if self.keyword in text:

                    self.keyword_match_count += 1

                    # limit再確認
                    if self.result_count < self.limit:

                        self.result_count += 1

                        self.logger.info(
                            "★★★ キーワード一致 ★★★ "
                            f"「{self.keyword}」 "
                            f"→ {img_url}"
                        )

                        self.logger.info(
                            "★★★ 現在の結果件数 ★★★ "
                            f"{self.result_count}/"
                            f"{self.limit}"
                        )

                        # ----------------------------------
                        # 結果を返す
                        # ----------------------------------

                        yield {
                            "url": img_url,
                            "text": text,
                            "page_url": page_url,
                        }

                        # ----------------------------------
                        # limit到達
                        # ----------------------------------

                        if self.result_count >= self.limit:

                            self.logger.info(
                                "★★★ limit到達 ★★★ "
                                f"{self.result_count}件"
                            )

                            self.crawler.engine.close_spider(
                                self,
                                reason="result_limit_reached"
                            )

                            return

                else:

                    self.logger.info(
                        "★★★ キーワード不一致 ★★★ "
                        f"「{self.keyword}」 "
                        f"→ {img_url}"
                    )

            else:

                # キーワードなしの場合は
                # OCRできた画像を結果にする

                if self.result_count < self.limit:

                    self.result_count += 1

                    self.logger.info(
                        "★★★ キーワードなし "
                        "→ 結果として返す ★★★"
                    )

                    self.logger.info(
                        "★★★ 現在の結果件数 ★★★ "
                        f"{self.result_count}/"
                        f"{self.limit}"
                    )

                    yield {
                        "url": img_url,
                        "text": text,
                        "page_url": page_url,
                    }

                    if self.result_count >= self.limit:

                        self.logger.info(
                            "★★★ limit到達 ★★★ "
                            f"{self.result_count}件"
                        )

                        self.crawler.engine.close_spider(
                            self,
                            reason="result_limit_reached"
                        )

                        return

        # ==================================================
        # OCR完了後
        #
        # 次の画像へ進む
        # ==================================================

        if self.result_count >= self.limit:
            return

        next_request = self._make_next_image_request(
            image_urls,
            image_index,
            page_url,
            next_page_url,
        )

        if next_request:

            yield next_request

            return

        # ==================================================
        # このページの画像を全部処理した
        #
        # → 次ページへ
        # ==================================================

        self.logger.info(
            "★★★ このページの画像OCR完了 ★★★"
        )

        if next_page_url:

            if self.page_count < self.max_pages:

                if (
                    next_page_url
                    not in self.visited_pages
                ):

                    self.visited_pages.add(
                        next_page_url
                    )

                    self.logger.info(
                        "★★★ 次のページへ ★★★ "
                        f"{next_page_url}"
                    )

                    yield scrapy.Request(
                        next_page_url,
                        callback=self.parse,
                    )

                    return

        self.logger.info(
            "★★★ 次に処理できるページなし ★★★"
        )

    # ==================================================
    # 次の画像Requestを作る
    # ==================================================

    def _make_next_image_request(
        self,
        image_urls,
        current_index,
        page_url,
        next_page_url,
    ):

        if not image_urls:
            return None

        next_index = current_index + 1

        # ----------------------------------------------
        # まだ画像がある
        # ----------------------------------------------

        if next_index < len(image_urls):

            if self.result_count >= self.limit:
                return None

            next_url = image_urls[next_index]

            self.logger.info(
                "★★★ 次の画像へ ★★★ "
                f"{next_index + 1}/{len(image_urls)} "
                f"{next_url}"
            )

            return scrapy.Request(
                next_url,
                callback=self.parse_image,
                meta={
                    "img_url": next_url,
                    "page_url": page_url,
                    "image_urls": image_urls,
                    "image_index": next_index,
                    "next_page_url": next_page_url,
                },
                dont_filter=True,
            )

        return None

    # ==================================================
    # 次ページRequest
    # ==================================================

    def _make_next_page_request(
        self,
        next_page_url
    ):

        if not next_page_url:
            return

        if self.result_count >= self.limit:
            return

        if self.page_count >= self.max_pages:
            return

        if next_page_url in self.visited_pages:
            return

        self.visited_pages.add(
            next_page_url
        )

        self.logger.info(
            "★★★ 次のページへ ★★★ "
            f"{next_page_url}"
        )

        yield scrapy.Request(
            next_page_url,
            callback=self.parse,
        )

    # ==================================================
    # Gemini OCR
    # ==================================================

    def _ocr_bytes(self, img_bytes):

        # ----------------------------------------------
        # Gemini APIを完全直列化
        # ----------------------------------------------

        with self.gemini_lock:

            # ------------------------------------------
            # 14秒間隔
            # ------------------------------------------

            now = time.monotonic()

            elapsed = (
                now - self.last_gemini_call
            )

            if elapsed < self.GEMINI_INTERVAL:

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

            # ------------------------------------------
            # 画像読み込み
            # ------------------------------------------

            try:

                image = Image.open(
                    io.BytesIO(img_bytes)
                )

                image.load()

            except Exception as e:

                self.logger.warning(
                    f"画像読み込みエラー: {e}"
                )

                return (
                    f"ERROR_IMAGE:{e}"
                )

            # ------------------------------------------
            # OCRプロンプト
            # ------------------------------------------

            prompt_text = """
この画像に含まれる日本語テキストだけを正確に抽出してください。

重要なルール：

1. 画像内に日本語の文字が存在する場合
   → 画像に実際に書かれている日本語テキストだけを返してください。

2. 画像内に日本語テキストが存在しない場合
   → 必ず NO_TEXT とだけ返してください。

3. 「この画像には日本語テキストがありません」
   「日本語テキストは含まれていません」
   などの説明文は絶対に返さないでください。

4. 画像の内容の説明、推測、解説は不要です。

5. 日本語テキストが存在する場合は、
   抽出した文字だけを返してください。
""".strip()

            # ------------------------------------------
            # Gemini API
            # ------------------------------------------

            for attempt in range(
                self.GEMINI_MAX_RETRIES + 1
            ):

                try:

                    self.logger.info(
                        "★★★ Gemini API呼び出し ★★★ "
                        f"attempt={attempt + 1}/"
                        f"{self.GEMINI_MAX_RETRIES + 1}"
                    )

                    # API呼び出し時刻
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
                                image,
                            ],
                        )
                    )

                    self.logger.info(
                        "★★★ Gemini API応答受信 ★★★"
                    )

                    if (
                        response
                        and response.text
                    ):

                        text = (
                            response.text
                            .strip()
                        )

                        if text.upper() == "NO_TEXT":

                            return "NO_TEXT"

                        if self._looks_like_no_text(
                            text
                        ):

                            self.logger.info(
                                "★★★ Geminiの"
                                "「文字なし」説明文を"
                                "NO_TEXTとして処理 ★★★"
                            )

                            return "NO_TEXT"

                        return text

                    return "NO_TEXT"

                except Exception as e:

                    error_text = str(e)

                    self.logger.error(
                        "★★★ Gemini APIエラー ★★★ "
                        f"{error_text}"
                    )

                    # ----------------------------------
                    # リトライ対象
                    # ----------------------------------

                    is_retryable = (
                        "429" in error_text
                        or
                        "RESOURCE_EXHAUSTED"
                        in error_text
                        or
                        "503" in error_text
                        or
                        "UNAVAILABLE"
                        in error_text
                        or
                        "high demand"
                        in error_text.lower()
                        or
                        "500" in error_text
                        or
                        "INTERNAL" in error_text
                    )

                    if not is_retryable:

                        return (
                            f"ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ----------------------------------
                    # 最終試行
                    # ----------------------------------

                    if (
                        attempt
                        >= self.GEMINI_MAX_RETRIES
                    ):

                        self.logger.error(
                            "★★★ Geminiリトライ上限到達 ★★★"
                        )

                        return (
                            f"ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ----------------------------------
                    # 待機時間
                    # ----------------------------------

                    wait_time = (
                        self._extract_retry_seconds(
                            error_text
                        )
                    )

                    if wait_time is None:

                        if (
                            "503" in error_text
                            or
                            "UNAVAILABLE"
                            in error_text
                            or
                            "high demand"
                            in error_text.lower()
                        ):
                            wait_time = 10
                        else:
                            wait_time = 45

                    # 少し余裕を持つ
                    wait_time += 2

                    self.logger.warning(
                        "★★★ Geminiリトライ待機 ★★★ "
                        f"{wait_time:.1f}秒"
                    )

                    time.sleep(
                        wait_time
                    )

            return (
                "ERROR_GEMINI:Unknown error"
            )

    # ==================================================
    # NO_TEXT判定
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
            "文字はありません",
        ]

        for pattern in no_text_patterns:

            if pattern in normalized:
                return True

        return False

    # ==================================================
    # Retry-After取得
    # ==================================================

    def _extract_retry_seconds(
        self,
        error_text
    ):

        patterns = [

            r"retry in ([0-9.]+)s",

            r"retryDelay.*?([0-9.]+)s",

            r"Retry-After.*?([0-9.]+)",

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

                except ValueError:

                    pass

        return None

    # ==================================================
    # Spider終了
    # ==================================================

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
            f"画像取得={self.image_fetched_count}"
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
            f"キーワード一致結果="
            f"{self.keyword_match_count}"
        )

        self.logger.info(
            f"取得結果={self.result_count}"
        )

        self.logger.info(
            f"limit={self.limit}"
        )

        self.logger.info(
            "===================================="
        )
