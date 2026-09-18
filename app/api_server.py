from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import subprocess
import uuid
import os
import json
import sys
import signal
import asyncio


app = FastAPI()

# ==================================================
# パス
# ==================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

templates = Jinja2Templates(
    directory=os.path.join(
        BASE_DIR,
        "templates"
    )
)

SCRAPY_DIR = os.path.join(
    BASE_DIR,
    "spocr",
    "spocr"
)


# ==================================================
# トップページ
# ==================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def read_root(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request
        }
    )


# ==================================================
# プロセス終了
# ==================================================

def terminate_process(
    process
):

    if process.poll() is not None:
        return

    try:

        if os.name == "nt":

            process.terminate()

        else:

            # Render/Linux
            # 子プロセスも含めて終了

            os.killpg(
                os.getpgid(
                    process.pid
                ),
                signal.SIGTERM
            )

    except Exception:

        try:
            process.terminate()
        except Exception:
            pass


# ==================================================
# Scrapy実行
# ==================================================

@app.get("/crawl")
async def crawl(
    url: str = Query(...),
    keyword: str = Query(""),
    limit: int = Query(5, ge=1),
    max_pages: int = Query(30, ge=1),
    timeout_seconds: int = Query(
        300,
        ge=10
    ),
):

    # ----------------------------------------------
    # URLチェック
    # ----------------------------------------------

    if not (
        url.startswith("http://")
        or
        url.startswith("https://")
    ):

        return JSONResponse(
            status_code=400,
            content={
                "error":
                    "URLはhttp://またはhttps://"
                    "で指定してください"
            }
        )

    # ----------------------------------------------
    # limit / max_pages
    # ----------------------------------------------

    limit = max(
        1,
        int(limit)
    )

    max_pages = max(
        1,
        int(max_pages)
    )

    # ----------------------------------------------
    # 出力ファイル
    # ----------------------------------------------

    output_file = (
        f"output_"
        f"{uuid.uuid4().hex}"
        f".json"
    )

    output_path = os.path.join(
        SCRAPY_DIR,
        output_file
    )

    # ----------------------------------------------
    # Scrapyコマンド
    # ----------------------------------------------

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

    process = None

    try:

        # ==========================================
        # Scrapy起動
        # ==========================================

        process = await asyncio.create_subprocess_exec(

            *cmd,

            cwd=SCRAPY_DIR,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,

            start_new_session=(
                os.name != "nt"
            ),
        )

        logs = []

        # ------------------------------------------
        # ログ読み取り
        # ------------------------------------------

        async def read_logs():

            while True:

                line = await process.stdout.readline()

                if not line:
                    break

                text = (
                    line
                    .decode(
                        "utf-8",
                        errors="replace"
                    )
                    .rstrip()
                )

                logs.append(text)

                # Renderログにも表示
                print(
                    f"[Scrapy] {text}",
                    flush=True
                )

        # ==========================================
        # Scrapy実行
        # ==========================================

        log_task = asyncio.create_task(
            read_logs()
        )

        try:

            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=timeout_seconds
            )

        except asyncio.TimeoutError:

            # --------------------------------------
            # タイムアウト
            # --------------------------------------

            terminate_process(
                process
            )

            try:

                await asyncio.wait_for(
                    process.wait(),
                    timeout=10
                )

            except asyncio.TimeoutError:

                try:

                    if os.name == "nt":
                        process.kill()

                    else:

                        os.killpg(
                            os.getpgid(
                                process.pid
                            ),
                            signal.SIGKILL
                        )

                except Exception:
                    pass

            try:

                await asyncio.wait_for(
                    log_task,
                    timeout=5
                )

            except asyncio.TimeoutError:

                log_task.cancel()

            return JSONResponse(
                status_code=504,
                content={
                    "error":
                        "Scrapy execution timed out.",
                    "timeout_seconds":
                        timeout_seconds,
                    "limit":
                        limit,
                    "max_pages":
                        max_pages,
                }
            )

        await log_task

        # ==========================================
        # Scrapy終了確認
        # ==========================================

        print(
            "====================================",
            flush=True
        )

        print(
            "★★★ Scrapy終了 ★★★",
            flush=True
        )

        print(
            f"return_code={return_code}",
            flush=True
        )

        print(
            "====================================",
            flush=True
        )

        # ------------------------------------------
        # プロセスエラー
        # ------------------------------------------

        if return_code != 0:

            # 後ろのログだけ返す
            error_logs = logs[-100:]

            return JSONResponse(
                status_code=500,
                content={
                    "error":
                        "Scrapy failed",
                    "return_code":
                        return_code,
                    "details":
                        "\n".join(error_logs),
                }
            )

        # ==========================================
        # JSON読み込み
        # ==========================================

        if not os.path.exists(
            output_path
        ):

            return JSONResponse(
                status_code=500,
                content={
                    "error":
                        "Scrapy completed, "
                        "but output file was "
                        "not generated."
                }
            )

        try:

            with open(
                output_path,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

        except Exception as e:

            return JSONResponse(
                status_code=500,
                content={
                    "error":
                        "JSON result could "
                        "not be read.",
                    "details":
                        str(e),
                }
            )

        # ==========================================
        # JSON形式確認
        # ==========================================

        if not isinstance(
            data,
            list
        ):

            return JSONResponse(
                status_code=500,
                content={
                    "error":
                        "Scrapy output is "
                        "not a list."
                }
            )

        # ==========================================
        # 最終limit適用
        #
        # 念のためAPI側でも制限
        # ==========================================

        if keyword:

            filtered_data = []

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

                if keyword in text:

                    filtered_data.append(
                        item
                    )

            data = filtered_data

        # 最終的に必ずlimit件以下
        data = data[:limit]

        # ==========================================
        # 結果
        # ==========================================

        return JSONResponse(
            status_code=200,
            content={
                "results": data,
                "count": len(data),
                "limit": limit,
                "max_pages": max_pages,
            }
        )

    except Exception as e:

        return JSONResponse(
            status_code=500,
            content={
                "error":
                    "Unexpected server error.",
                "details":
                    str(e),
            }
        )

    finally:

        # ==========================================
        # 出力ファイル削除
        # ==========================================

        try:

            if os.path.exists(
                output_path
            ):

                os.remove(
                    output_path
                )

        except Exception:

            pass
