"""兼容性修复：给下游保留「模型原始输出」，同时不破坏框架的"不重复发送"。

## 问题

抢先发送后，我们已经把前面几段发给了用户。为了让框架不要重复发送，
`resp.text_response` 被替换成了"还没发出去的那部分"。

但这会**误导下游**——实测有两处依赖 `resp.text_response` 是**模型的完整输出**：

1. **框架内置 `kira-ai` 插件**（`builtin_plugins/kira-ai/main.py:111`）：
   在 `ON_LLM_RESPONSE` 里对整个回复做 XML 校验，解析失败时用另一个模型去修。
   只拿到尾巴 ⇒ `<root>` 包不住 ⇒ 误判成解析失败 ⇒ **多花一次 LLM 调用去"修"一个本来没坏的东西**。

2. **sustained-chat 插件**（`main.py:1681`）：
   `ai_text = (resp.text_response or "").strip()` 用来判断 AI 是否发了内容（决定是否继续维持对话窗口）。
   只看到尾巴 ⇒ 可能**误判为"AI 没说话"**。

## 修法

把"给框架发送用的文本"与"给下游看的完整文本"**分开**：

- `resp.text_response`  ← **模型的完整输出**（下游看到的就是它）
- `resp.__dict__` 上的私有标记 ← 告诉**我们自己的发送层**："前 N 段已经发出去了"

真正的剥离发生在**发送时**（`_strip_early_sent`），而不是在构造响应时。
这样：
  - 下游插件的语义**完全不变**（拿到的就是完整输出）
  - 框架仍然**不会重复发送**已发出的段

### ⚠️ 标记绝不能放进 `resp.tool_results`（我踩过的坑）

第一版我把标记 append 进了 `resp.tool_results`，理由是"框架对没有 tool_calls 的响应
不会读它"。**这个推理是错的**：模型完全可能"先说一句，再决定调工具"——同一个响应里
`text_response` 有已抢发的段、`tool_calls` 又非空。这时 `agent_executor` 会走
tool_calls 分支并执行：

    tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError` ⇒ **整个回合崩掉**。

所以标记只放 `resp.__dict__`——框架从不遍历它，绝无副作用。
"""
from __future__ import annotations

# 历史遗留：曾经的标记键名。**已废弃** —— 标记现在只放 resp.__dict__，
# 放 tool_results 会让框架的 OpenAIMessage(**r) 崩（见 mark_early_sent 的说明）。
MARKER_KEY = "_accel_early_sent"

# 正则：匹配一个**已闭合**的 <msg> 段（与 stream_first 保持一致）
import posixpath  # noqa: E402
import re  # noqa: E402
from xml.sax.saxutils import unescape  # noqa: E402

_MSG_CLOSED = re.compile(r"<msg(?:\s[^>]*)?>.*?</msg>|<msg(?:\s[^>]*)?/>", re.DOTALL)


def _norm_seg(s: str) -> str:
    """把一段内容规范化成"可比对的纯文本"。

    · 去掉所有标签（只留文字）
    · **反转义 XML 实体** —— xml_tag_fixer 会先转义再解析，
      已发段里的 `&` 在改写后可能变成 `&amp;`，不反转义就匹配不上
    · 去掉所有空白 —— 对重新换行/重新包裹也稳健
    """
    if not s:
        return ""
    txt = re.sub(r"<[^>]*>", "", s)
    txt = unescape(txt)
    return re.sub(r"\s+", "", txt)


def _top_level_msgs(text: str) -> list[tuple[int, int]]:
    """列出文本里所有 `<msg>` 块的 (start, end)。"""
    out = []
    for m in _MSG_CLOSED.finditer(text or ""):
        out.append((m.start(), m.end()))
    return out


def _block_has_content(text: str, span) -> bool:
    """块内（去掉 <msg> 开/闭标签后）是否还有东西。

    用来区分两种"规范化文本为空"的块：
      · `<msg/>` / `<msg></msg>`          ⇒ 纯占位，没有内容
      · `<msg><sticker id="7"/></msg>` 等 ⇒ **媒体段**（贴纸/图片/语音/转发…）

    ★★★ 2026-09-28（自查发现的回归）：锚点搜索原来只按"可见文字"匹配，
      而**媒体段没有文字**（`_norm_seg` 去标签后为空）⇒ 匹配不到 ⇒ 保守不剥
      ⇒ **框架把贴纸/图片再发一遍**（本仓库实测：6 个媒体用例全部残留）。
      修法：把"媒体段"作为**第二类可匹配对象**，与文字流并行按序消费。
    """
    s, e = span
    seg = text[s:e]
    seg = re.sub(r"^<msg(?:\s[^>]*)?/?>", "", seg)      # 开标签（含自闭合 <msg/>）
    seg = re.sub(r"</msg>$", "", seg)                    # 闭标签
    return bool(seg.strip())


def _seg_sig(seg: str) -> str:
    """媒体段的"身份签名"：块内**标签本身**（标签名 + 全部属性），去掉空白。

    ★★★ 2026-09-28（自查发现的回归，两次）：
      锚点搜索原来只按"可见文字"匹配 ⇒ 媒体段（没有文字）匹配不到：
        ① 先修成"按序消费媒体段"—— 但那在"文本里有个*别的*媒体在前"时会**误吃**
           （实测：已发 sticker7、文本是 `sticker9 + sticker7 + 甲 + 尾`
            ⇒ 按序吃掉了 sticker9、留下 sticker7 ⇒ 9 丢 + 7 重复）；
        ② 最终改成**按签名匹配**：`<sticker id="7"/>` 只与**同签名**的块配对，
           位置无关 ⇒ 上面那例正确保留 9、剥掉 7 和甲。
      这样对"同一条媒体"（属性一致）稳，对"另一条媒体"（属性不同）不会误吃。
    """
    if not seg:
        return ""
    inner = re.sub(r"^<msg(?:\s[^>]*)?/?>", "", seg)
    inner = re.sub(r"</msg>$", "", inner)
    return re.sub(r"\s+", "", inner)


# ════════════════════════════════════════════════════════════════════════
# v1.0.83：链 ↔ 台账 的「身份令牌」（两侧同源，**tag 无关**）
#
#   3.0 的按链对账原来只比"可见文字"（`getattr(e, "text", "")`），而贴纸/图片
#   这类元素**没有 text** ⇒ 纯媒体链永远匹配不上 ⇒ 框架把已抢发的内容再发一次。
#   线上现象：**表情包被发两次，文字正常**（触发条件是"表情独立成行"把 sticker
#   拆成独立消息链）。
#
#   这里给"消息元素"与"台账里的原始 XML 段"各写一个身份提取器，规则必须一致：
#     · 元素侧：按优先级取第一个非空身份字段（`sticker_id` 必须排在 `file` 之前 ——
#       贴纸元素的 file 是 base64，不可比）；
#     · XML 侧：已知身份标签取**内文**（`<sticker>21</sticker>` → `21`），
#       没有内文再取属性；`<img>`/`<selfie>`/`<record>` 的值是**提示词**
#       （元素侧拿到的是生成结果，身份不可比）、`json`/`forward`/`text`/`msg`
#       结构复杂或属于正文 ⇒ 一律**不参与比对**（保守：宁可不剥，也不误吃）。
#
#   ⚠️ Tag 名**故意不参与**比对：同一个 `<file type="image">url</file>` 在链路里
#      是 `Image` 元素，按标签比会错配；按"身份 + 计数"比才对得上。
# ════════════════════════════════════════════════════════════════════════

#: 元素侧身份字段优先级
#:   · `sticker_id` 必须排在 `file` 之前（贴纸元素的 file 是 base64，不可比）；
#:   · `md5` 必须排在**最后**：它只有图片（`Image`）才有、且是**惰性算出**的
#:     （`message_formatter` 做图描述时会调 `hash_image()`），而台账侧**永远
#:     不会有 md5** ⇒ 一旦它排在 `file` 前面，同一个 URL / 本地路径的图片就会
#:     因为"元素侧给 md5、台账侧给 url/path"而对不上 ⇒ 图片重复发送。
_IDENT_FIELDS = ("sticker_id", "file", "image", "record", "video",
                 "pid", "user_id", "message_id", "emoji_id", "md5")

#: XML 侧不参与比对的标签（生成类 / 正文 / 结构类）
#:   `video` 与 `img`/`selfie`/`record` 同类：**生成类**（值只是提示词，
#:   元素侧拿到的是生成结果、身份不可比）⇒ 提示词绝不能当身份用
#:   （否则可能与别的段的属性撞车 ⇒ 误吃丢内容）。
_IDENT_SKIP_TAGS = ("img", "selfie", "record", "video", "json", "forward", "text", "msg")

#: XML 侧没有内文时，按这个顺序找属性当身份
_IDENT_ATTRS = ("url", "path", "file", "pid", "id", "message_id", "emoji_id")

_TAG_OPEN_RE = re.compile(r"<([a-zA-Z_][\w.-]*)((?:\s[^>]*)?)\s*(/?)>")


def _canon_ident(s: str) -> str:
    """身份令牌的规范化（两侧**同一套**规则）。

    为什么需要：框架解析 `<file>` 时会把 `data/...` 拼成**绝对路径**
    （kira-ai 的 `resolve_local_send_path`：`data/files/x.txt` →
    `<数据根>/files/x.txt`，2.x/3.0 同款实现），而台账里存的是**原始 XML**。
    这里统一去掉空白、把 `\\` 归一到 `/`、并对 `data/` 相对路径做 normpath，
    让"同一个文件"在两侧落到同一种写法上。
    """
    s = re.sub(r"\s+", "", s or "")
    if not s:
        return ""
    if "\\" in s:
        s = s.replace("\\", "/")
    if s.startswith("data/"):
        s = posixpath.normpath(s)
    return s


def ident_match(a: str, b: str) -> bool:
    """两个身份令牌是否指**同一个**东西。

    相等即匹配；此外只放宽**一种**已知情况（否则就是 bug 的来源）：
    台账里的 `data/...` 相对路径在链侧已被框架解析成**绝对路径** ⇒
    "短的那个"落在"长的那个"的**路径分隔符边界**上就算同一个
    （`/x/zhabc.txt` 与 `abc.txt` 不算 —— 必须整段路径对得上，避免误吃）。

    刻意**不**放宽的：不同目录下的同名文件（只比 basename）**不匹配** ——
    那会把"另一条媒体"误判成已发 ⇒ 丢内容。宁可重复一次，也不丢。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return short.startswith("data/") and long_.endswith("/" + short)


def elem_ident(e) -> str:
    """消息元素 → 可比对身份；拿不到身份（或本身是文字）返回 ""（不参与比对）。"""
    try:
        if type(e).__name__ == "Text":
            return ""
        for f in _IDENT_FIELDS:
            v = getattr(e, f, None)
            if isinstance(v, (str, int)) and str(v).strip():
                return _canon_ident(str(v))
    except Exception:  # noqa: BLE001
        pass
    return ""


def seg_idents(seg: str) -> list:
    """台账原始 XML 段 → 身份列表（与 `elem_ident` 同源，保持出现顺序）。

    实现要点：**先剥掉最外层 `<msg …>` 包裹**（否则外层标签会把内层一起吞掉，
    内层身份一个都扫不到），再逐个开标签去找它自己的闭合标签取内文；
    自闭合标签则从属性里取。
    """
    s = re.sub(r"</?msg(?:\s[^>]*)?>", " ", seg or "")
    out = []
    for m in _TAG_OPEN_RE.finditer(s):
        tag = m.group(1).lower()
        if tag in _IDENT_SKIP_TAGS:
            continue
        inner = ""
        if m.group(3) != "/":                      # 非自闭合：找自己的闭合标签
            close = s.find("</" + m.group(1), m.end())
            if close >= 0:
                inner = s[m.end():close].strip()
        if inner:
            out.append(_canon_ident(inner))
            continue
        attrs = m.group(2) or ""
        for a in _IDENT_ATTRS:
            mm = (re.search(a + r'\s*=\s*"([^"]*)"', attrs)
                  or re.search(a + r"\s*=\s*'([^']*)'", attrs))
            if mm and mm.group(1).strip():
                out.append(_canon_ident(mm.group(1)))
                break
    return out


#: 媒体/结构化标签（连**内文**一起从"可见文字"里去掉）
_MEDIA_TAGS = ("sticker", "at", "reply", "emoji", "poke", "file", "image",
               "img", "record", "video", "selfie", "json", "forward")
_MEDIA_BLOCK_RE = re.compile(
    r"<(" + "|".join(_MEDIA_TAGS) + r")(?:\s[^>]*)?(?:/>|>.*?</\1>)",
    re.DOTALL | re.IGNORECASE,
)


def norm_text_only(seg: str) -> str:
    """只要"可见文字"：把媒体/结构化标签**连同内文**一并去掉再规范化。

    与 `_norm_seg` 的区别（为什么 3.0 链级对账需要它）：
      · `_norm_seg('<msg><sticker>21</sticker></msg>')` = `'21'` —— 贴纸 id 混进了"文字"，
        于是"纯媒体段"永远不可能被判定为"文字也覆盖了" ⇒ 台账出不了账；
      · 这里 `norm_text_only(...)` = `''`，媒体身份一律走 `seg_idents`，
        两个维度各自干净 ✓（纯媒体段命中后能正常出账）。
    `_norm_seg` 本身**不动**（2.x 剥离路径的既有语义与测试都依赖它）。
    """
    return _norm_seg(_MEDIA_BLOCK_RE.sub(" ", seg or ""))


def chain_signature(chain) -> tuple:
    """消息链 → `(可见文字, 身份列表, 元素类名列表)`。

    ★★★ 为什么必须有这个函数（v1.0.85）：
      3.0 的对账原本是"**台账=原始 XML 文本** ↔ **链=解析后的元素**"两侧异源，
      于是解析期的**任何**改写都会静默让判据失效：
        · `<file>` 的本地路径被解析成绝对路径（v1.0.84 修的）
        · `Image.md5` 惰性算出后顶掉 `file`（v1.0.84 修的）
        · 生成类（出图/TTS/视频）的元素身份是**生成结果**、与提示词不可比
          ⇒ 台账侧永远配不上 ⇒ 整段无法出账 ⇒ 框架再发一次（同一类重复）
      抢发的那一刻，**那条链就在我们手上** ⇒ 台账直接记"元素侧签名"，
      两侧由**同一个函数**产出 ⇒ 从根上不存在这类差异。

    `kinds`（元素类名列表）用于"生成类"段：它们的身份不可比，只能靠
    "元素种类完全一致 + 本轮顺序"配对（见 `main._consume_chain_record`）。
    """
    texts, idents, kinds = [], [], []
    try:
        for e in chain:
            kinds.append(type(e).__name__)
            piece = getattr(e, "text", "") or ""
            if piece:
                texts.append(piece)
                continue
            ident = elem_ident(e)
            if ident:
                idents.append(ident)
    except Exception:  # noqa: BLE001
        pass
    return norm_text_only(" ".join(texts)), idents, kinds


def _find_anchor_run(text: str, blocks: list, emitted_full: str,
                     media_sigs: list, media_total: int):
    """扫描出"匹配最完整"的一段连续块 —— 它们合起来正是**已发内容**。

    与"必须从第一块开始累加"不同（那是审计 P3 之前的实现，**确认会丢内容 + 重复**），
    这里对**每个可能起点**各扫一遍，取"消费内容最多"的那组：

      · 已发段**前面**有新增块（xml_tag_fixer 把裸文本包成 `<msg>` 是常规行为）
        ⇒ 那些块不参与匹配、被**原样保留**；
      · 已发段**之间**夹着别的块 ⇒ 同样保留；
      · 允许一条被拆成多条 / 多条被并成一条（匹配的是**规范化文本**，
        "块的文字是待消费内容的**前缀**"这一条对拆分/合并都成立）；
      · ★ 万一**前导的无关块**恰好也是已发内容的某个前缀（罕见但可能），
        从它开始扫只会消费一点点；从正确的块开始扫能消费满 ⇒ 取满的那组，
        不会因为"前面撞了一下"就剥错位置；
      · ★★★ **媒体段**（没有可见文字）：按**签名**配对（见 `_seg_sig`），
        位置无关 ⇒ 既不会漏剥（重复），也不会误吃别的媒体（丢内容）。

    返回 `(consumed_len, consumed_indices, media_used)`；一段都没消费到则返回 None。
    """
    n = len(blocks)
    norm = [_norm_seg(text[s:e]) for (s, e) in blocks]
    sig = [_seg_sig(text[s:e]) for (s, e) in blocks]
    best = None
    for start in range(n):
        pos = 0
        media_left = list(media_sigs)
        consumed = []
        for i in range(start, n):
            piece = norm[i]
            if not piece:
                # 没文字的块：与"已发媒体"里**同签名**的配对消费（各匹配一次）
                s_i = sig[i]
                if s_i and s_i in media_left:
                    media_left.remove(s_i)
                    consumed.append(i)
                continue
            # ★ 用 startswith(piece, pos) 而不是 emitted_full[pos:] 切片 ——
            #   切片在段数多时是多余的字符串拷贝。语义完全一致。
            if emitted_full.startswith(piece, pos):
                consumed.append(i)
                pos += len(piece)
        media_used = media_total - len(media_left)
        if consumed and (best is None or (pos, media_used) > (best[0], best[2])):
            best = (pos, consumed, media_used)
            # ★★ 强早退（证明正确）：已把**全部**已发内容消费掉 ⇒ 任何其它起点
            #   最多也只能消费同样多（不可能更多）⇒ 再扫也不会更优，直接停。
            #   这正好覆盖最常见的形态（前面有新增块、我们的段连续），
            #   把这种 O(n²) 降到实际 O(n)。
            if pos >= len(emitted_full) and media_used >= media_total:
                break
    return best



def _strip_leftover_segments(text: str, segments: list) -> str:
    """逐段独立兜底剥离（v1.0.91）。

    为什么需要：`strip_by_content` 的锚点是"一段**连续且同序**的已发内容"。
    一旦下游把已发段**重排 / 拆分 / 穿插别的块**，或只匹配到其中一部分，
    剩下的段会原样留在文本里 ⇒ 框架**再发一次**（线上现象：同一个贴纸被上传两次、
    同一条引用回复在群里出现两遍；而日志只打了台账条数，看不出漏剥）。

    做法：对每个"已发段原文"，在**块级**（顶层 `<msg>…</msg>`）做一次**精确匹配**
    （规范化后相等），命中就整块删除；匹配不上就**原样保留**（绝不做局部切除，
    宁可留给调用方告警，也不切坏别人的块）。

    安全性：只处理**台账里的段**——accel 只把"确认投递成功"的段写进台账，
    因此这里删掉的一定是"已经发出去过、再发就是重复"的内容。
    """
    if not text or not segments:
        return text
    for seg in segments:
        key = _norm_seg(seg)
        if not key:
            continue
        hit = None
        for (s, e) in _top_level_msgs(text):
            if _norm_seg(text[s:e]) == key:
                hit = (s, e)
                break
        if hit is None:
            continue
        text = text[:hit[0]] + text[hit[1]:]
    return text if text.strip() else "<msg/>"


def count_segments_removed(text: str, segments: list) -> int:
    """返回 segments 里**已经不在 text 中**的条数（供调用方打真实日志/告警）。"""
    try:
        n_out = _norm_seg(text)
        return sum(1 for s in segments if not (_norm_seg(s) and _norm_seg(s) in n_out))
    except Exception:
        return 0


def strip_by_content(text: str, segments: list[str]) -> str | None:
    """按**内容**剥掉已发出的段（贪心锚点版，2026-09-28 审计 P3 重构）。成功返回剩余文本；无法匹配返回 None。

    ★★★ 与旧版的差别（旧版**确认**会"同时丢内容 + 重复发送"）：
      旧版要求"从**第一块**开始累加 == 已发内容的前缀" —— 一旦下游插件
      在已发段**之前**插入新块（xml_tag_fixer 会把散落裸文本包成 `<msg>`），
      第一块就匹配不上 ⇒ 整段匹配失败 ⇒ 退回按序号剥离（切前 N 块）
      ⇒ **新增块与第一个已发段被删掉（丢内容），第二个已发段却留下（重复）** ✗✗
      实测（审计脚本）：`X + E1 + E2 + T3` ⇒ 按序号切 2 块 ⇒ 剩 `E2 + T3`
      ⇒ X 丢、E2 重复。
    现在：**保留一切未被匹配的块**，只删掉"匹配上已发内容"的那些块（连同
    它们**之间**的空块），前后新增的块一律原样保留。上面那个例子得到 `X + T3` ✓
    （X 保住、E1+E2 剥掉、T3 留下）。
    """
    if not text or not segments:
        return None
    emitted_full = "".join(_norm_seg(s) for s in segments)
    # 已发段里的"媒体签名"清单（有内容但没文字的段 = 媒体段）
    media_sigs = [sig for s in segments
                  if (sig := _seg_sig(s)) and _norm_seg(s) == ""]
    if not emitted_full and not media_sigs:
        return None                     # 只发过空占位（<msg/>）：没有可锚定的东西
    blocks = _top_level_msgs(text)
    if not blocks:
        return None

    anchor = _find_anchor_run(text, blocks, emitted_full, media_sigs, len(media_sigs))
    if anchor is None:
        return None

    # 逐块重建：把"匹配上已发内容"的块删掉，其余（新增 / 未发的块、以及
    # <msg> 之外的散段文本）**原样保留**。
    # 空块（`<msg/>` 占位）若夹在已消费块之间 ⇒ 一并删（属于同一段已发内容的间隔）；
    # 落在消费区之外的 ⇒ 保留（可能是别的插件新增的占位）。
    _consumed_len, consumed_idxs, _media_used = anchor
    drop = set(consumed_idxs)
    norm_all = [_norm_seg(text[s:e]) for (s, e) in blocks]
    first_idx, last_idx = consumed_idxs[0], consumed_idxs[-1]
    for i in range(first_idx, last_idx + 1):
        # 纯占位 <msg/>：夹在中间，一并删（我们从不抢发这种段）
        if not norm_all[i] and not _block_has_content(text, blocks[i]):
            drop.add(i)

    out = []
    cursor = 0
    for i, (s, e) in enumerate(blocks):
        if i in drop:
            out.append(text[cursor:s])
            cursor = e
    out.append(text[cursor:])
    rest = "".join(out)
    # ★★★ v1.0.91：锚点只保证"连续且同序"的那一段被剥掉。剩下的已发段**逐段兜底**
    #   （块级精确匹配）——它们已经发出去过了，留下就会被框架再发一遍。
    rest = _strip_leftover_segments(rest, segments)
    return rest if rest.strip() else "<msg/>"


def mark_early_sent(resp, segments: list[str], full_text: str) -> None:
    """在响应上记录"这些段已经抢先发出去了"。

    ★★ 只写**私有属性**，绝不碰 `resp.tool_results`！

    我第一版把标记塞进了 `tool_results`，注释还写着"框架对没有 tool_calls 的响应
    不会读它"——**这个推理是错的**：模型完全可能"先说一句，再决定调工具"，
    于是同一个响应里 `text_response` 有已抢发的段、`tool_calls` 又非空。
    这时 `agent_executor` 会走 tool_calls 分支并执行：

        tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

    我的标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError`
    ⇒ **整个回合崩掉**。

    所以标记只放 `resp.__dict__`：框架从不遍历它，绝无副作用。
    """
    if not segments:
        return
    try:
        resp.__dict__["_accel_full_text"] = full_text or ""
        resp.__dict__["_accel_early_sent_count"] = len(segments)
        # ★ 同时记下**已发段的原文**：剥离改为按内容匹配（见 strip_by_content），
        #   这样即使别的插件改写了 text_response（补标签/拆分/合并），也仍然能正确剥离。
        resp.__dict__["_accel_early_sent_segments"] = list(segments)
    except Exception:  # noqa: BLE001
        pass


def early_sent_count(resp) -> int:
    """读取"已经抢先发出了几段"。"""
    try:
        return int(resp.__dict__.get("_accel_early_sent_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def strip_early_sent_smart(text: str, count: int, segments: list | None = None) -> str:
    """剥离的**推荐入口**：优先按内容匹配（锚点搜索）；匹配不上时**保守处理**。

    为什么要按内容：`count` 是流式期间按**原始**文本数出来的，
    而别的插件可能在发送前改写 `text_response`（补 `<msg>`、拆分消息块、
    合并块、转义实体）—— 那时 count 就对不上了：
      · count 偏小 ⇒ 少切 ⇒ 重复发送
      · count 偏大 ⇒ 多切 ⇒ 丢内容
    按内容匹配对这两种改写都成立。

    ★★★ 2026-09-28（审计 P3）：**匹配不上时不再退回"按序号剥离"**。
      旧行为（切前 count 块）在"下游插件在已发段**之前**新增了块"时会
      **同时丢内容 + 重复发送**：
        SENT=[E1,E2]，改写后文本 = X + E1 + E2 + T3（X 是新增块、从未发过）
        按序号切 2 块 ⇒ 删掉 X 与 E1 ⇒ X 丢了，而 E2 还留着 ⇒ 框架再发一次 ✗✗
      现在有两道保险：
        ① 锚点搜索：`strip_by_content` 能在**任意位置**找到"连续块 = 已发内容
           前缀"的那一段（上例会精确剥掉 E1+E2、原样留下 X 与 T3）✓
        ② 万一连锚点都找不到（文本被改得面目全非）⇒ **不剥**、交回框架原文，
           细节记 debug —— 代价最多是"多一条重复"，**绝不删掉从未发出的内容**。
           （真发生重复时，发送层的按内容核对会打 warning，那才是该看的信号。）
          这与本插件一贯的取舍一致："宁可重复，不可丢内容"。
    """
    if count <= 0 or not text:
        return text
    if segments:
        try:
            got = strip_by_content(text, segments)
            if got is not None:
                return got
            # 找不到锚点 ⇒ 保守：不剥（原样交回）。
            # ★ 2026-10-05 降级为 debug：这是**安全的保守回退**（宁可重复、不丢内容），
            #   绝大多数场景下锚点其实找得到；真找不到时"可能重复"自有发送层的
            #   按内容核对会报（那条才是真问题信号）。这里不再打吓人的多行 error。
            import logging
            logging.getLogger("kira_accelerator").debug(
                "[accel] 剥离：文本里锚定不到已发段（或被下游改写），"
                "本次不剥离、交回框架（已发 %d 段 / 文本 %d 字符）",
                len(segments), len(text))
            return text
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("kira_accelerator").exception(
                "[accel] 剥离：按内容匹配出错 ⇒ 保守地不剥离（不丢内容优先）")
            return text
    # 没有段原文可锚定（极罕见：标记在但原文丢失）⇒ 只能按序号切，
    # 这是唯一可用的启发式；调用方已就该路径单独记一条 warning。
    return strip_early_sent(text, count)


def early_sent_segments(resp) -> list:
    """读取"已经抢先发出的段"的原文（供按内容剥离用）。"""
    try:
        segs = resp.__dict__.get("_accel_early_sent_segments") or []
        return list(segs)
    except Exception:  # noqa: BLE001
        return []


def strip_early_sent(text: str, count: int) -> str:
    """从完整文本里去掉**前 count 个已闭合的段**，返回仍需框架发送的部分。

    - 全部发完 ⇒ 返回 "<msg/>"（框架解析为空消息，不会发东西）
    - 一个都没发 ⇒ 原样返回
    """
    if count <= 0 or not text:
        return text
    out = text
    removed = 0
    while removed < count:
        m = _MSG_CLOSED.search(out)
        if not m:
            break
        out = out[:m.start()] + out[m.end():]
        removed += 1
    return out if out.strip() else "<msg/>"
