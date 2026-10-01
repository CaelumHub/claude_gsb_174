# -*- coding: utf-8 -*-
"""
业务服务层：把编译器、解释器、调试器、剖析器、存储组织成可供 HTTP 接口调用的能力。

职责划分：
  * 项目管理与历史版本（创建/列表/重命名/删除/保存版本/恢复/对比）；
  * 编译流水线（带产物缓存）；
  * 普通运行（可选性能剖析）；
  * 交互式调试会话（断点/单步/续跑，会话状态驻留内存，支持跨请求同步）；
  * 内存快照（运行/调试暂停时产出）。

所有持久化都走 storage 层的"文件锁 + 原子写"，保证频繁写入下的并发安全。
"""

import os
import shutil
import time
import threading
import difflib
import hashlib
from typing import Dict, List, Optional

from . import config
from . import storage
from . import compiler as compiler_mod
from . import vm as vm_mod
from . import debugger as debugger_mod
from . import profiler as profiler_mod
from . import diagnostics as diag
from . import memory_model


# ---------------------------------------------------------------------------
# 版本 / 运行记录结构
# ---------------------------------------------------------------------------
def _empty_project(name, source, language="minilang"):
    now = storage.now_iso()
    pid = storage.new_id("proj")
    meta = {
        "id": pid,
        "name": name,
        "language": language,
        "created_at": now,
        "updated_at": now,
        "version_count": 0,
        "last_source": source,
        "description": "",
    }
    return meta


def _empty_version(pid, source, message, compiled=None):
    now = storage.now_iso()
    vid = storage.new_id("ver")
    manifest = {
        "id": vid,
        "project_id": pid,
        "message": message or "保存版本",
        "created_at": now,
        "source_hash": _hash(source),
        "source_len": len(source),
        "line_count": source.count("\n"),
        "compiled_ok": bool(compiled and compiled.success),
        "error_count": len(compiled.diagnostics.errors()) if compiled else 0,
    }
    return manifest


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _diff_lines(a: str, b: str) -> List[dict]:
    """行级 diff，返回结构化结果供前端渲染。"""
    al = a.splitlines()
    bl = b.splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({
            "op": tag,  # insert / delete / replace
            "old_start": i1 + 1, "old_end": i2,   # 1-based 行号
            "new_start": j1 + 1, "new_end": j2,
            "old_lines": al[i1:i2],
            "new_lines": bl[j1:j2],
        })
    return out


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------
class Service:
    def __init__(self):
        config.ensure_dirs()
        self.debug_sessions: Dict[str, "DebugSession"] = {}
        self._session_counter = 0
        # 会话表在多线程 HTTP 服务下被并发读写，用可重入锁保护"查表/淘汰/增删"；
        # VM 执行（可能耗时）在锁外进行，只在取到会话的短临界区内持锁。
        self._sessions_lock = threading.RLock()

    # ==================================================================
    # 项目管理
    # ==================================================================
    def list_projects(self):
        out = []
        if not os.path.isdir(config.PROJECTS_DIR):
            return out
        for name in os.listdir(config.PROJECTS_DIR):
            meta_path = os.path.join(config.PROJECTS_DIR, name, "meta.json")
            meta = storage.read_json(meta_path)
            if meta:
                out.append(meta)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def create_project(self, name, source, language="minilang"):
        name = (name or "").strip() or "未命名项目"
        meta = _empty_project(name, source, language)
        d = storage.project_dir(meta["id"])
        os.makedirs(d, exist_ok=True)
        storage.write_json(os.path.join(d, "meta.json"), meta)
        # 初始版本
        self.save_version(meta["id"], source, "初始版本")
        return self.get_project(meta["id"])

    def get_project(self, pid):
        meta = storage.read_json(os.path.join(storage.project_dir(pid), "meta.json"))
        return meta

    def update_project(self, pid, fields):
        meta_path = os.path.join(storage.project_dir(pid), "meta.json")
        def _mut(data):
            if data is None:
                return None, False
            for k, v in fields.items():
                if k in ("name", "description", "last_source", "language"):
                    data[k] = v
            data["updated_at"] = storage.now_iso()
            return data, True
        new_meta, _ = storage.update_json(meta_path, _mut)
        return new_meta

    def delete_project(self, pid):
        d = storage.project_dir(pid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        # 项目删除时连带清理其全部调试会话（含正在暂停的），杜绝孤儿会话残留。
        with self._sessions_lock:
            for sid in [s for s, sess in self.debug_sessions.items() if sess.project_id == pid]:
                self.debug_sessions.pop(sid, None)
        return True

    # ==================================================================
    # 历史版本
    # ==================================================================
    def list_versions(self, pid):
        vdir = storage.versions_dir(pid)
        out = []
        if not os.path.isdir(vdir):
            return out
        for name in os.listdir(vdir):
            mp = os.path.join(vdir, name, "manifest.json")
            m = storage.read_json(mp)
            if m:
                out.append(m)
        out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return out

    def save_version(self, pid, source, message=""):
        compiled = compiler_mod.compile_source(source)
        meta_path = os.path.join(storage.project_dir(pid), "meta.json")
        manifest = _empty_version(pid, source, message, compiled)
        vdir = storage.version_dir(pid, manifest["id"])
        os.makedirs(vdir, exist_ok=True)
        storage.write_json(os.path.join(vdir, "manifest.json"), manifest)
        storage.ensure_text(os.path.join(vdir, "source.txt"), source)
        storage.write_json(os.path.join(vdir, "compile.json"), {
            "diagnostics": compiled.diagnostics.to_list(),
            "success": compiled.success,
            "stage": compiled.stage,
        })
        storage.write_json(os.path.join(vdir, "run.json"), [])
        # 更新项目 meta
        def _mut(data):
            if data is None:
                return None, False
            data["version_count"] = int(data.get("version_count", 0)) + 1
            data["updated_at"] = storage.now_iso()
            data["last_source"] = source
            return data, True
        storage.update_json(meta_path, _mut)
        return manifest

    def get_version(self, pid, vid):
        vdir = storage.version_dir(pid, vid)
        manifest = storage.read_json(os.path.join(vdir, "manifest.json"))
        if not manifest:
            return None
        source = ""
        src_path = os.path.join(vdir, "source.txt")
        if os.path.exists(src_path):
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
        runs = storage.read_json(os.path.join(vdir, "run.json"), [])
        comp = storage.read_json(os.path.join(vdir, "compile.json"), {})
        return {"manifest": manifest, "source": source, "runs": runs, "compile": comp}

    def restore_version(self, pid, vid):
        ver = self.get_version(pid, vid)
        if not ver:
            return None
        src = ver["source"]
        msg = f"恢复到版本 {vid[:6]}"
        self.update_project(pid, {"last_source": src})
        return self.save_version(pid, src, msg)

    def diff_versions(self, pid, va, vb):
        a = self.get_version(pid, va)
        b = self.get_version(pid, vb)
        if not a or not b:
            return None
        return _diff_lines(a["source"], b["source"])

    # ==================================================================
    # 编译
    # ==================================================================
    def compile(self, source, pid=None, vid=None):
        result = compiler_mod.compile_source(source)
        if pid and vid:
            vdir = storage.version_dir(pid, vid)
            storage.write_json(os.path.join(vdir, "compile.json"), {
                "diagnostics": result.diagnostics.to_list(),
                "success": result.success,
                "stage": result.stage,
            })
        return result

    def compile_view(self, source, detail="all"):
        """供前端页面渲染的编译结果（token/ast/symbols/bytecode 按需返回）。"""
        result = compiler_mod.compile_source(source)
        view = result.to_dict()
        if "tokens" in detail or detail == "all":
            view["tokens"] = [{"type": t.type, "text": t.text, "line": t.line,
                               "column": t.column, "pos": t.pos} for t in result.tokens]
        if detail == "all" and result.ast is not None:
            view["ast"] = result.ast.to_dict()
        if detail == "all" and result.symbol_table is not None:
            view["symbols"] = result.symbol_table.to_dict()
        if detail == "all" and result.bytecode is not None:
            view["bytecode"] = result.bytecode.to_dict()
            view["bytecode"]["functions"].reverse()
        return view

    # ==================================================================
    # 运行（普通 / 性能剖析）
    # ==================================================================
    def run(self, source, options=None):
        options = options or {}
        result = compiler_mod.compile_source(source)
        if not result.success:
            return {
                "ok": False,
                "diagnostics": result.diagnostics.to_list(),
                "output": [],
                "stage": result.stage,
            }
        vm = vm_mod.VM(result.bytecode, result.source_lines)
        prof = None
        want_profile = options.get("profile", False)
        if want_profile:
            prof = profiler_mod.Profiler()
            vm.profiler = prof
            if options.get("sample", True):
                prof.start_sampling(float(options.get("sample_interval_ms", 1.0)))
        if options.get("inputs"):
            vm.input_queue = list(options["inputs"])
        vm.start()
        vm.run()
        if prof:
            prof.attach_vm(vm)
            prof.stop_sampling()
            report = prof.report()
        else:
            report = None
        heap = vm.heap.snapshot([]) if options.get("memory", False) else None
        out = {
            "ok": True,
            "output": list(vm.output),
            "return_value": vm.return_value,
            "error": vm.error.to_dict() if vm.error else None,
            "instruction_count": vm.instruction_count,
            "elapsed_ms": round(vm.elapsed_ms(), 3),
            "profile": report,
            "memory": heap,
        }
        return out

    def record_run(self, pid, vid, source, options=None):
        """运行并持久化运行记录到版本目录。"""
        options = options or {}
        started = time.time()
        out = self.run(source, options)
        rec = {
            "id": storage.new_id("run"),
            "timestamp": storage.now_iso(),
            "elapsed_ms": out.get("elapsed_ms", 0.0),
            "ok": out.get("ok", False),
            "output": out.get("output", []),
            "return_value": out.get("return_value"),
            "instruction_count": out.get("instruction_count", 0),
            "error": out.get("error"),
            "profiled": bool(out.get("profile")),
        }
        if pid and vid:
            vdir = storage.version_dir(pid, vid)
            run_path = os.path.join(vdir, "run.json")
            def _mut(data):
                data = data if isinstance(data, list) else []
                data.append(rec)
                if len(data) > 200:
                    data = data[-200:]
                return data, True
            storage.update_json(run_path, _mut)
        # 全局运行记录索引
        global_rec = dict(rec)
        global_rec["project_id"] = pid
        global_rec["version_id"] = vid
        storage.write_json(os.path.join(config.RUNS_DIR, rec["id"] + ".json"), global_rec)
        return rec

    # ==================================================================
    # 调试会话
    # ==================================================================
    # 允许通过调试命令驱动的动作白名单（杜绝 getattr 任意方法调用）。
    _DEBUG_COMMANDS = ("continue_", "step_instruction", "step_into",
                       "step_over", "step_out")

    def _new_session_id(self):
        self._session_counter += 1
        return f"dbg-{self._session_counter:x}-{int(time.time()*1000)%100000:x}"

    def debug_start(self, source, breakpoints=None, pid=None, vid=None):
        """创建并启动一个调试会话（编译 -> 建 VM -> 建调试器 -> 启动）。

        编译失败不产生会话；成功启动前先做过期清理与上限淘汰，保证驻留数量
        始终不超过 ``config.MAX_DEBUG_SESSIONS``。
        """
        bp = [b + 1 for b in (breakpoints or [])]
        sess = DebugSession(self._new_session_id(), source, bp, pid, vid)
        # 先编译：编译失败直接返回诊断，绝不登记进会话表（避免失败残留）。
        if not sess.result.success:
            return sess.state()
        with self._sessions_lock:
            self._purge_expired_locked()
            self._enforce_cap_locked()
            if len(self.debug_sessions) >= config.MAX_DEBUG_SESSIONS:
                # 全部是正在进行（暂停/运行）的会话，没有可淘汰对象。
                return {
                    "ok": False,
                    "error": f"调试会话数量已达上限（{config.MAX_DEBUG_SESSIONS} 个），"
                             "请先停止其他正在进行的调试后再试",
                }
            self.debug_sessions[sess.id] = sess
        # VM 启动放在锁外，避免长执行阻塞其他请求的会话查表。
        sess.start()
        return self.debug_state(sess.id)

    def debug_state(self, sid):
        with self._sessions_lock:
            sess = self._get_live_session_locked(sid)
            if not sess:
                return {"ok": False, "error": "会话不存在或已过期", "session_id": sid}
            sess.touch()
        return sess.state()

    def debug_command(self, sid, command, breakpoints=None):
        if command not in self._DEBUG_COMMANDS:
            return {"ok": False, "error": f"不支持的调试命令: {command}", "session_id": sid}
        with self._sessions_lock:
            sess = self._get_live_session_locked(sid)
            if not sess:
                return {"ok": False, "error": "会话不存在或已过期", "session_id": sid}
            # 已结束的会话只可查看、不可再驱动；防止对终结状态继续操作。
            if sess.is_finished():
                sess.touch()
                return {
                    "ok": False,
                    "error": "调试已结束，无法继续操作",
                    "session_id": sid,
                    "finished": True,
                }
            if breakpoints is not None:
                sess.set_breakpoints(breakpoints)
            sess.touch()
        # VM 执行在锁外进行。
        getattr(sess, command)()
        return self.debug_state(sid)

    def debug_stop(self, sid):
        """停止调试：立即从会话表删除，之后该会话不可再访问。"""
        with self._sessions_lock:
            sess = self.debug_sessions.pop(sid, None)
        if sess is not None:
            sess.dispose()
        return {"ok": True, "removed": bool(sess)}

    def debug_sessions_list(self):
        with self._sessions_lock:
            self._purge_expired_locked()
            now = time.monotonic()
            out = []
            for s in self.debug_sessions.values():
                out.append({
                    "session_id": s.id,
                    "project_id": s.project_id,
                    "started": s.started_at_iso,
                    "finished": s.is_finished(),
                    "status": s.status(),
                    "last_access": s.last_access_iso,
                    "idle_ttl_s": config.DEBUG_SESSION_IDLE_TTL,
                    "expires_in_s": max(0, int(sess_expires_in(s, now))),
                })
        return out

    # ------------------------------------------------------------------
    # 会话表内部维护（调用方须持有 self._sessions_lock）
    # ------------------------------------------------------------------
    def _get_live_session_locked(self, sid):
        """取会话；若已过期（空闲/绝对 TTL）则顺带清除并返回 None。"""
        sess = self.debug_sessions.get(sid)
        if sess is None:
            return None
        if sess.is_expired():
            self.debug_sessions.pop(sid, None)
            sess.dispose()
            return None
        return sess

    def _purge_expired_locked(self):
        """清除所有已超过空闲 / 绝对 TTL 的会话。"""
        for sid in [s for s, sess in self.debug_sessions.items() if sess.is_expired()]:
            sess = self.debug_sessions.pop(sid, None)
            if sess is not None:
                sess.dispose()

    def _enforce_cap_locked(self):
        """数量超上限时淘汰可回收会话：先淘汰已结束的，再淘汰最久未访问的暂停会话。

        正在执行（VM 短暂运行）的会话不在淘汰候选中；正在暂停等待下一步的会话
        按"最久未访问优先"回收，因此活跃调试（持续单步/查看）不会被打断。
        """
        while len(self.debug_sessions) >= config.MAX_DEBUG_SESSIONS:
            victim = None
            # 1) 已结束（正常完成 / 出错 / 编译产物已终结）的优先回收
            candidates = [s for s in self.debug_sessions.values() if s.is_finished()]
            if candidates:
                victim = min(candidates, key=lambda s: s.last_access)
            else:
                # 2) 暂停中、最久未访问的会话（正在执行的不会出现在此）
                paused = [s for s in self.debug_sessions.values() if s.is_paused()]
                if paused:
                    victim = min(paused, key=lambda s: s.last_access)
            if victim is None:
                break
            self.debug_sessions.pop(victim.id, None)
            victim.dispose()

    def memory_snapshot(self, sid):
        with self._sessions_lock:
            sess = self._get_live_session_locked(sid)
        if not sess or not sess.vm:
            return None
        return sess.vm.heap.snapshot(sess.vm.frame_snapshot())


def sess_expires_in(sess, now):
    """会话剩余有效期（秒）：取空闲 TTL 与绝对 TTL 的较早者。"""
    idle_deadline = sess.last_access + config.DEBUG_SESSION_IDLE_TTL
    abs_deadline = sess.created + config.DEBUG_SESSION_MAX_TTL
    return min(idle_deadline, abs_deadline) - now


class DebugSession:
    """一个交互式调试会话：持有 VM 与 Debugger，状态跨 HTTP 请求保留。"""

    def __init__(self, sid, source, breakpoints, pid=None, vid=None):
        self.id = sid
        self.source = source
        self.breakpoints = set(breakpoints)
        self.project_id = pid or None
        self.version_id = vid or None
        self.result = compiler_mod.compile_source(source)
        self.vm: Optional[vm_mod.VM] = None
        self.debugger: Optional[debugger_mod.Debugger] = None
        self.started = False
        # ---- 生命周期时间戳（monotonic 用于 TTL 判断，ISO 用于对外展示） ----
        self.created = time.monotonic()
        self.last_access = self.created
        self.started_at_iso = storage.now_iso()
        self.last_access_iso = self.started_at_iso

    # ------------------------------------------------------------------
    # 生命周期状态
    # ------------------------------------------------------------------
    def touch(self):
        """标记一次访问，刷新空闲 TTL（正在调试的会话因此不会被过期清理）。"""
        self.last_access = time.monotonic()
        self.last_access_iso = storage.now_iso()

    def is_expired(self):
        now = time.monotonic()
        if now - self.last_access > config.DEBUG_SESSION_IDLE_TTL:
            return True
        if now - self.created > config.DEBUG_SESSION_MAX_TTL:
            return True
        return False

    def is_finished(self):
        return bool(self.started and self.vm is not None and self.vm.finished)

    def is_paused(self):
        """暂停等待下一步命令——可作为上限淘汰的候选（按最久未访问排序）。"""
        return bool(self.started and self.vm is not None
                    and getattr(self.vm, "paused", False) and not self.vm.finished)

    def status(self):
        if not self.started or self.vm is None:
            return "not_started"
        if self.vm.finished:
            return "finished"
        if getattr(self.vm, "paused", False):
            return "paused"
        return "running"

    def dispose(self):
        """释放对 VM / 调试器 / 源码的引用，便于尽早回收内存。"""
        self.debugger = None
        self.vm = None
        self.source = None
        self.started = False

    def set_breakpoints(self, lines):
        self.breakpoints = set(lines)
        if self.debugger:
            self.debugger.set_breakpoints(list(self.breakpoints))

    def start(self):
        if not self.result.success:
            self.started = False
            return
        self.vm = vm_mod.VM(self.result.bytecode, self.result.source_lines)
        self.debugger = debugger_mod.Debugger(self.vm, self.breakpoints)
        self.debugger.start()
        self.started = True

    def state(self):
        if not self.result.success:
            return {
                "ok": False,
                "compile_failed": True,
                "diagnostics": self.result.diagnostics.to_list(),
                "source_lines": self.result.source_lines,
            }
        if not self.started or self.vm is None:
            return {"ok": True, "not_started": True, "diagnostics": self.result.diagnostics.to_list()}
        snap = self.debugger.snapshot()
        snap["ok"] = True
        snap["session_id"] = self.id
        snap["source_lines"] = self.result.source_lines
        snap["source"] = self.source
        snap["diagnostics"] = self.result.diagnostics.to_list()
        snap["breakpoint_instructions"] = self._breakpoint_hits()
        snap["memory"] = self.vm.heap.snapshot(self.vm.frame_snapshot())
        return snap

    def _breakpoint_hits(self):
        """返回每个函数里命中断点的指令偏移（供前端高亮字节码）。"""
        out = []
        for name, fc in self.result.bytecode.functions.items():
            for ins in fc.instructions:
                if ins.line in self.breakpoints:
                    out.append({"function": name, "offset": ins.offset, "line": ins.line})
        main = self.result.bytecode.main
        if main:
            for ins in main.instructions:
                if ins.line in self.breakpoints:
                    out.append({"function": "<main>", "offset": ins.offset, "line": ins.line})
        return out

    # ---- 命令分发 ----
    def continue_(self):
        if self.debugger:
            self.debugger.continue_()

    def step_instruction(self):
        if self.debugger:
            self.debugger.step_instruction()

    def step_into(self):
        if self.debugger:
            self.debugger.step_into()

    def step_over(self):
        if self.debugger:
            self.debugger.step_over()

    def step_out(self):
        if self.debugger:
            self.debugger.step_out()
