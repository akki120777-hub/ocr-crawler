from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import subprocess
import uuid
import os
import json
import asyncio


app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates")
)


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )


# Scrapyプロジェクトの場所
SCRAPY_DIR = os.path.join(
    BASE_DIR,
    "spocr",
    "spocr"
)


@app.get("/crawl")
async def crawl(
    url: str = Query(...),
    keyword: str = Query(""),
    limit: int = Query(5)
):
    """
    Scrapyクローラーを実行して結果を返す
    """

    # Scrapyが出力するJSONファイル
    output_file = f"output_{uuid.uuid4()}.json"

    output_path = os.path.join(
        SCRAPY_DIR,
        output_file
    )

    # Scrapy実行コマンド
    cmd = [
        "scrapy",
        "crawl",
        "crawler",
        "-a",
        f"url={url}",
        "-a",
        f"keyword={keyword}",
        "-s",
        f"CLOSESPIDER_ITEMCOUNT={limit}",
        "-O",
        output_path
    ]

    print("====================================")
    print("Scrapy開始")
    print(f"URL: {url}")
    print(f"Keyword: {keyword}")
    print(f"Limit: {limit}")
    print(f"Scrapy directory: {SCRAPY_DIR}")
    print(f"Output: {output_path}")
    print("====================================")

    def run_scrapy_and_read_result():

        try:

            # ★重要
            # capture_output=Trueを使わず、
            # ScrapyのログをRenderへ直接表示する
            subprocess.run(
                cmd,
                check=True,
                cwd=SCRAPY_DIR,
                timeout=180
            )

        except subprocess.CalledProcessError as e:

            error_msg = (
                f"Scrapy failed "
                f"(Exit Code: {e.returncode})"
            )

            raise Exception(error_msg)

        except subprocess.TimeoutExpired:

            raise Exception(
                "Scrapy execution timed out."
            )

        # --------------------------------
        # JSONファイル確認
        # --------------------------------

        if os.path.exists(output_path):

            print(
                f"Scrapy結果ファイル発見: {output_path}"
            )

            with open(
                output_path,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            # 読み終わったら削除
            os.remove(output_path)

            print(
                f"Scrapy完了。結果件数: {len(data)}"
            )

            return {
                "results": data
            }

        else:

            raise Exception(
                "Scrapy completed, "
                "but output file was not generated."
            )

    try:

        result = await asyncio.to_thread(
            run_scrapy_and_read_result
        )

        return result

    except Exception as e:

        if os.path.exists(output_path):

            os.remove(output_path)

        print(
            f"Scrapyエラー: {str(e)}"
        )

        return JSONResponse(
            content={
                "error": "Scrapy failed",
                "details": str(e)
            },
            status_code=500
        )
