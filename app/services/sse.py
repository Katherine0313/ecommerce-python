"""SSE 分帧：前端只认纯文本 data: 行，必须严格保持格式。

规则：
- 单个事件以空行结束，行尾固定 LF（不要 CRLF）
- 文本里的换行拆成多条 data: 行，前端会拼回 \n
- 不发送心跳注释、[DONE]、event: 名
"""


def sse_pack(text: str) -> str:
    """把一段增量文本编码成一个 SSE 事件。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    return "".join(f"data: {line}\n" for line in lines) + "\n"
