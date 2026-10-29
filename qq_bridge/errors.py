# -*- coding: utf-8 -*-
"""统一异常类型，方便上层区分"用户可读错误"和"程序缺陷"。"""


class BridgeError(Exception):
    """本项目自定义错误的基类，message 会直接展示给用户。"""


class ConfigError(BridgeError):
    pass


class ProfileError(BridgeError):
    pass


class ComfyError(BridgeError):
    pass


class QQAPIError(BridgeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code
