"""
模块名称: feishu.py
说明:    飞书群机器人通知（webhook 写死，不随环境变更）
"""
import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

# 写死，不随环境变更
WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/f91e7e9b-05f8-44a3-8b02-3ffd8c488f3a"


def send(payload: dict) -> dict:
    """发送消息到飞书群，返回响应"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("飞书消息发送失败: %s", e)
        return {"StatusCode": -1, "StatusMessage": str(e)}


def send_text(text: str, title: str = "") -> dict:
    """发送纯文本消息"""
    if title:
        text = f"{title}\n{text}"
    return send({"msg_type": "text", "content": {"text": text}})


def send_card(title: str, body: str, color: str = "blue") -> dict:
    """发送卡片消息

    color: blue/red/green/grey/purple/orange
    """
    color_map = {
        "blue": "blue", "red": "red", "green": "green",
        "grey": "grey", "purple": "purple", "orange": "orange",
    }
    theme = color_map.get(color, "blue")

    # 按行拆分，每行一个元素
    lines = body.strip().split("\n")
    elements = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})

    return send(
        {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": theme,
                },
                "elements": elements,
            },
        }
    )


def send_heartbeat_alert(agent_name: str, last_heartbeat: str, reason: str = "") -> dict:
    """心跳离线告警卡片"""
    body = f"Agent: {agent_name}\n最后心跳: {last_heartbeat}\n原因: {reason or '心跳超时'}"
    return send_card("StockGame 心跳离线告警", body, color="red")


def send_heartbeat_recover(agent_name: str) -> dict:
    """心跳恢复通知卡片"""
    return send_card("StockGame 心跳恢复", f"Agent: {agent_name}\n心跳已恢复正常", color="green")
