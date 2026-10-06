"""
LLM 输出复读治理 — 检测 + 剔除 + 重生成

治理 LLM 输出侧的两类"复读"行为:

1. 复读用户话语
   LLM 回复复述用户原话 (如 "你问今天天气怎么样, 今天..."),
   或整句复读不做回答。检测回复与用户输入的最长重合子串,
   命中后剔除复读片段; 剔除后所剩无几 → 触发重生成。

2. 自我复读
   LLM 连续轮次输出相同/高度相似内容 (短回复模型高发,
   如连续 "好的呢。")。检测回复与最近 N 条助手回复的
   字符 bigram Jaccard 相似度, 短回复高相似 → 触发重生成。

纯文本处理, 不依赖 LLM, 零额外推理成本 (重生成除外)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class GuardResult:
    """一次复读治理的判定结果"""
    action: str          # "pass" | "strip" | "retry"
    cleaned: str         # 剔除复读片段后的文本 (action=strip/retry 时可能不等于原文)
    reason: str          # 触发原因 (日志用)
    retry_hint: str      # 重生成提示 (action=retry 时非空)


# 比较时忽略的字符 (标点/空白, 不计入重合度)
_PUNCT_WS_RE = re.compile(r"[\s\W_]+", re.UNICODE)


class ResponseGuard:
    """
    LLM 输出复读治理器。

    决策顺序:
      1. 复读用户检测 (最长重合子串) → 剔除片段
         剔除后过短 → retry (提示: 不要复述问句)
      2. 自我复读检测 (bigram Jaccard × 最近 N 条回复)
         短回复高相似 → retry (提示: 换个说法/补充新信息)
      3. 否则 pass
    """

    def __init__(self, enabled: bool = True, user_echo_min_chars: int = 6,
                 user_echo_ratio: float = 0.5, min_kept_chars: int = 6,
                 self_repeat_similarity: float = 0.8, self_repeat_window: int = 3,
                 self_repeat_short_max: int = 40):
        self.enabled = enabled
        self.user_echo_min_chars = max(2, user_echo_min_chars)
        self.user_echo_ratio = user_echo_ratio
        self.min_kept_chars = max(1, min_kept_chars)
        self.self_repeat_similarity = self_repeat_similarity
        self.self_repeat_window = max(1, self_repeat_window)
        self.self_repeat_short_max = self_repeat_short_max

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def guard(self, reply: str, user_text: str,
              recent_replies: Optional[list[str]] = None) -> GuardResult:
        """治理一轮 LLM 回复。

        Args:
            reply:          LLM 本轮回复全文 (已 strip think)
            user_text:      本轮用户输入 (已精简)
            recent_replies: 最近 N 条助手回复 (旧→新或新→旧均可)

        Returns:
            GuardResult — action=pass 时 cleaned 即原文
        """
        r = (reply or "").strip()
        u = (user_text or "").strip()
        if not self.enabled or not r:
            return GuardResult("pass", r, "", "")

        # ---- 1) 复读用户检测 ----
        intervals = self.user_echo_intervals(r, u)
        echo_len = sum(end - start for start, end in intervals)
        echo_ratio = echo_len / max(1, len(_PUNCT_WS_RE.sub("", u)))
        echo_detected = echo_len >= self.user_echo_min_chars \
            and echo_ratio >= self.user_echo_ratio

        # 仅当复读片段从回复开头开始 (允许 ≤2 字语气词前缀) 才处理:
        # 中段出现的用户原话是"呼应/引用" (如 "好的, 我来检查一下语音状况"
        # 中的后半句), 属正常表达; 挖掉会破坏句子结构, 导致播报断句
        if echo_detected and not (intervals and intervals[0][0] <= 2):
            echo_detected = False

        cleaned = r
        if echo_detected:
            cleaned = self._remove_intervals(r, intervals)
            cleaned = self._tidy(cleaned)

        if echo_detected and len(_PUNCT_WS_RE.sub("", cleaned)) < self.min_kept_chars:
            # 剔除后所剩无几 → 整条回复都是复读, 重生成
            return GuardResult(
                action="retry",
                cleaned=cleaned,
                reason=f"复读用户话语 (重合 {echo_len}字/{echo_ratio:.0%})",
                retry_hint="你刚才的回复只是重复了用户的话，没有回答内容。"
                           "请直接、简短地回答用户的问题，不要复述用户的问句。",
            )

        # ---- 2) 自我复读检测 (对最终候选文本判定) ----
        candidate = cleaned if echo_detected else r
        # 历史中的"(被打断…)"等标注后缀会稀释相似度 (如 0.93<0.95 漏拦), 比较前剥离
        recent = [re.sub(r"\s*\(被打断[^)]*\)\s*$", "", x.strip())
                  for x in (recent_replies or []) if x.strip()]
        if recent:
            # 完全相同 (规范化后) → 直接拦截: 不依赖相似度阈值与长度豁免。
            # (历史案例: 标注后缀/细微改写会把相似度压到阈值以下导致漏拦)
            cand_norm = _PUNCT_WS_RE.sub("", candidate)
            for prev in recent:
                if cand_norm and cand_norm == _PUNCT_WS_RE.sub("", prev):
                    return GuardResult(
                        action="retry",
                        cleaned=cleaned,
                        reason="自我复读 (完全相同)",
                        retry_hint="你刚才的回复和之前的回答一字不差。"
                                   "请换个说法，或者补充新的信息，不要重复之前说过的话。",
                    )
            sim = max(self.similarity(candidate, prev) for prev in recent)
            # 短回复: 相似度门槛即可; 长回复: 仅"几乎逐字重复"(≥0.95)才拦截
            # (长回复重生成成本高, 但"答非所问式重复上一轮"体验极差)
            if sim >= self.self_repeat_similarity \
                    and (len(candidate) <= self.self_repeat_short_max or sim >= 0.95):
                return GuardResult(
                    action="retry",
                    cleaned=cleaned,
                    reason=f"自我复读 (与近期回复相似度 {sim:.0%})",
                    retry_hint="你刚才的回复和之前的回答几乎一样。"
                               "请换个说法，或者补充新的信息，不要重复之前说过的话。",
                )

        if echo_detected:
            return GuardResult(
                action="strip",
                cleaned=cleaned,
                reason=f"剔除复读片段 (重合 {echo_len}字/{echo_ratio:.0%})",
                retry_hint="",
            )
        return GuardResult("pass", r, "", "")

    # ------------------------------------------------------------------
    # 检测算法 (公开, 便于单测)
    # ------------------------------------------------------------------

    def user_echo_intervals(self, reply: str, user_text: str) -> list[tuple[int, int]]:
        """找出 reply 中与 user_text 重合的区间 (reply 下标, 半开)。

        对 user 每个起点向后贪心扩展最长匹配子串; 子串延长后
        匹配集单调缩小, 首次失配即可停止。区间合并时吞掉
        间隔 ≤2 的标点/空白, 避免残留孤立标点。
        """
        if not user_text or not reply:
            return []
        min_len = self.user_echo_min_chars
        intervals: list[tuple[int, int]] = []
        i, ulen = 0, len(user_text)
        while i < ulen:
            if _PUNCT_WS_RE.match(user_text[i]):
                i += 1
                continue
            best: tuple[int, int] | None = None
            j = i + min_len
            while j <= ulen:
                pos = reply.find(user_text[i:j])
                if pos >= 0:
                    best = (pos, pos + (j - i))
                    j += 1
                else:
                    break          # 更长必不匹配
            if best:
                intervals.append(best)
                i += best[1] - best[0]
            else:
                i += 1

        return _merge_intervals(intervals, gap=2)

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """字符 bigram Jaccard 相似度 (语言无关, 忽略标点空白)。

        中文短句 "好的呢" vs "好的呀" → 高相似;
        "今天天气不错" vs "今天天气很好" → 中等相似。
        """
        sa = _bigrams(a)
        sb = _bigrams(b)
        if not sa and not sb:
            return 1.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return inter / union if union else 0.0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_intervals(reply: str, intervals: list[tuple[int, int]]) -> str:
        """按区间删除子串 (区间按起点升序且互不重叠)"""
        out: list[str] = []
        pos = 0
        for start, end in intervals:
            out.append(reply[pos:start])
            pos = end
        out.append(reply[pos:])
        return "".join(out)

    @staticmethod
    def _tidy(s: str) -> str:
        """清理剔除后的残留: 折叠重复标点、去首部标点"""
        t = re.sub(r"([，,。.!！?？…])\1+", r"\1", s)
        t = re.sub(r"^[\s，,。.!！?？…]+", "", t)
        return t.strip()


def _bigrams(s: str) -> set[str]:
    t = _PUNCT_WS_RE.sub("", s or "")
    if len(t) < 2:
        return set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _merge_intervals(intervals: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """合并重叠/相邻 (间隔 ≤gap) 区间"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
