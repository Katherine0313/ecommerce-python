"""本地启动入口：在项目根目录执行 `python main.py` 即可。

生产环境由 Dockerfile 直接跑 uvicorn，不经过这个文件。
"""

import uvicorn

if __name__ == "__main__":
    # reload 方便本地改代码后自动重启；生产不要用它
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
