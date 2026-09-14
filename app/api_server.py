from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, HTMLResponse
import subprocess
import uuid
import os
import json
import asyncio


app = FastAPI()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------
# トップページ
# ----------------------------------------

@app.get("/", response_class=HTMLResponse)
def read_root():

    index_path = os.path.join(
        BASE_DIR,
        "templates",
        "index.html"
    )

    try:

        with open(
            index_path,
            "r",
            encoding="utf-8"
        ) as f:

            html = f.read()

        return HTMLResponse(content=html)

    except Exception as e:

        return HTMLResponse(
            content=f"""
            <h1>エラー</h1>
            <p>index.htmlを読み込めませんでした。</p>
            <p>{str(e)}</p>
            """,
            status_code=500
        )


# ----------------------------------------
# Scrapyプロジェクトの場所
# ----------------------------------------

SCRAPY_DIR = os.path.join(
    BASE_DIR,
    "spocr",
    "spocr"
)


# ----------------------------------------
# /crawl
# ----------------------------------------

@app.get("/crawl")
async def crawl(
    url: str = Query(...),
    keyword: str = Query(""),
    limit: int = Query(5)
):

    """
    Scrapyクローラーを実行して結果を返す
    """

    # ----------------------------------------
    # 出力JSONファイル
    # ----------------------------------------

    output_file = f"output_{uuid.uuid4()}.json"

    output_path = os.path.join(
        SCRAPY_DIR,
        output_file
    )


    # ----------------------------------------
    # Scrapyコマンド
    # ----------------------------------------

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


    # ----------------------------------------
    # Scrapy実行
    # ----------------------------------------

    def run_scrapy_and_read_result():

        try:

            subprocess.run(
                cmd,
                check=True,
                cwd=SCRAPY_DIR,
                timeout=180
            )

        except subprocess.CalledProcessError as e:

            raise Exception(
                f"Scrapy failed "
                f"(Exit Code: {e.returncode})"
            )

        except subprocess.TimeoutExpired:

            raise Exception(
                "Scrapy execution timed out."
            )


        # ----------------------------------------
        # JSONファイル確認
        # ----------------------------------------

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


            # JSON削除
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


    # ----------------------------------------
    # 非同期でScrapy実行
    # ----------------------------------------

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
