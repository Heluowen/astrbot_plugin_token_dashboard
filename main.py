"""Token 用量看板 —— 可视化模型的 token 消耗与缓存命中情况。

数据来源：AstrBot 自己的 provider_stats 表（只读）。
提供：
  * WebUI 页面  pages/token-dashboard/index.html
  * JSON 接口   /astrbot_plugin_token_dashboard/*
  * 聊天命令    /token [小时数]
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_token_dashboard"


def _utc_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@register(
    PLUGIN_NAME,
    "heluo",
    "可视化模型 token 消耗：缓存命中率趋势、按会话/模型拆分、单次请求明细与成本估算。",
    "1.0.0",
    "https://github.com/",
)
class TokenDashboard(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._holiday_cache: set[str] | None = None
        self.db_path = self._resolve_db_path()
        self._labels = self._load_labels()
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/summary", self.api_summary, ["GET"], "用量总览"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/series", self.api_series, ["GET"], "时间序列"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/recent", self.api_recent, ["GET"], "最近请求"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/sessions", self.api_sessions, ["GET"], "按会话拆分"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/meta", self.api_meta, ["GET"], "看板元信息"
            )
            logger.info(f"[{PLUGIN_NAME}] 已注册 5 个 WebUI 接口，数据库: {self.db_path}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 注册 WebUI 接口失败: {e}")

    # ------------------------------------------------------------------ 配置

    def _resolve_db_path(self) -> str:
        custom = str(self.config.get("db_path") or "").strip()
        if custom and os.path.exists(custom):
            return custom
        candidates = [
            Path.home() / ".astrbot" / "data" / "data_v4.db",
            Path(os.environ.get("ASTRBOT_DATA_DIR", "")) / "data_v4.db"
            if os.environ.get("ASTRBOT_DATA_DIR")
            else None,
        ]
        for c in candidates:
            if c and Path(c).exists():
                return str(c)
        return str(Path.home() / ".astrbot" / "data" / "data_v4.db")

    def _load_labels(self) -> dict[str, str]:
        raw = str(self.config.get("session_labels") or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _price(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_hhmm(value: Any, fallback: str) -> int:
        """把 "HH:MM" 解析成一天中的分钟数。"""
        text = str(value or fallback).strip()
        try:
            hh, _, mm = text.partition(":")
            return int(hh) * 60 + int(mm or 0)
        except (TypeError, ValueError):
            hh, _, mm = fallback.partition(":")
            return int(hh) * 60 + int(mm or 0)

    def _peak_windows(self) -> list[tuple[int, int]]:
        """解析高峰时段窗口，默认 09:00-12:00、14:00-18:00。"""
        raw = str(
            self.config.get("peak_windows", "09:00-12:00,14:00-18:00")
            or "09:00-12:00,14:00-18:00"
        )
        windows: list[tuple[int, int]] = []
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            left, _, right = part.partition("-")
            if not right:
                continue
            start = self._parse_hhmm(left, "09:00")
            end = self._parse_hhmm(right, "12:00")
            if start < end:
                windows.append((start, end))
        if not windows:
            windows = [(540, 720), (840, 1080)]
        return windows

    def _holidays(self) -> set[str]:
        """法定节假日日期集合（YYYY-MM-DD），来自配置的 JSON 数组。"""
        if self._holiday_cache is None:
            raw = str(self.config.get("holidays", "") or "").strip()
            dates: set[str] = set()
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, list):
                        for item in data:
                            text = str(item).strip()
                            if not text:
                                continue
                            # 支持 "2026-10-01" 与 "2026-10-01..2026-10-07" 区间写法
                            if ".." in text:
                                left, _, right = text.partition("..")
                                try:
                                    d0 = datetime.strptime(left.strip(), "%Y-%m-%d").date()
                                    d1 = datetime.strptime(right.strip(), "%Y-%m-%d").date()
                                    cur = d0
                                    while cur <= d1:
                                        dates.add(cur.strftime("%Y-%m-%d"))
                                        cur += timedelta(days=1)
                                except ValueError:
                                    pass
                            else:
                                dates.add(text)
                except Exception:
                    pass
            self._holiday_cache = dates
        return self._holiday_cache

    @staticmethod
    def _beijing(dt: datetime) -> datetime:
        """把时间换算成北京时间（Asia/Shanghai）。"""
        try:
            from zoneinfo import ZoneInfo

            return dt.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            return dt

    def _is_peak(self, dt_local: datetime) -> bool:
        """是否处于高峰时段。

        规则：北京时间 周一至周五（不含法定节假日）的 09:00-12:00 与 14:00-18:00；
        其余时间——含周末、节假日全天——都算空闲时段。
        """
        dt = self._beijing(dt_local)
        if dt.strftime("%Y-%m-%d") in self._holidays():
            return False
        if dt.weekday() >= 5:  # 周六 / 周日
            return False
        minute = dt.hour * 60 + dt.minute
        return any(start <= minute < end for start, end in self._peak_windows())

    def _price_triplet(self, peak: bool) -> tuple[float, float, float]:
        """返回 (未命中输入, 命中输入, 输出) 单价，单位 元/百万 token。"""
        if peak:
            return (
                self._price("price_input_miss_peak", 2.0),
                self._price("price_input_hit_peak", 0.04),
                self._price("price_output_peak", 8.0),
            )
        return (
            self._price("price_input_miss_offpeak", 1.0),
            self._price("price_input_hit_offpeak", 0.02),
            self._price("price_output_offpeak", 4.0),
        )

    @staticmethod
    def _row_ts(created_at: Any) -> float | None:
        try:
            return (
                datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except Exception:
            return None

    def _cost_at(
        self, ts: float | None, miss: int, hit: int, out: int
    ) -> float:
        """按请求发生时刻的计费档位估算花费。"""
        peak = self._is_peak(datetime.fromtimestamp(ts)) if ts else False
        p_miss, p_hit, p_out = self._price_triplet(peak)
        return (
            miss / 1_000_000 * p_miss
            + hit / 1_000_000 * p_hit
            + out / 1_000_000 * p_out
        )

    # ------------------------------------------------------------------ 数据

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch(self, hours: float) -> list[sqlite3.Row]:
        since = _utc_str(datetime.now(timezone.utc) - timedelta(hours=hours))
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT created_at, umo, provider_model, agent_type, "
                "token_input_other, token_input_cached, token_output "
                "FROM provider_stats WHERE created_at >= ? ORDER BY created_at",
                (since,),
            ).fetchall()
        finally:
            conn.close()

    def _label_for(self, umo: str) -> str:
        if not umo:
            return "未知会话"
        tail = str(umo).split(":")[-1]
        if tail in self._labels:
            return self._labels[tail]
        for key, name in self._labels.items():
            if key and key in str(umo):
                return name
        kind = "群" if "GroupMessage" in str(umo) else "私聊"
        return f"{kind} {tail}"

    @staticmethod
    def _rate(hit: int, miss: int) -> float:
        total = hit + miss
        return round(hit / total * 100, 1) if total else 0.0

    def _aggregate(self, hours: float) -> dict[str, Any]:
        rows = self._fetch(hours)
        miss = sum(r["token_input_other"] or 0 for r in rows)
        hit = sum(r["token_input_cached"] or 0 for r in rows)
        out = sum(r["token_output"] or 0 for r in rows)
        cost = 0.0
        for r in rows:
            cost += self._cost_at(
                self._row_ts(r["created_at"]),
                r["token_input_other"] or 0,
                r["token_input_cached"] or 0,
                r["token_output"] or 0,
            )
        return {
            "requests": len(rows),
            "input_miss": miss,
            "input_hit": hit,
            "input_total": miss + hit,
            "output": out,
            "hit_rate": self._rate(hit, miss),
            "cost": round(cost, 4),
        }

    # ------------------------------------------------------------------ 接口

    async def api_meta(self):
        now_local = datetime.now()
        now_peak = self._is_peak(now_local)
        peak = self._price_triplet(True)
        off = self._price_triplet(False)
        windows = ", ".join(
            f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
            for s, e in self._peak_windows()
        )
        return jsonify(
            {
                "db_path": self.db_path,
                "db_exists": os.path.exists(self.db_path),
                "refresh_seconds": int(self.config.get("refresh_seconds", 60) or 0),
                "now_peak": now_peak,
                "now_period": "高峰时段" if now_peak else "空闲时段",
                "peak_rule": f"北京时间 周一至周五（不含法定节假日）{windows}",
                "holiday_count": len(self._holidays()),
                "prices": {
                    "offpeak": {
                        "input_miss": off[0],
                        "input_hit": off[1],
                        "output": off[2],
                    },
                    "peak": {
                        "input_miss": peak[0],
                        "input_hit": peak[1],
                        "output": peak[2],
                    },
                },
                "server_time": now_local.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    async def api_summary(self):
        try:
            hours = float(request.args.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24
        try:
            return jsonify(
                {
                    "ok": True,
                    "hours": hours,
                    "today": self._aggregate(24),
                    "last7d": self._aggregate(24 * 7),
                    "last30d": self._aggregate(24 * 30),
                    "window": self._aggregate(hours),
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] summary 失败: {e}")
            return jsonify({"ok": False, "error": str(e)})

    async def api_series(self):
        try:
            hours = float(request.args.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24
        bucket_hours = 1 if hours <= 72 else 24
        try:
            rows = self._fetch(hours)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)})

        now_local = datetime.now()
        buckets: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                ts = datetime.strptime(
                    str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if bucket_hours == 1:
                slot = int(ts // 3600 * 3600)
            else:
                local = datetime.fromtimestamp(ts)
                slot = int(
                    local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                )
            item = buckets.setdefault(
                slot, {"ts": slot, "requests": 0, "miss": 0, "hit": 0, "out": 0}
            )
            item["requests"] += 1
            item["miss"] += r["token_input_other"] or 0
            item["hit"] += r["token_input_cached"] or 0
            item["out"] += r["token_output"] or 0

        points = []
        for slot in sorted(buckets):
            b = buckets[slot]
            dt = datetime.fromtimestamp(slot)
            label = (
                dt.strftime("%H:%M")
                if bucket_hours == 1
                else dt.strftime("%m-%d")
            )
            points.append(
                {
                    "ts": slot,
                    "label": label,
                    "requests": b["requests"],
                    "miss": b["miss"],
                    "hit": b["hit"],
                    "out": b["out"],
                    "hit_rate": self._rate(b["hit"], b["miss"]),
                }
            )
        return jsonify(
            {
                "ok": True,
                "hours": hours,
                "bucket_hours": bucket_hours,
                "now": now_local.strftime("%m-%d %H:%M"),
                "points": points,
            }
        )

    async def api_recent(self):
        try:
            limit = int(request.args.get("limit", 40))
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(limit, 300))
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT created_at, umo, provider_model, agent_type, "
                    "token_input_other, token_input_cached, token_output "
                    "FROM provider_stats ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)})

        items = []
        for r in rows:
            ts = self._row_ts(r["created_at"])
            try:
                local_time = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
            except Exception:
                local_time = str(r["created_at"])[:19]
            miss = r["token_input_other"] or 0
            hit = r["token_input_cached"] or 0
            out = r["token_output"] or 0
            items.append(
                {
                    "time": local_time,
                    "session": self._label_for(r["umo"] or ""),
                    "model": r["provider_model"] or "-",
                    "agent": r["agent_type"] or "-",
                    "miss": miss,
                    "hit": hit,
                    "out": out,
                    "hit_rate": self._rate(hit, miss),
                    "peak": self._is_peak(datetime.fromtimestamp(ts)) if ts else False,
                    "cost": round(self._cost_at(ts, miss, hit, out), 5),
                }
            )
        return jsonify({"ok": True, "items": items})

    async def api_sessions(self):
        try:
            hours = float(request.args.get("hours", 24 * 7))
        except (TypeError, ValueError):
            hours = 24 * 7
        try:
            rows = self._fetch(hours)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)})

        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            umo = r["umo"] or ""
            item = agg.setdefault(
                umo,
                {
                    "session": self._label_for(umo),
                    "umo": umo,
                    "requests": 0,
                    "miss": 0,
                    "hit": 0,
                    "out": 0,
                    "cost": 0.0,
                },
            )
            miss = r["token_input_other"] or 0
            hit = r["token_input_cached"] or 0
            out = r["token_output"] or 0
            item["requests"] += 1
            item["miss"] += miss
            item["hit"] += hit
            item["out"] += out
            item["cost"] += self._cost_at(self._row_ts(r["created_at"]), miss, hit, out)

        items = []
        for item in agg.values():
            item["hit_rate"] = self._rate(item["hit"], item["miss"])
            item["input_total"] = item["miss"] + item["hit"]
            item["cost"] = round(item["cost"], 4)
            items.append(item)
        items.sort(key=lambda x: x["input_total"], reverse=True)
        return jsonify({"ok": True, "hours": hours, "items": items})

    # ------------------------------------------------------------------ 命令

    @filter.command("token")
    async def cmd_token(self, event: AstrMessageEvent):
        """查看 token 用量。用法：/token [小时数，默认 24]"""
        arg = (event.message_str or "").strip().split()
        hours = 24.0
        for token in arg[1:]:
            try:
                hours = float(token)
                break
            except ValueError:
                continue
        try:
            data = self._aggregate(hours)
            win = self._aggregate(24 * 7)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"读取用量失败: {e}")
            return

        def human(n: int) -> str:
            if n >= 10_000:
                return f"{n / 10_000:.1f}万"
            return str(n)

        lines = [
            f"近 {hours:g} 小时：{data['requests']} 次请求",
            f"输入 {human(data['input_total'])}（命中 {human(data['input_hit'])} / {data['hit_rate']}%）",
            f"输出 {human(data['output'])} · 约 ¥{data['cost']:.3f}",
            "",
            f"近 7 天：{win['requests']} 次 · 命中率 {win['hit_rate']}% · 约 ¥{win['cost']:.2f}",
            f"当前时段：{'高峰（贵一倍）' if self._is_peak(datetime.now()) else '空闲'}",
        ]
        yield event.plain_result("\n".join(lines))
