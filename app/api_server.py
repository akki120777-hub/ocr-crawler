import asyncio
import json
import os
import signal
import sys
import uuid

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates


# ==========================================================
# FastAPI
# ==========================================================

app = FastAPI()


# ==========================================================
# パス
# ==========================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

templates = Jinja2Templates(
    directory=os.path.join(
        BASE_DIR,
        "templates"
    )
)

# Scrapyプロジェクト
SCRAPY_DIR = os.path.join(
    BASE_DIR,
    "spocr",
    "spocr"
)


# ==========================================================
# トップページ
# ==========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def read_root(
    request: Request
):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


# ==========================================================
# プロセス終了
# ==========================================================

async def terminate_process(
    process
):

    if process.returncode is not None:
        return

    print(
        "===================================="
    )

    print(
        "★★★ Scrapyプロセスを終了します ★★★"
    )

    try:

        if os.name == "nt":

            process.terminate()

        else:

            # --------------------------------------------------
            # Unix系
            #
            # RenderはLinuxなのでこちらが基本
            # --------------------------------------------------

            try:

                os.killpg(
                    process.pid,
                    signal.SIGTERM
                )

            except ProcessLookupError:

                pass

    except Exception as e:

        print(
            f"プロセス終了処理エラー: {e}"
        )

    # ----------------------------------------------------------
    # Graceful shutdown待機
    # ----------------------------------------------------------

    try:

        await asyncio.wait_for(
            process.wait(),
            timeout=10
        )

        return

    except asyncio.TimeoutError:

        print(
            "Scrapyが終了しないため強制終了します"
        )

    # ----------------------------------------------------------
    # 強制終了
    # ----------------------------------------------------------

    try:

        if os.name == "nt":

            process.kill()

        else:

            try:

                os.killpg(
                    process.pid,
                    signal.SIGKILL
                )

            except ProcessLookupError:

                pass

    except Exception as e:

        print(
            f"強制終了エラー: {e}"
        )

    try:

        await process.wait()

    except Exception:

        pass


# ==========================================================
# /crawl
# ==========================================================

@app.get("/crawl")
async def crawl(

    url: str = Query(
        ...,
        description="クロール開始URL"
    ),

    keyword: str = Query(
        "",
        description="OCR検索キーワード"
    ),

    limit: int = Query(
        5,
        ge=1,
        le=100,
        description="最大結果件数"
    ),

    max_pages: int = Query(
        30,
        ge=1,
        le=1000,
        description="最大ページ数"
    ),

    timeout_seconds: int = Query(
        300,
        ge=30,
        le=3600,
        description="Scrapy最大実行時間"
    ),
):

    # ========================================================
    # 入力チェック
    # ========================================================

    url = url.strip()

    keyword = keyword.strip()

    if not url.startswith(
        (
            "http://",
            "https://"
        )
    ):

        return JSONResponse(
            content={
                "error": "URLはhttp://またはhttps://で指定してください"
            },
            status_code=400
        )

    # ========================================================
    # 出力ファイル
    # ========================================================

    output_file = (
        f"output_{uuid.uuid4().hex}.json"
    )

    output_path = os.path.join(
        SCRAPY_DIR,
        output_file
    )

    # ========================================================
    # Scrapyコマンド
    # ========================================================

    cmd = [
        sys.executable,
        "-m",
        "scrapy",
        "crawl",
        "crawler",

        "-a",
        f"url={url}",

        "-a",
        f"keyword={keyword}",

        "-a",
        f"limit={limit}",

        "-a",
        f"max_pages={max_pages}",

        "-O",
        output_path,
    ]

    print(
        "===================================="
    )

    print(
        "★★★ Scrapy開始 ★★★"
    )

    print(
        f"URL={url}"
    )

    print(
        f"keyword={keyword}"
    )

    print(
        f"limit={limit}"
    )

    print(
        f"max_pages={max_pages}"
    )

    print(
        f"timeout_seconds={timeout_seconds}"
    )

    print(
        "===================================="
    )

    process = None

    try:

        # ====================================================
        # Scrapy起動
        #
        # stdoutとstderrをまとめて取得
        # ====================================================

        if os.name == "nt":

            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=SCRAPY_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        else:

            # Render/Linux
            # プロセスグループを作る
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=SCRAPY_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )

        # ====================================================
        # Scrapyログをリアルタイム表示
        # ====================================================

        async def read_output():

            while True:

                line = await process.stdout.readline()

                if not line:

                    break

                try:

                    decoded = line.decode(
                        "utf-8",
                        errors="replace"
                    ).rstrip()

                except Exception:

                    decoded = str(line)

                print(
                    f"[Scrapy] {decoded}"
                )

        output_task = asyncio.create_task(
            read_output()
        )

        # ====================================================
        # タイムアウト付きでScrapy終了を待つ
        # ====================================================

        try:

            await asyncio.wait_for(
                process.wait(),
                timeout=timeout_seconds
            )

        except asyncio.TimeoutError:

            print(
                "===================================="
            )

            print(
                "★★★ Scrapyタイムアウト ★★★"
            )

            print(
                f"{timeout_seconds}秒経過"
            )

            print(
                "===================================="
            )

            await terminate_process(
                process
            )

            await output_task

            return JSONResponse(
                content={
                    "error": "Scrapy execution timed out.",
                    "timeout_seconds": timeout_seconds,
                    "limit": limit,
                    "max_pages": max_pages,
                },
                status_code=504
            )

        # ====================================================
        # ログ読み取り完了
        # ====================================================

        await output_task

        # ====================================================
        # 終了コード確認
        # ====================================================

        return_code = process.returncode

        print(
            "===================================="
        )

        print(
            "★★★ Scrapy終了 ★★★"
        )

        print(
            f"return_code={return_code}"
        )

        print(
            "===================================="
        )

        if return_code != 0:

            return JSONResponse(
                content={
                    "error": "Scrapy failed",
                    "return_code": return_code,
                },
                status_code=500
            )

        # ====================================================
        # JSON確認
        # ====================================================

        if not os.path.exists(
            output_path
        ):

            return JSONResponse(
                content={
                    "error": (
                        "Scrapy completed, "
                        "but output file was not generated."
                    )
                },
                status_code=500
            )

        # ====================================================
        # JSON読み込み
        # ====================================================

        try:

            with open(
                output_path,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

        except Exception as e:

            return JSONResponse(
                content={
                    "error": "Output JSON could not be read.",
                    "details": str(e),
                },
                status_code=500
            )

        # ====================================================
        # データ形式確認
        # ====================================================

        if not isinstance(
            data,
            list
        ):

            return JSONResponse(
                content={
                    "error": (
                        "Scrapy output is not a JSON list."
                    )
                },
                status_code=500
            )

        # ====================================================
        # 最終的なlimitチェック
        #
        # ★重要★
        #
        # crawler側だけでなく、
        # FastAPI側でもlimitを保証する。
        # ====================================================

        if keyword:

            filtered = []

            for item in data:

                if not isinstance(
                    item,
                    dict
                ):
                    continue

                text = item.get(
                    "text",
                    ""
                )

                if not isinstance(
                    text,
                    str
                ):
                    continue

                if keyword in text:

                    filtered.append(
                        item
                    )

            data = filtered

        # ----------------------------------------------------
        # 最終limit
        # ----------------------------------------------------

        data = data[:limit]

        # ====================================================
        # レスポンス
        # ====================================================

        result = {
            "results": data,
            "meta": {
                "result_count": len(data),
                "limit": limit,
                "max_pages": max_pages,
                "timeout_seconds": timeout_seconds,
                "keyword": keyword,
            }
        }

        return JSONResponse(
            content=result
        )

    except Exception as e:

        print(
            "===================================="
        )

        print(
            "★★★ /crawl エラー ★★★"
        )

        print(
            repr(e)
        )

        print(
            "===================================="
        )

        # ----------------------------------------------------
        # 実行中なら終了
        # ----------------------------------------------------

        if process is not None:

            try:

                await terminate_process(
                    process
                )

            except Exception:

                pass

        return JSONResponse(
            content={
                "error": "Scrapy failed",
                "details": str(e),
            },
            status_code=500
        )

    finally:

        # ====================================================
        # 一時JSON削除
        # ====================================================

        try:

            if os.path.exists(
                output_path
            ):

                os.remove(
                    output_path
                )

        except Exception as e:

            print(
                f"一時ファイル削除失敗: {e}"
            )
