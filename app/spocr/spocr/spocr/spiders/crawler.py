import scrapy
from urllib.parse import urlparse
import io
import os
from twisted.internet import threads
from google.genai import client
from PIL import Image


class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    # 使用するGeminiモデル
    MODEL_NAME = "gemini-2.5-flash"

    def __init__(self, url=None, keyword=None, domain=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not url:
            raise ValueError("URLを指定してください")

        self.start_urls = [url]
        self.keyword = keyword

        # Gemini APIキーを環境変数から取得
        api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            raise ValueError("GEMINI_API_KEY が設定されていません")

        self.gemini_client = client.Client(api_key=api_key)

        # domainが指定されていれば優先
        # 指定されていなければ開始URLから自動取得
        if domain is not None:
            if domain == "":
                self.allowed_domains = []
            else:
                self.allowed_domains = [domain]
        else:
            parsed = urlparse(url)

            if parsed.hostname:
                self.allowed_domains = [parsed.hostname]
            else:
                self.allowed_domains = []

        self.logger.info(
            f"★★★ Spider開始 ★★★ URL={url}, keyword={keyword}, domain={self.allowed_domains}"
        )

    def parse(self, response):
        """
        HTMLページから画像を取得し、
        ページ内リンクも辿る。
        """

        # ★ Renderでどこまで進んでいるか確認するためのログ
        self.logger.info(
            f"★★★ parse開始 ★★★ URL={response.url}"
        )

        # Content-Typeを確認
        content_type = response.headers.get(
            "Content-Type", b""
        ).decode(
            "utf-8",
            errors="ignore"
        ).lower()

        self.logger.info(
            f"★★★ Content-Type: {content_type} ★★★"
        )

        # HTML以外をスキップ
        if "text/html" not in content_type:
            self.logger.info(
                f"★★★ HTMLではないためスキップ: {response.url} ★★★"
            )
            return

        # --------------------------------
        # ページ内の画像を取得
        # --------------------------------

        image_sources = response.css("img::attr(src)").getall()

        self.logger.info(
            f"★★★ ページ内画像数: {len(image_sources)} ★★★"
        )

        for img_src in image_sources:

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
        # ページ内リンクを辿る
        # --------------------------------

        links = response.css("a::attr(href)").getall()

        self.logger.info(
            f"★★★ ページ内リンク数: {len(links)} ★★★"
        )

        for href in links:

            parsed_href = urlparse(href)

            # http / https / 相対URLのみ処理
            if (
                parsed_href.scheme in ("http", "https", "")
                and not parsed_href.path.startswith("#")
            ):

                next_url = response.urljoin(href)

                self.logger.info(
                    f"★★★ 次のページへ: {next_url} ★★★"
                )

                yield response.follow(
                    href,
                    callback=self.parse
                )

            elif parsed_href.scheme in ("tel", "mailto"):

                self.logger.debug(
                    f"tel/mailtoをスキップ: {href}"
                )

    def _ocr_bytes(self, img_bytes):
        """
        Gemini APIを使って画像内の日本語テキストをOCRする。
        """

        # --------------------------------
        # 画像をPILで読み込む
        # --------------------------------

        try:

            image = Image.open(
                io.BytesIO(img_bytes)
            )

            image.load()

        except Exception as e:

            return f"Error loading image: {e}"

        # --------------------------------
        # Geminiへのプロンプト
        # --------------------------------

        prompt_text = (
            "この画像に含まれる日本語テキストを正確に抽出してください。"
            "ただし、画像を説明するテキストは不要です。"
            "抽出したテキストのみを返してください。"
        )

        # --------------------------------
        # Gemini API呼び出し
        # --------------------------------

        try:

            self.logger.info(
                "★★★ Gemini API呼び出し ★★★"
            )

            response = self.gemini_client.models.generate_content(
                model=self.MODEL_NAME,
                contents=[
                    prompt_text,
                    image
                ]
            )

            self.logger.info(
                "★★★ Gemini API応答受信 ★★★"
            )

            # OCR結果
            if response and response.text:

                return response.text.strip()

            return ""

        except Exception as e:

            return f"Gemini API error: {e}"

    def parse_image(self, response):
        """
        取得した画像をGemini OCRにかける。
        """

        # --------------------------------
        # 画像か確認
        # --------------------------------

        content_type = response.headers.get(
            "Content-Type", b""
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
            f"★★★ 画像Content-Type: {content_type} ★★★"
        )

        if not content_type.startswith("image/"):

            self.logger.info(
                f"★★★ 画像ではないためスキップ ★★★ {img_url}"
            )

            return

        # --------------------------------
        # OCR開始
        # --------------------------------

        self.logger.info(
            f"★★★ Gemini OCR開始 ★★★ {img_url}"
        )

        # Gemini APIは同期処理なので別スレッドで実行
        d = threads.deferToThread(
            self._ocr_bytes,
            response.body
        )

        # --------------------------------
        # OCR成功時
        # --------------------------------

        def _on_ocr(text):

            self.logger.info(
                f"★★★ Gemini OCR完了 ★★★ {img_url}"
            )

            # 画像読み込みエラー
            if text.startswith(
                "Error loading image:"
            ):

                self.logger.warning(
                    f"画像読み込みエラー: {text}"
                )

                return []

            # Gemini APIエラー
            if text.startswith(
                "Gemini API error:"
            ):

                self.logger.error(
                    f"Gemini APIエラー: {text}"
                )

                return []

            # OCR結果をログに出す
            self.logger.info(
                f"★★★ OCR結果 ★★★ {text[:300]}"
            )

            # --------------------------------
            # キーワード検索
            # --------------------------------

            if self.keyword:

                if self.keyword in text:

                    self.logger.info(
                        f"★★★ キーワード一致 ★★★ "
                        f"「{self.keyword}」 → {img_url}"
                    )

                    return [
                        {
                            "url": img_url,
                            "text": text,
                            "page_url": page_url
                        }
                    ]

                else:

                    self.logger.info(
                        f"★★★ キーワード不一致 ★★★ "
                        f"「{self.keyword}」 → {img_url}"
                    )

                    return []

            # キーワード空欄の場合
            else:

                self.logger.info(
                    f"★★★ キーワードなし → 結果として返す ★★★"
                )

                return [
                    {
                        "url": img_url,
                        "text": text,
                        "page_url": page_url
                    }
                ]

        # --------------------------------
        # OCR失敗時
        # --------------------------------

        def _on_error(failure):

            self.logger.error(
                f"★★★ OCRエラー ★★★ {failure}"
            )

            return []

        # Deferredに処理を登録
        d.addCallback(_on_ocr)
        d.addErrback(_on_error)

        return d
        
        
