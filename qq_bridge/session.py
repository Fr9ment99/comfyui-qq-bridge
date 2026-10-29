# -*- coding: utf-8 -*-
"""会话状态与最近结果缓存。

单用户自用场景下不需要数据库，进程内保存即可：
- UserSession：当前选中的模式（一次性，出图后清空）
- JobRecord：最近几次生成的输入与结果，供 /again 和"重绘这张图"使用
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class UserSession:
    mode: Optional[str] = None
    updated: float = field(default_factory=time.time)


@dataclass
class JobRecord:
    mode: str
    profile_id: str
    prompt: str = ""
    region_prompt: str = ""
    draw_prompt: str = ""
    seed: int = 0
    images: List[bytes] = field(default_factory=list)
    output: bytes = b""
    created: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, session_ttl=3600, history_limit=5, history_ttl=86400):
        self.session_ttl = session_ttl
        self.history_limit = history_limit
        self.history_ttl = history_ttl
        self._sessions: Dict[str, UserSession] = {}
        self._history: Dict[str, deque] = {}

    def get(self, openid):
        session = self._sessions.get(openid)
        now = time.time()
        if session is None or now - session.updated > self.session_ttl:
            session = UserSession(updated=now)
            self._sessions[openid] = session
        return session

    def set_mode(self, openid, mode):
        session = self.get(openid)
        session.mode = mode
        session.updated = time.time()

    def clear_mode(self, openid):
        session = self.get(openid)
        session.mode = None
        session.updated = time.time()

    def remember(self, openid, record):
        bucket = self._history.setdefault(openid, deque(maxlen=self.history_limit))
        bucket.append(record)
        # 顺手清理过期记录
        cutoff = time.time() - self.history_ttl
        while bucket and bucket[0].created < cutoff:
            bucket.popleft()

    def last(self, openid):
        bucket = self._history.get(openid)
        if not bucket:
            return None
        record = bucket[-1]
        if time.time() - record.created > self.history_ttl:
            return None
        return record
