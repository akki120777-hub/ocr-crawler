import scrapy
from urllib.parse import urlparse
import io
import os
from twisted.internet import threads
from google.genai import client
from google.genai import types
from PIL import Image

class CrawlerSpider(scrapy.Spider):
    name = "crawler"

    # 使用するモデルを定数として定義 (gemini-1.5-flashが404だったため、2.5 flashを試行)
    # NOTE: 環境に合わせて 'gemini-2.5-flash' や 'gemini-pro' に変更してください。
    MODEL_NAME = "gemini-2.5-flash"

    def __init__(self, url=None, keyword=None, domain=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not url:
            raise ValueError("URLを指定してください")

        self.start_urls = [url]
        self.keyword = keyword

        # 最新 SDK 対応：Client 作成時に API キーを渡す (環境変数から取得)
        self.gemini_client = client.Client(api_key=os.environ.get("GEMINI_API_KEY"))

        # domain が指定されていれば優先、なければ url から自動抽出
        if domain is not None:
            if domain == "":
                self.allowed_domains = []
            else:
                self.allowed_domains = [domain]
        else:
            parsed = urlparse(url)
            self.allowed_domains = [parsed.netloc] if parsed.netloc else []

    def parse(self, response):
        # 1. PDFなどの非テキストコンテンツをスキップ
        content_type = response.headers.get('Content-Type', b'').decode('utf-8').lower()
        if 'text/html' not in content_type:
            self.logger.info(f"Skipping non-HTML content: {response.url} (Type: {content_type})")
            return

        # ページ内の画像を取得
        for img_src in response.css("img::attr(src)").getall():
            img_url = response.urljoin(img_src)
            yield scrapy.Request(
                img_url,
                callback=self.parse_image,
                meta={"img_url": img_url, "page_url": response.url}
            )

        # ページ内リンクを辿る
        for href in response.css("a::attr(href)").getall():
            # 2. Webスキーム（http, https）を持つリンクのみを処理
            parsed_href = urlparse(href)
            if parsed_href.scheme in ('http', 'https', '') and not parsed_href.path.startswith('#'):
                yield response.follow(href, callback=self.parse)
            elif parsed_href.scheme in ('tel', 'mailto'):
                self.logger.debug(f"Skipping non-web scheme URL: {href}")
            # 'tel:', 'mailto:', '#' などの非ウェブリンクは無視

    def _ocr_bytes(self, img_bytes):
        """Gemini API を使った OCR（最新 SDK 対応）"""
        
        # 画像データを PIL Image オブジェクトに変換
        try:
            image_part = Image.open(io.BytesIO(img_bytes))
        except Exception as e:
            # 画像でない、または壊れている場合はスキップ
            return f"Error loading image: {e}"

        # 3. 最新 SDK 形式で画像を渡し、生成を行う
        prompt_text = "この画像に含まれる日本語テキストを正確に抽出してください。ただし、画像を説明するテキストは不要で、抽出したテキストのみを返してください。"

        # types.Part を使用してマルチモーダル入力を構成
        parts = [
            prompt_text,
            image_part
        ]

        response = self.gemini_client.models.generate_content(
            model=self.MODEL_NAME,  # 修正したモデル名を使用
            contents=parts
        )
        
        # 結果を返す
        return response.text.strip() if response and response.text else ""

    def parse_image(self, response):
        """画像を OCR してアイテムを返す"""
        
        # レスポンスが画像であるか確認（厳密ではないが、一般的なチェック）
        content_type = response.headers.get('Content-Type', b'').decode('utf-8').lower()
        if not content_type.startswith('image/'):
            self.logger.debug(f"Skipping non-image content in parse_image: {response.url} (Type: {content_type})")
            return

        img_url = response.meta.get("img_url", response.url)
        
        # スレッドプールで _ocr_bytes を実行
        d = threads.deferToThread(self._ocr_bytes, response.body)

        def _on_ocr(text):
            # OCRエラーメッセージが含まれていないかチェック
            if text.startswith("Error loading image:"):
                self.logger.warning(f"Failed to OCR image {img_url}: {text}")
                return []
                
            if self.keyword:
                if self.keyword in text:
                    return [{"url": img_url, "text": text, "page_url": response.meta.get("page_url")}]
                else:
                    return []
            else:
                return [{"url": img_url, "text": text, "page_url": response.meta.get("page_url")}]

        def _on_error(failure):
            self.logger.error(f"OCR error: {failure}")
            return []

        d.addCallback(_on_ocr)
        d.addErrback(_on_error)
        return d
