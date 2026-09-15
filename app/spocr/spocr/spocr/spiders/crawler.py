import scrapy
from urllib.parse import urlparse
import io
import os

from twisted.internet import threads

from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    MODEL_NAME = "gemini-2.5-flash"

    def __init__(
        self,
        url=None,
        keyword=None,
        domain=None,
        limit=5,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)

        # --------------------------------
        # URL
        # --------------------------------
        if not url:
            raise ValueError("URLを指定してください")

        self.start_urls = [url]

        # --------------------------------
        # キーワード
        # --------------------------------
        self.keyword = keyword or ""

        # --------------------------------
        # 検索結果の最大件数
        # --------------------------------
        try:
            self.limit = max(1, int(limit))
        except (ValueError, TypeError):
            self.limit = 5

        # 実際に検索結果として採用した件数
        self.result_count = 0

        # --------------------------------
        # Gemini API
        # --------------------------------
        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY が設定されていません"
            )

        self.gemini_client = client.Client(
            api_key=api_key
        )

        # --------------------------------
        # allowed_domains
        # --------------------------------
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
            "★★★ Spider開始 ★★★ "
            f"URL={url}, "
            f"keyword={keyword}, "
            f"limit={self.limit}, "
            f"domain={self.allowed_domains}"
        )

    # ==================================================
    # HTMLページ
    # ==================================================

    def parse(self, response):

        self.logger.info(
            f"★★★ parse開始 ★★★ URL={response.url}"
        )

        # --------------------------------
        # Content-Type確認
        # --------------------------------

        content_type = response.headers.get(
            "Content-Type",
            b""
        ).decode(
            "utf-8",
            errors="ignore"
        ).lower()

        self.logger.info(
            f"★★★ Content-Type: {content_type} ★★★"
        )

        if "text/html" not in content_type:

            self.logger.info(
                "★★★ HTMLではないためスキップ: "
                f"{response.url} ★★★"
            )

            return

        # --------------------------------
        # 画像取得
        # --------------------------------

        image_sources = response.css(
            "img::attr(src)"
        ).getall()

        self.logger.info(
            f"★★★ ページ内画像数: "
            f"{len(image_sources)} ★★★"
        )

        for img_src in image_sources:

            # SVGなどはOCR対象外
            if img_src.lower().split("?")[0].endswith(".svg"):
                self.logger.info(
                    f"★★★ SVG画像をスキップ ★★★ {img_src}"
                )
                continue

            img_url = response.urljoin(img_src)

            self.logger.info(
                f"★★★ 画像発見 ★★★ {img_url}"
            )

            yield scrapy.Request(
                img_url,
                callback=self.parse_image,
                meta={
                    "img_url": img_url,
                    "page_url": response.url
                }
            )

        # --------------------------------
        # ページ内リンク
        # --------------------------------

        links = response.css(
            "a::attr(href)"
        ).getall()

        self.logger.info(
            f"★★★ ページ内リンク数: "
            f"{len(links)} ★★★"
        )

        for href in links:

            parsed_href = urlparse(href)

            if (
                parsed_href.scheme in (
                    "http",
                    "https",
                    ""
                )
                and not parsed_href.path.startswith("#")
            ):

                next_url = response.urljoin(href)

                self.logger.info(
                    f"★★★ 次のページへ: "
                    f"{next_url} ★★★"
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

    # ==================================================
    # Gemini OCR
    # ==================================================

    def _ocr_bytes(self, img_bytes):

        # --------------------------------
        # 画像をPILで読み込む
        # --------------------------------

        try:

            image = Image.open(
                io.BytesIO(img_bytes)
            )

            image.load()

        except Exception as e:

            return f"ERROR_IMAGE:{e}"

        # --------------------------------
        # Geminiへの指示
        # --------------------------------

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

        # --------------------------------
        # Gemini API
        # --------------------------------

        try:

            self.logger.info(
                "★★★ Gemini API呼び出し ★★★"
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

            if response and response.text:

                text = response.text.strip()

                # --------------------------------
                # NO_TEXT判定
                # --------------------------------

                if text.upper() == "NO_TEXT":

                    return "NO_TEXT"

                return text

            return "NO_TEXT"

        except Exception as e:

            return f"ERROR_GEMINI:{e}"

    # ==================================================
    # 画像処理
    # ==================================================

    def parse_image(self, response):

        content_type = response.headers.get(
            "Content-Type",
            b""
        ).decode(
            "utf-8",
            errors="ignore"
        ).lower()

        img_url = response.meta.get(
            "img_url",
            response.url
        )

        page_url = response.meta.get(
            "page_url",
            ""
        )

        self.logger.info(
            f"★★★ 画像取得 ★★★ {img_url}"
        )

        self.logger.info(
            f"★★★ 画像Content-Type: "
            f"{content_type} ★★★"
        )

        # --------------------------------
        # 本当に画像か確認
        # --------------------------------

        if not content_type.startswith("image/"):

            self.logger.info(
                "★★★ 画像ではないためスキップ ★★★ "
                f"{img_url}"
            )

            return

        # --------------------------------
        # OCR開始
        # --------------------------------

        self.logger.info(
            f"★★★ Gemini OCR開始 ★★★ {img_url}"
        )

        d = threads.deferToThread(
            self._ocr_bytes,
            response.body
        )

        # ==================================================
        # OCR成功時
        # ==================================================

        def _on_ocr(text):

            self.logger.info(
                f"★★★ Gemini OCR完了 ★★★ {img_url}"
            )

            # --------------------------------
            # 画像読み込みエラー
            # --------------------------------

            if text.startswith("ERROR_IMAGE:"):

                self.logger.warning(
                    f"画像読み込みエラー: {text}"
                )

                return []

            # --------------------------------
            # Gemini APIエラー
            # --------------------------------

            if text.startswith("ERROR_GEMINI:"):

                self.logger.error(
                    f"Gemini APIエラー: {text}"
                )

                return []

            # --------------------------------
            # 日本語テキストなし
            # --------------------------------

            if text == "NO_TEXT":

                self.logger.info(
                    "★★★ 日本語テキストなし ★★★ "
                    f"{img_url}"
                )

                return []

            # --------------------------------
            # OCR結果
            # --------------------------------

            self.logger.info(
                f"★★★ OCR結果 ★★★ "
                f"{text[:300]}"
            )

            # ==================================================
            # limitに達していたら結果に追加しない
            # ==================================================

            if self.result_count >= self.limit:

                self.logger.info(
                    "★★★ 結果件数がlimitに到達済み "
                    "→ スキップ ★★★"
                )

                return []

            # ==================================================
            # キーワード検索
            # ==================================================

            if self.keyword:

                if self.keyword in text:

                    # --------------------------------
                    # 結果件数を1件増やす
                    # --------------------------------

                    self.result_count += 1

                    self.logger.info(
                        "★★★ キーワード一致 ★★★ "
                        f"「{self.keyword}」 "
                        f"→ {img_url}"
                    )

                    self.logger.info(
                        "★★★ 現在の結果件数 ★★★ "
                        f"{self.result_count}/{self.limit}"
                    )

                    result = [{
                        "url": img_url,
                        "text": text,
                        "page_url": page_url
                    }]

                    # --------------------------------
                    # limit到達
                    # --------------------------------

                    if self.result_count >= self.limit:

                        self.logger.info(
                            "★★★ limit到達 ★★★ "
                            f"{self.result_count}件"
                        )

                        self.crawler.engine.close_spider(
                            self,
                            reason="result_limit_reached"
                        )

                    return result

                else:

                    self.logger.info(
                        "★★★ キーワード不一致 ★★★ "
                        f"「{self.keyword}」 "
                        f"→ {img_url}"
                    )

                    return []

            # ==================================================
            # キーワードなし
            # ==================================================

            else:

                self.result_count += 1

                self.logger.info(
                    "★★★ キーワードなし "
                    "→ 結果として返す ★★★"
                )

                self.logger.info(
                    "★★★ 現在の結果件数 ★★★ "
                    f"{self.result_count}/{self.limit}"
                )

                result = [{
                    "url": img_url,
                    "text": text,
                    "page_url": page_url
                }]

                if self.result_count >= self.limit:

                    self.logger.info(
                        "★★★ limit到達 ★★★ "
                        f"{self.result_count}件"
                    )

                    self.crawler.engine.close_spider(
                        self,
                        reason="result_limit_reached"
                    )

                return result

        # ==================================================
        # OCR失敗
        # ==================================================

        def _on_error(failure):

            self.logger.error(
                f"★★★ OCRエラー ★★★ {failure}"
            )

            return []

        d.addCallback(_on_ocr)
        d.addErrback(_on_error)

        return d
        
        
