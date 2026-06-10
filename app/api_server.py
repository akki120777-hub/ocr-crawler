from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import subprocess
import uuid
import os
import json
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import asyncio  # 非同期処理のために追加

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Scrapy プロジェクトの相対パス
SCRAPY_DIR = os.path.join(BASE_DIR, "spocr", "spocr")

# 修正: エンドポイントを非同期 (async def) に変更
@app.get("/crawl")
async def crawl(url: str = Query(...), keyword: str = Query(""), limit: int = Query(5)):
    """Scrapy クローラーを非同期的に実行し、結果を返す"""
    # Scrapy が保存する JSON の絶対パス
    output_file = f"output_{uuid.uuid4()}.json"
    output_path = os.path.join(SCRAPY_DIR, output_file)

    # Scrapy を実行するためのコマンド
    cmd = [
        "scrapy", "crawl", "crawler",
        "-a", f"url={url}",
        "-a", f"keyword={keyword}",
        "-s", f"CLOSESPIDER_ITEMCOUNT={limit}",
        "-O", output_path
    ]

    def run_scrapy_and_read_result():
        """同期的なScrapy実行とファイルI/Oを行う関数。別スレッドで実行される。"""
        try:
            # subprocess.run は同期的なI/Oブロッキング処理
            # capture_output=True, text=True はエラー時の詳細ログ取得に役立ちます
            subprocess.run(
                cmd,
                check=True,
                cwd=SCRAPY_DIR,
                capture_output=True,
                text=True,
                timeout=180 # クローリングの最大実行時間を設定することを推奨
            )
        except subprocess.CalledProcessError as e:
            # Scrapyが失敗した場合、詳細なエラーメッセージを例外として送出
            error_msg = f"Scrapy failed (Exit Code: {e.returncode}).\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}"
            raise Exception(error_msg)
        except subprocess.TimeoutExpired:
            raise Exception("Scrapy execution timed out.")

        # JSON の読み込みと削除 (これも同期的なI/O)
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(output_path)
            return {"results": data}
        else:
            # Scrapyが正常終了してもファイルがない場合はエラーとして扱う
            raise Exception("Scrapy completed, but output file was not generated.")


    try:
        # asyncio.to_thread を使用して、ブロッキング処理をワーカープロセスに委譲し、
        # メインイベントループ（FastAPI）をブロックしないようにする
        result = await asyncio.to_thread(run_scrapy_and_read_result)
        return result
    except Exception as e:
        # エラーが発生した場合、生成された可能性のあるファイルをクリーンアップ
        if os.path.exists(output_path):
             os.remove(output_path)
        # 詳細なエラーメッセージをクライアントに返す
        return JSONResponse(content={"error": "Scrapy failed", "details": str(e)}, status_code=500)

