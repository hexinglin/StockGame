"""
模块名称: api/socket_events.py
说明:    Socket.IO 事件 — 客户端 join 轮次房间，接收实时推送
"""
import logging

from flask_socketio import join_room

logger = logging.getLogger(__name__)


def register_socket_events(socketio):
    @socketio.on("join_round")
    def on_join_round(data):
        """客户端加入轮次房间 {round_id}"""
        round_id = (data or {}).get("round_id")
        if round_id is None:
            return
        room = f"round_{round_id}"
        join_room(room)
        logger.info("socket 客户端加入房间 %s (sid=%s)", room, request_sid())

    @socketio.on("leave_round")
    def on_leave_round(data):
        from flask_socketio import leave_room
        round_id = (data or {}).get("round_id")
        if round_id is None:
            return
        leave_room(f"round_{round_id}")


def request_sid():
    try:
        from flask import request
        return request.sid
    except Exception:
        return "?"
