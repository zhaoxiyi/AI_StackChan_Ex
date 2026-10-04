"""
STT 文本精简与更正记忆

两个轻量组件, 服务于上下文保持:

1. UserTextCondenser — STT 识别文本的精简
   语音输入天然包含重复 (口吃/重说/回声残留) 与口语填充词,
   直接入 Context 会稀释语义并浪费 vLLM 输入预算。
   精简后入历史, 语义不变, 对话关联更紧密。

2. CorrectionMemory — 用户更正记录
   用户纠正信息 ("不是A, 是B" / "A不对, 应该是B") 时,
   记录更正映射并注入 system prompt, 保证后续对话
   沿正确方向进行, 不会重复犯同样的理解错误。

两者均为纯文本处理, 不依赖 LLM, 零额外推理成本。
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Optional


# =============================================================================
# 用户输入精简器
# =============================================================================

class UserTextCondenser:
    """
    STT 文本精简器。

    处理顺序:
      1. 整串周期重复:  "测试一下语音测试一下语音" → "测试一下语音"
                        "Yeah Yeah Yeah"           → "Yeah"
      2. 前缀重复:      "测试一下语音测试一下语音。" → "测试一下语音。"
                        (带句末标点时整串规则不命中, 用前缀规则兜底)
      3. 口语填充词:    嗯/呃/啊/哦/额/那个/这个/就是/えーと... 清除
      4. 词级连续重复:  填充词清除后, 空格分词下相邻重复词去重
                        "今天天气怎么样 嗯 那个 今天天气怎么样" → "今天天气怎么样"

    保底: 精简后若过短 (<2字), 返回原文, 避免语义丢失。
    """

    # 多字填充词必须排在单字之前 (先替换长的)
    _FILLERS = (
        "嗯嗯", "呃呃", "啊啊", "哦哦", "额额",
        "那个", "这个", "就是",
        "えーと", "ええと", "あのー", "あの",
        "嗯", "呃", "啊", "哦", "额",
    )

    def condense(self, text: str) -> str:
        """精简 STT 文本。返回精简后的文本 (可能等于原文)。"""
        t = (text or "").strip()
        if not t:
            return ""
        original = t

        t = self._collapse_whole_repeats(t)
        t = self._collapse_prefix_repeat(t)
        # 先清填充词再去词重复 — 被填充词隔开的重复片段才能相邻命中
        t = self._strip_fillers(t)
        t = self._collapse_word_repeats(t)
        t = re.sub(r"\s+", " ", t).strip()

        # 保底: 精简过头就保留原文
        if len(t) < 2 and len(original) >= 2:
            return original
        return t

    # --- 内部步骤 ---

    @staticmethod
    def _collapse_whole_repeats(text: str) -> str:
        """整串周期重复压缩: AABBC... 形如 unit*k 的整串 → unit

        单元下限 3: 避免两位数字等短模式误伤 (如 "2020")
        """
        n = len(text)
        # 从大到小找最大重复单元 (unit >= 3)
        for unit in range(n // 2, 2, -1):
            if n % unit == 0 and text[:unit] * (n // unit) == text:
                return text[:unit]
        return text

    @staticmethod
    def _collapse_prefix_repeat(text: str) -> str:
        """前缀重复压缩: "AAB" → "AB" (迭代直到无前缀重复)

        单元下限 3: 避免两位数字等短模式误伤 (如 "2020" 不应被当成 "20"×2)
        """
        while True:
            n = len(text)
            found = False
            for unit in range(min(n // 2, 24), 2, -1):
                if text[:unit] == text[unit:2 * unit]:
                    text = text[unit:]
                    found = True
                    break
            if not found or len(text) < 2:
                return text

    @staticmethod
    def _collapse_word_repeats(text: str) -> str:
        """空格分词下连续重复词去重 (英文语音输入常见)"""
        words = text.split()
        if len(words) < 2:
            return text
        out: list[str] = []
        for w in words:
            if out and w == out[-1]:
                continue
            out.append(w)
        return " ".join(out)

    @classmethod
    def _strip_fillers(cls, text: str) -> str:
        """清除口语填充词"""
        t = text
        for f in cls._FILLERS:
            t = t.replace(f, "")
        return t

    # --- 重复检测 (跨轮) ---

    @staticmethod
    def is_duplicate(new: str, prev: str, threshold: float = 0.85) -> bool:
        """
        判断本轮输入是否与上一轮入历史的用户文本重复。

        场景: 用户反复说同一句测试话 / 回声残留导致的重复识别。
        重复时不提交 LLM, 避免无效推理与 Context 污染。
        """
        a = (new or "").strip()
        b = (prev or "").strip()
        if not a or not b:
            return False
        if a == b:
            return True
        if len(a) < 3 or len(b) < 3:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


# =============================================================================
# 更正记忆
# =============================================================================

class CorrectionMemory:
    """
    用户更正记录 — 提取并记忆"正确方向"。

    支持句式 (中文):
      - "不是X，是Y" / "不是X 是Y"
      - "X不对，应该是Y" / "X说错了，是Y"
      - "应该是Y，不是X"
      - "纠正：Y" / "更正一下：Y"
    日语:
      - "XじゃなくてY" / "XではなくY"

    同一主题 (old 相同) 再次更正时覆盖为最新值 — 始终跟随
    用户最新指示。记录注入 system prompt, LLM 每轮可见。
    """

    # (pattern, 捕获组顺序) — group 顺序: (old, new)
    _PATTERNS: list[tuple[re.Pattern, str]] = [
        # 不是X，是Y / 不是X 是Y
        (re.compile(r"不是(.{1,24}?)[，,、]?\s*是(.{1,48}?)(?:[。！？!?…]|$)"), "old_new"),
        # X不对/说错了，应该是Y
        (re.compile(r"(.{1,24}?)(?:不对|说错了|记错了|搞错了)[，,。]?\s*(?:应该是|正确的是|是)(.{1,48}?)(?:[。！？!?…]|$)"), "old_new"),
        # 应该是Y，不是X → 捕获顺序 (new, old)
        (re.compile(r"(?:应该是|正确的是)(.{1,48}?)[，,、]\s*不是(.{1,24}?)(?:[。！？!?…]|$)"), "new_old"),
        # 纠正：Y (无 old)
        (re.compile(r"(?:纠正|更正)(?:一下)?[，,：:]\s*(.{1,80}?)(?:[。！？!?…]|$)"), "new_only"),
        # 日语: XじゃなくてY
        (re.compile(r"(.{1,24}?)(?:じゃなくて|ではなく|じゃない[、,]?)\s*(.{1,48}?)(?:[。！？!?…]|$)"), "old_new"),
    ]

    def __init__(self, max_entries: int = 5):
        self._max = max(1, max_entries)
        self._entries: list[dict] = []

    # --- 提取 ---

    def extract(self, text: str) -> Optional[dict]:
        """从用户语句中提取更正。无更正返回 None。

        Returns:
            {"old": str, "new": str} — old 可为空 (仅声明正确内容时)
        """
        t = (text or "").strip()
        if not t:
            return None

        for pattern, order in self._PATTERNS:
            m = pattern.search(t)
            if not m:
                continue
            if order == "new_old":
                new, old = m.group(1), m.group(2)
            elif order == "new_only":
                new, old = m.group(1), ""
            else:
                old, new = m.group(1), m.group(2)

            old = self._clean(old)
            new = self._clean(new)
            if not new:
                continue
            return {"old": old, "new": new}
        return None

    # --- 记录 ---

    def record(self, old: str, new: str) -> None:
        """记录一条更正。同一 old 已存在时覆盖为最新值。"""
        old = self._clean(old)
        new = self._clean(new)
        if not new:
            return

        for e in self._entries:
            if old and e["old"] == old:
                e["new"] = new
                e["at"] = time.time()
                return

        self._entries.append({"old": old, "new": new, "at": time.time()})
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    # --- 注入 ---

    def to_system_suffix(self) -> str:
        """生成追加到 system prompt 的更正信息文本。无记录返回空串。"""
        if not self._entries:
            return ""
        lines = ["[已记录的用户更正 (后续对话请始终遵循)]"]
        for e in self._entries:
            if e["old"]:
                lines.append(f"- 关于'{e['old']}'，用户的正确说法是：'{e['new']}'")
            else:
                lines.append(f"- 用户更正/明确：'{e['new']}'")
        return "\n".join(lines)

    @property
    def count(self) -> int:
        return len(self._entries)

    # --- 内部 ---

    @staticmethod
    def _clean(s: str) -> str:
        """清理提取片段: 去空白、去首尾标点/助词"""
        t = (s or "").strip()
        t = t.strip("，,。.!！?？…～~、 ")
        # 去掉捕获到的首尾助词残留
        t = re.sub(r"^(是|说|叫|为)+", "", t)
        t = re.sub(r"(这个|那个)$", "", t)
        return t.strip()
