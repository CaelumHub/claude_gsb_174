# -*- coding: utf-8 -*-
"""
配置与路径：集中管理后端运行所需的目录、常量与语言版本。

本模块不依赖任何第三方库，仅使用 Python 标准库，是整个平台的"地基"。
所有与磁盘相关的路径都从这里派生，保证前端静态目录、数据目录、运行目录
始终指向一致的位置，避免散落各处的硬编码路径。
"""

import os
import sys

# ---------------------------------------------------------------------------
# 目录与路径
# ---------------------------------------------------------------------------
# backend/ 的上一级即项目根目录（gsb5/）
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(BACKEND_DIR)

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
DATA_DIR = os.path.join(BASE_DIR, "data")
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
SETTINGS_DIR = os.path.join(DATA_DIR, "settings")

# 语言与平台元信息
LANGUAGE_NAME = "MiniLang"
LANGUAGE_VERSION = "1.0.0"
PLATFORM_VERSION = "1.0.0"

# 前端默认页面（也是"10 个页面"的入口映射）
DEFAULT_PAGE = "editor.html"

# HTTP 服务默认绑定
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# 运行时限制（防止死循环 / 无限递归把服务拖垮）
MAX_INSTRUCTION_LIMIT = 10_000_000     # 单次运行允许执行的最大指令数
MAX_CALL_DEPTH = 512                    # 最大调用深度
MAX_RECURSION_REPORTED = 200            # 报告堆栈溢出时的可见深度
MAX_LIST_LEN = 1_000_000                # 列表最大长度
MAX_SAMPLE_INTERVAL_MS = 1000           # 采样剖析最大间隔（毫秒）
MIN_SAMPLE_INTERVAL_MS = 1              # 采样剖析最小间隔（毫秒）

# ---------------------------------------------------------------------------
# 调试会话生命周期
# ---------------------------------------------------------------------------
# 会话状态全部驻留内存，反复新建会只增不减，因此需要明确的"上限 + 失效"机制：
#   * 达到数量上限时，优先淘汰已结束、再淘汰最久未访问的暂停会话；
#   * 空闲超过 TTL（自上次访问起）或绝对存活时间超过上限，即视为过期并清理；
#   * 停止调试会立即删除会话，所属项目删除时连带清理其全部会话。
# 以下默认值均可通过同名环境变量覆盖（数字，单位见注释），便于压测/运维调整。
MAX_DEBUG_SESSIONS = int(os.environ.get("GSB_MAX_DEBUG_SESSIONS", "20"))   # 同时驻留会话上限
DEBUG_SESSION_IDLE_TTL = int(os.environ.get("GSB_DEBUG_IDLE_TTL", "1800"))  # 空闲失效：秒，默认 30 分钟
DEBUG_SESSION_MAX_TTL = int(os.environ.get("GSB_DEBUG_MAX_TTL", "7200"))    # 绝对失效：秒，默认 2 小时


def ensure_dirs():
    """确保运行所需的目录都存在（幂等）。"""
    for d in (DATA_DIR, PROJECTS_DIR, RUNS_DIR, SETTINGS_DIR):
        os.makedirs(d, exist_ok=True)


def resolve_port(argv=None):
    """从命令行参数 / 环境变量解析监听端口，返回 (host, port)。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("GSB_HOST", DEFAULT_HOST)
    port = int(os.environ.get("GSB_PORT", DEFAULT_PORT))
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1]); i += 1
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]; i += 2
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]; i += 1
        else:
            i += 1
    return host, port


def has_flag(argv, flag):
    """判断命令行参数里是否包含某个开关（如 --check / --seed）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    return flag in argv
