from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, HTMLResponse

import subprocess
import uuid
import os
import json
import asyncio
import sys
import signal


app = FastAPI()


# ==================================================
# 基本ディレクトリ
# ==================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)


# ==================================================
# トップページ
# ==================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
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

        return HTMLResponse(
            content=html
        )

    except Exception as e:

        return HTMLResponse(

            content=f"""
            <h1>エラー</h1>

            <p>
                index.htmlを読み込めませんでした。
            </p>

            <p>
                {str(e)}
            </p>
            """,

            status_code=500
        )


# ==================================================
# Scrapyプロジェクト
# ==================================================

SCRAPY_DIR = os.path.join(
    BASE_DIR,
    "spocr",
    "spocr"
)


# ==================================================
# クロールAPI
# ==================================================

@app.get("/crawl")
async def crawl(

    # ------------------------------------------------
    # 検索対象URL
    # ------------------------------------------------

    url: str = Query(...),

    # ------------------------------------------------
    # OCRキーワード
    # ------------------------------------------------

    keyword: str = Query(""),

    # ------------------------------------------------
    # 検索結果最大件数
    # ------------------------------------------------

    limit: int = Query(
        5,
        ge=1,
        le=20
    ),

    # ------------------------------------------------
    # 最大ページ数
    # ------------------------------------------------

    max_pages: int = Query(
        30,
        ge=1,
        le=100
    ),

    # ------------------------------------------------
    # タイムアウト
    # ------------------------------------------------

    timeout_seconds: int = Query(
        600,
        ge=30,
        le=3600
    )
):

    # ==================================================
    # URLチェック
    # ==================================================

    if not url.startswith(
        (
            "http://",
            "https://"
        )
    ):

        return JSONResponse(

            content={
                "error":
                    "URLはhttp://またはhttps://"
                    "から始めてください"
            },

            status_code=400
        )

    # ==================================================
    # 出力ファイル
    # ==================================================

    output_file = (
        f"output_{uuid.uuid4()}.json"
    )

    output_path = os.path.join(
        SCRAPY_DIR,
        output_file
    )

    # ==================================================
    # Scrapyコマンド
    # ==================================================

    cmd = [

        sys.executable,

        "-m",

        "scrapy",

        "crawl",

        "crawler",

        # URL
        "-a",
        f"url={url}",

        # キーワード
        "-a",
        f"keyword={keyword}",

        # 結果件数
        "-a",
        f"limit={limit}",

        # 最大ページ
        "-a",
        f"max_pages={max_pages}",

        # JSON
        "-O",

        output_path
    ]

    # ==================================================
    # ログ
    # ==================================================

    print(
        "===================================="
    )

    print(
        "Scrapy開始"
    )

    print(
        f"URL: {url}"
    )

    print(
        f"Keyword: {keyword}"
    )

    print(
        f"Limit: {limit}"
    )

    print(
        f"Max pages: {max_pages}"
    )

    print(
        f"Timeout: {timeout_seconds}秒"
    )

    print(
        f"Scrapy directory: "
        f"{SCRAPY_DIR}"
    )

    print(
        f"Output: {output_path}"
    )

    print(
        "===================================="
    )

    # ==================================================
    # 既存ファイル削除
    # ==================================================

    if os.path.exists(
        output_path
    ):

        try:

            os.remove(
                output_path
            )

        except Exception:

            pass

    process = None

    try:

        # ==================================================
        # Scrapy起動
        # ==================================================

        process = await asyncio.create_subprocess_exec(

            *cmd,

            cwd=SCRAPY_DIR,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.STDOUT,

            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1"
            },

            # Render/Linuxでプロセスグループを作る
            start_new_session=True
        )

        # ==================================================
        # Scrapyログを読む
        # ==================================================

        async def read_output():

            while True:

                line = (
                    await process.stdout.readline()
                )

                if not line:

                    break

                try:

                    text = line.decode(
                        "utf-8",
                        errors="replace"
                    ).rstrip()

                    print(
                        f"[Scrapy] {text}"
                    )

                except Exception:

                    pass

        # ==================================================
        # ログ読み込み開始
        # ==================================================

        log_task = asyncio.create_task(
            read_output()
        )

        # ==================================================
        # タイムアウト付き待機
        # ==================================================

        try:

            return_code = await asyncio.wait_for(

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
                "Scrapyプロセスを終了します"
            )

            print(
                "===================================="
            )

            # ------------------------------------------------
            # プロセスグループ終了
            # ------------------------------------------------

            try:

                os.killpg(
                    process.pid,
                    signal.SIGTERM
                )

            except Exception:

                try:

                    process.terminate()

                except Exception:

                    pass

            # ------------------------------------------------
            # 少し待つ
            # ------------------------------------------------

            try:

                await asyncio.wait_for(
                    process.wait(),
                    timeout=5
                )

            except asyncio.TimeoutError:

                print(
                    "Scrapyが終了しないため強制終了します"
                )

                try:

                    os.killpg(
                        process.pid,
                        signal.SIGKILL
                    )

                except Exception:

                    try:

                        process.kill()

                    except Exception:

                        pass

                await process.wait()

            # ------------------------------------------------
            # ログタスク終了
            # ------------------------------------------------

            try:

                await asyncio.wait_for(
                    log_task,
                    timeout=5
                )

            except Exception:

                pass

            # ------------------------------------------------
            # タイムアウトとして返す
            # ------------------------------------------------

            return JSONResponse(

                content={

                    "error":
                        "Scrapy timeout",

                    "details":
                        f"指定した{timeout_seconds}秒以内に"
                        "検索が完了しませんでした。",

                    "timeout_seconds":
                        timeout_seconds
                },

                status_code=504
            )

        # ==================================================
        # ログ読み込み完了
        # ==================================================

        try:

            await asyncio.wait_for(
                log_task,
                timeout=10
            )

        except Exception:

            pass

        # ==================================================
        # Scrapy終了確認
        # ==================================================

        print(
            "Scrapy終了"
            f" Exit Code={return_code}"
        )

        # ==================================================
        # 結果ファイル確認
        # ==================================================

        if not os.path.exists(
            output_path
        ):

            return JSONResponse(

                content={

                    "error":
                        "Scrapy failed",

                    "details":
                        "Scrapyは終了しましたが、"
                        "結果JSONが生成されませんでした。",

                    "exit_code":
                        return_code
                },

                status_code=500
            )

        # ==================================================
        # JSON読み込み
        # ==================================================

        try:

            with open(
                output_path,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

        except json.JSONDecodeError as e:

            return JSONResponse(

                content={

                    "error":
                        "Invalid JSON",

                    "details":
                        str(e),

                    "exit_code":
                        return_code
                },

                status_code=500
            )

        # ==================================================
        # 配列確認
        # ==================================================

        if not isinstance(
            data,
            list
        ):

            data = []

        # ==================================================
        # キーワードの最終確認
        # ==================================================

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

        # ==================================================
        # limitの最終防衛
        #
        # Spider側でもlimit制御しているが、
        # API側でも念のため制限する。
        # ==================================================

        data = data[
            :limit
        ]

        # ==================================================
        # JSON削除
        # ==================================================

        try:

            os.remove(
                output_path
            )

        except Exception:

            pass

        # ==================================================
        # 結果
        # ==================================================

        print(
            "===================================="
        )

        print(
            "Scrapy完了"
        )

        print(
            f"結果件数: {len(data)}"
        )

        print(
            "===================================="
        )

        return {

            "results":
                data,

            "meta": {

                "limit":
                    limit,

                "max_pages":
                    max_pages,

                "timeout_seconds":
                    timeout_seconds
            }
        }

    # ==================================================
    # Scrapy実行エラー
    # ==================================================

    except Exception as e:

        print(
            "===================================="
        )

        print(
            "★★★ Scrapyエラー ★★★"
        )

        print(
            str(e)
        )

        print(
            "===================================="
        )

        # ------------------------------------------------
        # プロセスが残っていたら終了
        # ------------------------------------------------

        if process is not None:

            try:

                if process.returncode is None:

                    try:

                        os.killpg(
                            process.pid,
                            signal.SIGTERM
                        )

                    except Exception:

                        process.terminate()

                    try:

                        await asyncio.wait_for(
                            process.wait(),
                            timeout=5
                        )

                    except asyncio.TimeoutError:

                        try:

                            os.killpg(
                                process.pid,
                                signal.SIGKILL
                            )

                        except Exception:

                            process.kill()

                        await process.wait()

            except Exception:

                pass

        return JSONResponse(

            content={

                "error":
                    "Scrapy failed",

                "details":
                    str(e)
            },

            status_code=500
        )

    finally:

        # ==================================================
        # 一時JSONが残っていたら削除
        # ==================================================

        if os.path.exists(
            output_path
        ):

            try:

                os.remove(
                    output_path
                )

            except Exception:

                pass
