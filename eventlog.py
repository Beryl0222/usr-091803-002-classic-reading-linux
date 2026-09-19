"""只追加事件日志：阅读档案全部状态的唯一事实来源。

任何状态修改都以事件表达，事件一经追加不可修改；教师撤回批注、调整路线、
换用新版教材时，可按 as_of 时刻重放，复原过去班级看到的内容。
"""

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone


class EventError(ValueError):
    """追加事件违反流一致性时抛出。"""


def utc_now():
    """统一的 UTC 时间戳格式（秒精度，便于测试与归档比对）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value):
    return datetime.fromisoformat(value)


class Event:
    """一条不可变事实。stream 是实体 id（如 viewpoint:v1）。"""

    __slots__ = ("seq", "at", "stream", "kind", "payload")

    def __init__(self, seq, at, stream, kind, payload):
        object.__setattr__(self, "seq", seq)
        object.__setattr__(self, "at", at)
        object.__setattr__(self, "stream", stream)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", payload)

    def __setattr__(self, name, value):
        raise AttributeError("事件不可变")

    def to_dict(self):
        return {"seq": self.seq, "at": self.at, "stream": self.stream,
                "kind": self.kind, "payload": self.payload}

    @classmethod
    def from_dict(cls, data):
        return cls(data["seq"], data["at"], data["stream"],
                   data["kind"], data["payload"])

    def __repr__(self):
        return f"Event(seq={self.seq}, {self.stream}/{self.kind}@{self.at})"


class EventLog:
    """内存事件日志，可选 JSONL 持久化。

    - 全局 seq 单调递增；同一 stream 内要求 at 非递减，杜绝乱序写入。
    - read(stream) 只回放该流；scan() 支持跨流投影。
    - all_events(as_of=...) 让整个系统能回到任意历史时点。
    """

    def __init__(self, path=None, clock=utc_now):
        self._events = []
        self._streams = defaultdict(list)  # stream -> [event]
        self._lock = threading.RLock()
        self._clock = clock
        self.path = path
        if path:
            self._load(path)

    # ---- 写入 -----------------------------------------------------------

    def append(self, stream, kind, payload, at=None):
        """追加一条事件。at 可注入（测试/迁移），生产默认使用时钟。"""
        at = at or self._clock()
        with self._lock:
            history = self._streams[stream]
            if history and at < history[-1].at:
                raise EventError(
                    f"流 {stream} 时间戳回退：{at} 早于 {history[-1].at}")
            event = Event(len(self._events), at, stream, kind, dict(payload))
            self._events.append(event)
            history.append(event)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return event

    # ---- 读取 -----------------------------------------------------------

    def read(self, stream, as_of=None):
        with self._lock:
            events = list(self._streams.get(stream, ()))
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
        return events

    def all_events(self, as_of=None):
        with self._lock:
            events = list(self._events)
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
        return events

    def scan(self, stream_prefix=None, kinds=None, as_of=None):
        """按流前缀和事件类型过滤（均为可选），结果按 seq 有序。"""
        kinds = set(kinds) if kinds else None
        result = []
        for event in self.all_events(as_of):
            if stream_prefix is not None and not event.stream.startswith(stream_prefix):
                continue
            if kinds is not None and event.kind not in kinds:
                continue
            result.append(event)
        return result

    @property
    def count(self):
        with self._lock:
            return len(self._events)

    # ---- 持久化 ---------------------------------------------------------

    def _load(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        event = Event.from_dict(json.loads(line))
                        self._events.append(event)
                        self._streams[event.stream].append(event)
        except FileNotFoundError:
            pass
