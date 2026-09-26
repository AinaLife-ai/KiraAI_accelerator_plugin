import os, sys, pathlib, importlib.util, re
os.environ.setdefault("KIRA_FW", "/var/minis/shared/alife_vs_kira/kira_fw_v2346")
sys.path.insert(0, "/tmp/a8/tests")
import _env
FW = _env.framework()
sys.path.insert(0, FW)
sys.path.insert(0, "/tmp/a8")
os.makedirs("/tmp/xft/data", exist_ok=True)
os.chdir("/tmp/xft")

# 导入 xml_tag_fixer
XFIX = os.environ.get("XFIX", "/tmp/xfix/main.py")
if not os.path.isfile(XFIX):
    print("⏭  跳过：需要 xml_tag_fixer 源码（设 XFIX=/path/to/main.py）")
    sys.exit(0)
spec = importlib.util.spec_from_file_location("xfix_main", XFIX)
xm = importlib.util.module_from_spec(spec)
sys.modules["xfix_main"] = xm
spec.loader.exec_module(xm)

# 导入我们的 early_sent（剥离实现）
ES = _env.load("early_sent")

OK, BAD = [], []
def check(name, cond, detail=""):
    print(("  \u2713 " if cond else "  \u2717 ") + name + (f"  [{detail}]" if detail else ""))
    (OK if cond else BAD).append(name)


def make_fixer(cfg=None):
    P = xm.XmlTagFixerPlugin
    inst = P.__new__(P)
    ns = {"inst": inst}
    for line in pathlib.Path("/tmp/xft/initvals.txt").read_text(encoding="utf-8").split("\n"):
        if line.strip():
            exec(line.replace(" = true", " = True").replace(" = false", " = False")
                     .replace(" = null", " = None"), ns)
    inst._registered_tags = set()
    inst._registered_msg_tags = set()
    inst._registered_root_tags = set()
    inst.IGNORE_TAGS = {"file","record","video","image","sticker","forward","reply","reasoning",
      "at","face","json","lightapp","animation","poke","node","location","share",
      "voice","shortvideo","gif","cardimage","tts","pe","redbag","emoji","img","selfie"}
    inst.BUILTIN_NO_WRAP_TAGS = {"mimo_tts"}
    inst.no_wrap_tags = set(inst.BUILTIN_NO_WRAP_TAGS)
    inst.force_wrap_tags = set()
    inst._normalize_tag_name = lambda x: str(x).lower() if x else ""
    inst._mimo_checked = False
    for k, v in (cfg or {}).items():
        setattr(inst, k, v)
    return inst


print("═══ xml_tag_fixer 改写后，我们的剥离是否仍有效 ═══")
fx = make_fixer()

cases = [
    ("实体转义",   ["<msg><text>价格 A & B</text></msg>"],      2),
    ("补全标签",   ["<msg><text>你好</text>"],                   1),
    ("拆分消息块", ["<msg><text>A</text></msg>", "<msg><text>B</text></msg>"], 2),
    ("反斜杠转义", ["<msg><text>他说 <\\/msg> 写法不对</text></msg>"], 1),
]

for name, segs, n in cases:
    # ① 我们抢发的原文
    seg0 = segs[0]
    # ② 框架收到的完整文本 = 各段拼接（抢发时就是这个）
    full = "".join(segs)
    # ③ xml_tag_fixer 在 ON_LLM_RESPONSE 把它改写一遍
    fixed = fx.fix_xml(full)
    # ④ 发送层做剥离
    rest = ES.strip_early_sent_smart(fixed, n, segs)
    # 判据：剥离后**不得**再含"已发段的可比对内容"
    rest_norm = ES._norm_seg(rest or "")
    leaked = []
    for s in segs:
        piece = ES._norm_seg(s)
        if piece and piece in rest_norm:
            leaked.append(s)
    check(f"{name}：改写后仍能剥干净", not leaked,
          f"泄漏={leaked}" if leaked else repr(rest))

print()
print("═══ 反向：不剥离时会泄漏（证明判据有效）═══")
full = "".join(cases[0][1])
fixed = fx.fix_xml(full)
rest0 = ES.strip_early_sent_smart(fixed, 0, [])
check("n=0（不剥离）时确实会泄漏 ⇒ 判据不是恒真",
      ES._norm_seg(cases[0][1][0]) in ES._norm_seg(rest0))

print()
if BAD:
    print(f"❌ {len(BAD)} 项未通过: {BAD}"); sys.exit(1)
print(f"🎉 兼容性实测通过（{len(OK)} 项）")
