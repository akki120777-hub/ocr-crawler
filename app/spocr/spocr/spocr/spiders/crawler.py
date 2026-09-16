import scrapy
from urllib.parse import urlparse
import io
import os
import time
import re

from twisted.internet import threads

from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    MODEL_NAME = "gemini-2.5-flash"

    # Gemini Free Tier 5 RPM対策
    # 5回/分を確実に超えにくくするため14秒間隔
    GEMINI_INTERVAL = 14

    # 429時の最大リトライ回数
    GEMINI_MAX_RETRIES = 3

    # クロールするページ数の上限
    MAX_PAGES = 30

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

        # 結果取得数
        try:
            self.limit = max(1, int(limit))
        except (ValueError, TypeError):
            self.limit = 5

        # ページ数上限
        try:
            self.max_pages = max(1, int(max_pages))
        except (ValueError, TypeError):
            self.max_pages = self.MAX_PAGES

        # 実際に返した結果数
        self.result_count = 0

        # 解析したページ数
        self.page_count = 0

        # Gemini APIの最後の呼び出し時刻
        self.last_gemini_call = 0.0

        # API呼び出しを直列化するためのロック
        import threading
        self.gemini_lock = threading.Lock()

        # 既に処理したURL
        self.visited_pages = set()

        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY が設定されていません"
            )

        self.gemini_client = client.Client(
            api_key=api_key
        )

        # allowed_domains
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
            "Gemini OCR間隔="
            f"{self.GEMINI_INTERVAL}秒"
        )

        self.logger.info(
            "===================================="
        )

    # --------------------------------------------------
    # ページ解析
    # --------------------------------------------------

    def parse(self, response):
        # 既に結果上限に達している場合
        if self.result_count >= self.limit:
            self.logger.info(
                "★★★ 結果上限到達済み → ページ解析終了 ★★★"
            )
            return

        # ページ数上限
        self.page_count += 1

        self.logger.info(
            "===================================="
        )

        self.logger.info(
            f"★★★ ページ解析 {self.page_count}"
            f"/{self.max_pages} ★★★"
        )

        self.logger.info(
            f"URL={response.url}"
        )

        # Content-Type確認
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

        # ----------------------------------------------
        # 画像取得
        # ----------------------------------------------

        image_sources = response.css(
            "img::attr(src)"
        ).getall()

        self.logger.info(
            f"★★★ ページ内画像数: "
            f"{len(image_sources)} ★★★"
        )

        for img_src in image_sources:

            # 結果上限に達したら終了
            if self.result_count >= self.limit:
                self.logger.info(
                    "★★★ limit到達 → 画像処理終了 ★★★"
                )
                break

            if not img_src:
                continue

            # URL化
            img_url = response.urljoin(
                img_src
            )

            # SVG除外
            clean_url = img_url.lower().split("?")[0]

            if clean_url.endswith(".svg"):
                self.logger.info(
                    f"★★★ SVG画像をスキップ ★★★ "
                    f"{img_url}"
                )
                continue

            # data URLなどを除外
            if img_url.startswith("data:"):
                continue

            self.logger.info(
                f"★★★ 画像発見 ★★★ "
                f"{img_url}"
            )

            yield scrapy.Request(
                img_url,
                callback=self.parse_image,
                meta={
                    "img_url": img_url,
                    "page_url": response.url
                },
                dont_filter=True
            )

        # ----------------------------------------------
        # 次のページ
        # ----------------------------------------------

        if self.page_count >= self.max_pages:
            self.logger.info(
                "★★★ ページ上限到達 ★★★"
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

            if self.result_count >= self.limit:
                break

            parsed_href = urlparse(href)

            if parsed_href.scheme in (
                "http",
                "https",
                ""
            ):

                if parsed_href.path.startswith("#"):
                    continue

                next_url = response.urljoin(
                    href
                )

                # 同一URLの重複防止
                normalized_url = (
                    next_url.split("#")[0]
                )

                if normalized_url in self.visited_pages:
                    continue

                self.visited_pages.add(
                    normalized_url
                )

                self.logger.info(
                    f"★★★ 次のページへ ★★★ "
                    f"{next_url}"
                )

                yield response.follow(
                    href,
                    callback=self.parse
                )

            elif parsed_href.scheme in (
                "tel",
                "mailto"
            ):
                self.logger.debug(
                    f"tel/mailtoをスキップ: {href}"
                )

    # --------------------------------------------------
    # Gemini OCR
    # --------------------------------------------------

    def _ocr_bytes(self, img_bytes):

        # Gemini APIの同時実行を防ぐ
        with self.gemini_lock:

            # ------------------------------------------
            # 最低14秒間隔を確保
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

                time.sleep(wait_time)

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

                return f"ERROR_IMAGE:{e}"

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
                        f"attempt={attempt + 1}"
                    )

                    # API呼び出し直前に時刻を記録
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

                    # ----------------------------------
                    # 結果取得
                    # ----------------------------------

                    if (
                        response
                        and response.text
                    ):

                        text = (
                            response.text
                            .strip()
                        )

                        # NO_TEXT
                        if text.upper() == "NO_TEXT":
                            return "NO_TEXT"

                        # Geminiが説明文を返した場合の対策
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
                    # 429判定
                    # ----------------------------------

                    if (
                        "429" not in error_text
                        and
                        "RESOURCE_EXHAUSTED"
                        not in error_text
                    ):
                        return (
                            f"ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # 最終試行なら終了
                    if (
                        attempt
                        >= self.GEMINI_MAX_RETRIES
                    ):
                        self.logger.error(
                            "★★★ Gemini 429 "
                            "リトライ上限到達 ★★★"
                        )

                        return (
                            f"ERROR_GEMINI:"
                            f"{error_text}"
                        )

                    # ----------------------------------
                    # Retry-After秒数を抽出
                    # ----------------------------------

                    wait_time = (
                        self._extract_retry_seconds(
                            error_text
                        )
                    )

                    if wait_time is None:
                        wait_time = 45

                    # 少し余裕を持たせる
                    wait_time += 2

                    self.logger.warning(
                        "★★★ Gemini 429発生 ★★★ "
                        f"{wait_time:.1f}秒待って再試行"
                    )

                    time.sleep(
                        wait_time
                    )

            return "ERROR_GEMINI:Unknown error"

    # --------------------------------------------------
    # 「日本語テキストなし」判定
    # --------------------------------------------------

    def _looks_like_no_text(self, text):

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

    # --------------------------------------------------
    # 429の待機時間を取得
    # --------------------------------------------------

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

    # --------------------------------------------------
    # 画像OCR結果
    # --------------------------------------------------

    def parse_image(self, response):

        img_url = response.meta.get(
            "img_url",
            response.url
        )

        page_url = response.meta.get(
            "page_url",
            ""
        )

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
            f"★★★ 画像取得 ★★★ "
            f"{img_url}"
        )

        self.logger.info(
            f"★★★ 画像Content-Type ★★★ "
            f"{content_type}"
        )

        # ----------------------------------------------
        # 画像ではない
        # ----------------------------------------------

        if not content_type.startswith(
            "image/"
        ):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★ "
                f"{img_url}"
            )

            return

        # SVG
        if "svg" in content_type:

            self.logger.info(
                "★★★ SVGのためスキップ ★★★ "
                f"{img_url}"
            )

            return

        # ----------------------------------------------
        # limit確認
        # ----------------------------------------------

        if self.result_count >= self.limit:

            self.logger.info(
                "★★★ limit到達済み "
                "→ OCRしない ★★★"
            )

            return

        # ----------------------------------------------
        # OCR開始
        # ----------------------------------------------

        self.logger.info(
            f"★★★ Gemini OCR開始 ★★★ "
            f"{img_url}"
        )

        d = threads.deferToThread(
            self._ocr_bytes,
            response.body
        )

        # ----------------------------------------------
        # OCR成功
        # ----------------------------------------------

        def _on_ocr(text):

            self.logger.info(
                f"★★★ Gemini OCR完了 ★★★ "
                f"{img_url}"
            )

            # 画像エラー
            if text.startswith(
                "ERROR_IMAGE:"
            ):

                self.logger.warning(
                    f"画像読み込みエラー: "
                    f"{text}"
                )

                return []

            # Geminiエラー
            if text.startswith(
                "ERROR_GEMINI:"
            ):

                self.logger.error(
                    f"Gemini APIエラー: "
                    f"{text}"
                )

                return []

            # NO_TEXT
            if text == "NO_TEXT":

                self.logger.info(
                    "★★★ 日本語テキストなし ★★★ "
                    f"{img_url}"
                )

                return []

            self.logger.info(
                "★★★ OCR結果 ★★★ "
                f"{text[:300]}"
            )

            # ------------------------------------------
            # 結果上限チェック
            # ------------------------------------------

            if self.result_count >= self.limit:

                self.logger.info(
                    "★★★ 他のOCR処理でlimit到達 "
                    "→ 結果を追加しない ★★★"
                )

                return []

            # ------------------------------------------
            # キーワード検索
            # ------------------------------------------

            if self.keyword:

                if self.keyword in text:

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

                    result = [
                        {
                            "url": img_url,
                            "text": text,
                            "page_url": page_url
                        }
                    ]

                    # ----------------------------------
                    # limit到達
                    # ----------------------------------

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
                            reason=(
                                "result_limit_reached"
                            )
                        )

                    return result

                else:

                    self.logger.info(
                        "★★★ キーワード不一致 ★★★ "
                        f"「{self.keyword}」 "
                        f"→ {img_url}"
                    )

                    return []

            # ------------------------------------------
            # キーワードなし
            # ------------------------------------------

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

            result = [
                {
                    "url": img_url,
                    "text": text,
                    "page_url": page_url
                }
            ]

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
                    reason=(
                        "result_limit_reached"
                    )
                )

            return result

        # ----------------------------------------------
        # OCRエラー
        # ----------------------------------------------

        def _on_error(failure):

            self.logger.error(
                f"★★★ OCRエラー ★★★ "
                f"{failure}"
            )

            return []

        d.addCallback(
            _on_ocr
        )

        d.addErrback(
            _on_error
        )

        return d
        
        
