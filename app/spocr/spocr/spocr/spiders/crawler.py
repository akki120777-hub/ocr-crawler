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

    # ==================================================
    # 基本設定
    # ==================================================

    MODEL_NAME = "gemini-2.5-flash"

    # Gemini APIの最低呼び出し間隔
    # Free TierのRPM対策
    GEMINI_INTERVAL = 14

    # Gemini APIの最大リトライ回数
    # 初回 + 3回リトライ = 最大4回
    GEMINI_MAX_RETRIES = 3

    # デフォルト最大ページ数
    MAX_PAGES = 30

    custom_settings = {

        # ----------------------------------------------
        # Scrapy逐次処理
        # ----------------------------------------------

        "CONCURRENT_REQUESTS": 1,

        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,

        "DOWNLOAD_DELAY": 0.2,

        "DOWNLOAD_TIMEOUT": 30,

        # ----------------------------------------------
        # Telnet Console無効
        # ----------------------------------------------

        "TELNETCONSOLE_ENABLED": False,

        # ----------------------------------------------
        # Remote Control無効
        # ----------------------------------------------

        "EXTENSIONS": {
            "scrapy.extensions.telnet.TelnetConsole": None,
            "scrapy.extensions.remote_control.RemoteControl": None,
        },

        # ----------------------------------------------
        # ログ
        # ----------------------------------------------

        "LOG_LEVEL": "INFO",
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

        super().__init__(*args, **kwargs)

        # ----------------------------------------------
        # URL
        # ----------------------------------------------

        if not url:
            raise ValueError(
                "URLを指定してください"
            )

        self.start_url = url

        self.start_urls = [
            url
        ]

        # ----------------------------------------------
        # keyword
        # ----------------------------------------------

        self.keyword = keyword or ""

        # ----------------------------------------------
        # limit
        # ----------------------------------------------

        try:

            self.limit = max(
                1,
                int(limit)
            )

        except (
            ValueError,
            TypeError
        ):

            self.limit = 5

        # ----------------------------------------------
        # max_pages
        # ----------------------------------------------

        try:

            self.max_pages = max(
                1,
                int(max_pages)
            )

        except (
            ValueError,
            TypeError
        ):

            self.max_pages = self.MAX_PAGES

        # ==================================================
        # カウンタ
        # ==================================================

        self.result_count = 0

        self.page_count = 0

        self.image_found_count = 0

        self.image_fetched_count = 0

        self.ocr_started_count = 0

        self.ocr_completed_count = 0

        self.ocr_result_count = 0

        self.keyword_match_count = 0

        # ==================================================
        # Gemini API管理
        # ==================================================

        self.last_gemini_call = 0.0

        self.gemini_lock = threading.Lock()

        # ==================================================
        # ページ重複防止
        # ==================================================

        self.visited_pages = set()

        # ==================================================
        # APIキー
        # ==================================================

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

        # ==================================================
        # allowed_domains
        # ==================================================

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

        # ==================================================
        # 開始ログ
        # ==================================================

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
            "★★★ OCR完全逐次処理モード ★★★"
        )

        self.logger.info(
            "画像取得 → OCR開始 → API応答 → OCR完了 → 次画像"
        )

        self.logger.info(
            "===================================="
        )

    # ==================================================
    # ページ解析
    # ==================================================

    def parse(self, response):

        # ----------------------------------------------
        # limit確認
        # ----------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み → ページ解析終了 ★★★"
            )

            return

        # ----------------------------------------------
        # max_pages確認
        # ----------------------------------------------

        if self.page_count >= self.max_pages:

            self.logger.info(
                "★★★ max_pages到達 → 終了 ★★★"
            )

            return

        # ----------------------------------------------
        # ページ数
        # ----------------------------------------------

        self.page_count += 1

        current_page_url = (
            response.url
            .split("#")[0]
        )

        self.visited_pages.add(
            current_page_url
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

        # ==================================================
        # Content-Type
        # ==================================================

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

        # ----------------------------------------------
        # img src
        # ----------------------------------------------

        image_sources = response.css(
            "img::attr(src)"
        ).getall()

        for img_src in image_sources:

            if not img_src:
                continue

            img_url = response.urljoin(
                img_src
            )

            if self._is_valid_image_url(
                img_url
            ):

                if img_url not in image_urls:

                    image_urls.append(
                        img_url
                    )

        # ----------------------------------------------
        # data-src
        # ----------------------------------------------

        data_sources = response.css(
            "img::attr(data-src)"
        ).getall()

        for img_src in data_sources:

            if not img_src:
                continue

            img_url = response.urljoin(
                img_src
            )

            if self._is_valid_image_url(
                img_url
            ):

                if img_url not in image_urls:

                    image_urls.append(
                        img_url
                    )

        # ----------------------------------------------
        # data-lazy-src
        # ----------------------------------------------

        lazy_sources = response.css(
            "img::attr(data-lazy-src)"
        ).getall()

        for img_src in lazy_sources:

            if not img_src:
                continue

            img_url = response.urljoin(
                img_src
            )

            if self._is_valid_image_url(
                img_url
            ):

                if img_url not in image_urls:

                    image_urls.append(
                        img_url
                    )

        # ----------------------------------------------
        # img srcset
        # ----------------------------------------------

        srcsets = response.css(
            "img::attr(srcset)"
        ).getall()

        for srcset in srcsets:

            if not srcset:
                continue

            for part in srcset.split(","):

                candidate = part.strip()

                if not candidate:
                    continue

                img_src = candidate.split()[0]

                img_url = response.urljoin(
                    img_src
                )

                if self._is_valid_image_url(
                    img_url
                ):

                    if img_url not in image_urls:

                        image_urls.append(
                            img_url
                        )

        # ----------------------------------------------
        # picture source srcset
        # ----------------------------------------------

        picture_srcsets = response.css(
            "source::attr(srcset)"
        ).getall()

        for srcset in picture_srcsets:

            if not srcset:
                continue

            for part in srcset.split(","):

                candidate = part.strip()

                if not candidate:
                    continue

                img_src = candidate.split()[0]

                img_url = response.urljoin(
                    img_src
                )

                if self._is_valid_image_url(
                    img_url
                ):

                    if img_url not in image_urls:

                        image_urls.append(
                            img_url
                        )

        # ==================================================
        # 画像発見ログ
        # ==================================================

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
        # 次ページ候補
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

                parsed_href = urlparse(
                    href
                )

                # tel / mailto / javascript
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
                next_url = next_url.split(
                    "#"
                )[0]

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
                if (
                    next_url
                    == current_page_url
                ):
                    continue

                # 重複
                if (
                    next_url
                    in self.visited_pages
                ):
                    continue

                next_page_url = next_url

                break

        # ==================================================
        # 画像が存在する
        # ==================================================

        if image_urls:

            self.logger.info(
                "★★★ 画像逐次処理開始 ★★★"
            )

            first_url = image_urls[0]

            self.logger.info(
                "★★★ 最初の画像へ ★★★ "
                f"1/{len(image_urls)} "
                f"{first_url}"
            )

            # ----------------------------------------------
            # ★重要
            #
            # 最初の画像だけRequestする。
            #
            # 2枚目以降はparse_image()が
            # OCR完了後にRequestする。
            #
            # したがって、
            #
            # 画像1
            # ↓
            # OCR1
            # ↓
            # 画像2
            # ↓
            # OCR2
            #
            # となる。
            # ----------------------------------------------

            yield scrapy.Request(

                first_url,

                callback=self.parse_image,

                errback=self.parse_image_error,

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
        # 画像がない
        # ==================================================

        self.logger.info(
            "★★★ このページには処理対象画像なし ★★★"
        )

        next_request = (
            self._make_next_page_request(
                next_page_url
            )
        )

        if next_request:

            yield next_request

        else:

            self.logger.info(
                "★★★ 次に処理できるページなし ★★★"
            )

    # ==================================================
    # 画像URLチェック
    # ==================================================

    def _is_valid_image_url(
        self,
        img_url
    ):

        if not img_url:
            return False

        if img_url.startswith(
            "data:"
        ):
            return False

        parsed = urlparse(
            img_url
        )

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

        # SVG除外
        if clean_url.endswith(
            ".svg"
        ):
            return False

        return True

    # ==================================================
    # 画像解析
    #
    # ★重要
    #
    # async generatorとして動作するが、
    # この関数自身をawaitしない。
    #
    # OCRが完全終了してから、
    # 次のRequestをyieldする。
    # ==================================================

    async def parse_image(
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

        # ==================================================
        # limit確認
        # ==================================================

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み "
                "→ OCRしない ★★★"
            )

            return

        # ==================================================
        # Content-Type
        # ==================================================

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

        # ==================================================
        # 画像ではない
        # ==================================================

        if not content_type.startswith(
            "image/"
        ):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★"
            )

            next_request = (
                self._make_next_image_request(
                    image_urls,
                    image_index,
                    page_url,
                    next_page_url,
                )
            )

            if next_request:

                yield next_request

                return

            next_page_request = (
                self._make_next_page_request(
                    next_page_url
                )
            )

            if next_page_request:

                yield next_page_request

            return

        # ==================================================
        # SVG
        # ==================================================

        if "svg" in content_type:

            self.logger.info(
                "★★★ SVGのためスキップ ★★★"
            )

            next_request = (
                self._make_next_image_request(
                    image_urls,
                    image_index,
                    page_url,
                    next_page_url,
                )
            )

            if next_request:

                yield next_request

                return

            next_page_request = (
                self._make_next_page_request(
                    next_page_url
                )
            )

            if next_page_request:

                yield next_page_request

            return

        # ==================================================
        # OCR開始
        # ==================================================

        self.ocr_started_count += 1

        self.logger.info(
            "------------------------------------"
        )

        self.logger.info(
            f"★★★ Gemini OCR開始 ★★★ "
            f"{img_url}"
        )

        self.logger.info(
            f"★★★ OCR進捗 ★★★ "
            f"{image_index + 1}/{len(image_urls)}"
        )

        # ==================================================
        # Gemini OCR
        # ==================================================

        try:

            # ----------------------------------------------
            # Gemini APIは同期処理。
            #
            # asyncio.to_thread()を使用するが、
            # このRequest自体は次へ進まない。
            #
            # OCRが完全に終わるまでawaitする。
            # ----------------------------------------------

            text = await asyncio.to_thread(

                self._ocr_bytes,

                response.body,

                content_type,
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

        # ==================================================
        # OCRエラー
        # ==================================================

        if text.startswith(
            "ERROR_IMAGE:"
        ):

            self.logger.warning(
                "★★★ 画像読み込みエラー ★★★ "
                f"{text}"
            )

        elif text.startswith(
            "ERROR_GEMINI:"
        ):

            self.logger.error(
                "★★★ Gemini APIエラー ★★★ "
                f"{text}"
            )

        # ==================================================
        # NO_TEXT
        # ==================================================

        elif text == "NO_TEXT":

            self.logger.info(
                "★★★ OCR結果 ★★★ NO_TEXT"
            )

        # ==================================================
        # OCR結果あり
        # ==================================================

        else:

            self.ocr_result_count += 1

            self.logger.info(
                "★★★ OCR結果 ★★★ "
                f"{text[:300]}"
            )

            # ----------------------------------------------
            # キーワードあり
            # ----------------------------------------------

            if self.keyword:

                if self.keyword in text:

                    self.keyword_match_count += 1

                    self.logger.info(
                        "★★★ キーワード一致 ★★★ "
                        f"「{self.keyword}」 "
                        f"→ {img_url}"
                    )

                    # ------------------------------------------
                    # limit再確認
                    # ------------------------------------------

                    if (
                        self.result_count
                        < self.limit
                    ):

                        self.result_count += 1

                        self.logger.info(
                            "★★★ 現在の結果件数 ★★★ "
                            f"{self.result_count}/"
                            f"{self.limit}"
                        )

                        # --------------------------------------
                        # 結果を返す
                        # --------------------------------------

                        yield {

                            "url": img_url,

                            "text": text,

                            "page_url": page_url,

                        }

                        # --------------------------------------
                        # limit到達
                        # --------------------------------------

                        if (
                            self.result_count
                            >= self.limit
                        ):

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

            # ----------------------------------------------
            # キーワードなし
            # ----------------------------------------------

            else:

                if (
                    self.result_count
                    < self.limit
                ):

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

                    if (
                        self.result_count
                        >= self.limit
                    ):

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
        # limit確認
        # ==================================================

        if (
            self.result_count
            >= self.limit
        ):

            return

        # ==================================================
        # 次の画像へ
        # ==================================================

        next_request = (
            self._make_next_image_request(

                image_urls,

                image_index,

                page_url,

                next_page_url,
            )
        )

        if next_request:

            self.logger.info(
                "★★★ OCR完了 → 次の画像へ ★★★"
            )

            yield next_request

            return

        # ==================================================
        # このページの画像を全部処理完了
        # ==================================================

        self.logger.info(
            "★★★ このページの画像OCR完了 ★★★"
        )

        self.logger.info(
            f"★★★ このページの処理枚数 ★★★ "
            f"{len(image_urls)}件"
        )

        # ==================================================
        # 次ページ
        # ==================================================

        next_page_request = (
            self._make_next_page_request(
                next_page_url
            )
        )

        if next_page_request:

            yield next_page_request

            return

        self.logger.info(
            "★★★ 次に処理できるページなし ★★★"
        )

    # ==================================================
    # 画像Requestエラー
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

        page_url = request.meta.get(
            "page_url",
            ""
        )

        image_urls = request.meta.get(
            "image_urls",
            []
        )

        image_index = request.meta.get(
            "image_index",
            0
        )

        next_page_url = request.meta.get(
            "next_page_url"
        )

        self.logger.warning(
            "★★★ 画像取得失敗 ★★★ "
            f"{img_url}"
        )

        self.logger.warning(
            f"★★★ 詳細 ★★★ "
            f"{failure.value}"
        )

        # ----------------------------------------------
        # 次の画像へ
        # ----------------------------------------------

        next_request = (
            self._make_next_image_request(
                image_urls,
                image_index,
                page_url,
                next_page_url,
            )
        )

        if next_request:

            self.logger.info(
                "★★★ 画像取得失敗 → 次の画像へ ★★★"
            )

            yield next_request

            return

        # ----------------------------------------------
        # 次ページへ
        # ----------------------------------------------

        next_page_request = (
            self._make_next_page_request(
                next_page_url
            )
        )

        if next_page_request:

            yield next_page_request

            return

        self.logger.info(
            "★★★ 次に処理できるページなし ★★★"
        )

    # ==================================================
    # ページRequestエラー
    # ==================================================

    def parse_page_error(
        self,
        failure
    ):

        self.logger.warning(
            "★★★ ページ取得失敗 ★★★ "
            f"{failure.request.url}"
        )

        self.logger.warning(
            f"★★★ 詳細 ★★★ "
            f"{failure.value}"
        )

    # ==================================================
    # 次の画像Requestを作る
    #
    # ★通常のdef
    #
    # async generatorではない。
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

        next_index = (
            current_index + 1
        )

        # ----------------------------------------------
        # limit
        # ----------------------------------------------

        if (
            self.result_count
            >= self.limit
        ):

            return None

        # ----------------------------------------------
        # 次の画像
        # ----------------------------------------------

        if (
            next_index
            < len(image_urls)
        ):

            next_url = image_urls[
                next_index
            ]

            self.logger.info(
                "★★★ 次の画像へ ★★★ "
                f"{next_index + 1}/"
                f"{len(image_urls)} "
                f"{next_url}"
            )

            return scrapy.Request(

                next_url,

                callback=self.parse_image,

                errback=self.parse_image_error,

                meta={

                    "img_url": next_url,

                    "page_url": page_url,

                    "image_urls": image_urls,

                    "image_index": next_index,

                    "next_page_url": next_page_url,

                },

                dont_filter=True,
            )

        # ----------------------------------------------
        # 画像終了
        # ----------------------------------------------

        return None

    # ==================================================
    # 次ページRequest
    #
    # ★重要
    #
    # generatorではなく
    # RequestまたはNoneを返す。
    # ==================================================

    def _make_next_page_request(
        self,
        next_page_url
    ):

        if not next_page_url:

            return None

        if (
            self.result_count
            >= self.limit
        ):

            return None

        if (
            self.page_count
            >= self.max_pages
        ):

            return None

        if (
            next_page_url
            in self.visited_pages
        ):

            return None

        self.visited_pages.add(
            next_page_url
        )

        self.logger.info(
            "★★★ 次のページへ ★★★ "
            f"{next_page_url}"
        )

        return scrapy.Request(

            next_page_url,

            callback=self.parse,

            errback=self.parse_page_error,

            dont_filter=True,
        )

    # ==================================================
    # Gemini OCR
    # ==================================================

    def _ocr_bytes(
        self,
        img_bytes,
        content_type="image/jpeg"
    ):

        # ==================================================
        # Gemini APIを完全直列化
        # ==================================================

        with self.gemini_lock:

            # ==================================================
            # 14秒間隔
            # ==================================================

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

            # ==================================================
            # 画像読み込み
            # ==================================================

            try:

                image = Image.open(
                    io.BytesIO(
                        img_bytes
                    )
                )

                image.load()

            except Exception as e:

                self.logger.warning(
                    f"画像読み込みエラー: {e}"
                )

                return (
                    f"ERROR_IMAGE:{e}"
                )

            # ==================================================
            # OCRプロンプト
            # ==================================================

            prompt_text = """
この画像に含まれる日本語テキストだけを正確に抽出してください。

重要なルール：

1. 画像内に日本語の文字が存在する場合
   → 画像に実際に書かれている日本語テキストだけを返してください。

2. 画像内に日本語の文字が存在しない場合
   → 必ず NO_TEXT とだけ返してください。

3. 「この画像には日本語テキストがありません」
   「日本語テキストは含まれていません」
   などの説明文は絶対に返さないでください。

4. 画像の内容の説明、推測、解説は不要です。

5. 日本語テキストが存在する場合は、
   抽出した文字だけを返してください。
""".strip()

            # ==================================================
            # Gemini API
            # ==================================================

            for attempt in range(
                self.GEMINI_MAX_RETRIES + 1
            ):

                try:

                    self.logger.info(
                        "★★★ Gemini API呼び出し ★★★ "
                        f"attempt={attempt + 1}/"
                        f"{self.GEMINI_MAX_RETRIES + 1}"
                    )

                    # ------------------------------------------
                    # API呼び出し時刻
                    # ------------------------------------------

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

                    # ------------------------------------------
                    # response
                    # ------------------------------------------

                    if (
                        response
                        and response.text
                    ):

                        text = (
                            response.text
                            .strip()
                        )

                        # --------------------------------------
                        # NO_TEXT
                        # --------------------------------------

                        if (
                            text.upper()
                            == "NO_TEXT"
                        ):

                            return "NO_TEXT"

                        # --------------------------------------
                        # Geminiの説明文
                        # --------------------------------------

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

                    # ------------------------------------------
                    # 空レスポンス
                    # ------------------------------------------

                    return "NO_TEXT"

                except Exception as e:

                    error_text = str(e)

                    self.logger.error(
                        "★★★ Gemini APIエラー ★★★ "
                        f"{error_text}"
                    )

                    # ==================================================
                    # リトライ対象
                    # ==================================================

                    is_retryable = (

                        "429"
                        in error_text

                        or

                        "RESOURCE_EXHAUSTED"
                        in error_text

                        or

                        "503"
                        in error_text

                        or

                        "UNAVAILABLE"
                        in error_text

                        or

                        "high demand"
                        in error_text.lower()

                        or

                        "500"
                        in error_text

                        or

                        "502"
                        in error_text

                        or

                        "504"
                        in error_text

                        or

                        "INTERNAL"
                        in error_text
                    )

                    if not is_retryable:

                        return (
                            "ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ==================================================
                    # 最終試行
                    # ==================================================

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

                    # ==================================================
                    # 待機時間
                    # ==================================================

                    wait_time = (
                        self._extract_retry_seconds(
                            error_text
                        )
                    )

                    if wait_time is None:

                        if (

                            "503"
                            in error_text

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

                    # ------------------------------------------
                    # 少し余裕を持つ
                    # ------------------------------------------

                    wait_time += 2

                    self.logger.warning(
                        "★★★ Geminiリトライ待機 ★★★ "
                        f"{wait_time:.1f}秒"
                    )

                    time.sleep(
                        wait_time
                    )

            return (
                "ERROR_GEMINI:"
                "Unknown error"
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
