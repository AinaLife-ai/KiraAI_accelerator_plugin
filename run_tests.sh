#!/bin/sh
# 全量自检 —— 一条命令跑完所有测试
set -e
cd "$(dirname "$0")"
echo "════ KiraAI 加速器 · 全量自检 ════"
fail=0

run() {
  name="$1"; shift
  printf '\n──── %s ────\n' "$name"
  if timeout -k 5 240 python3 "$@" 2>&1 | tail -4; then :; fi
  # 取真实退出码
  # ★ 硬超时：真集成/真流式测试会起服务器，卡住不能让整套挂死
  if ! timeout -k 5 240 python3 "$@" > /tmp/_accel_out 2>&1; then
    fail=$((fail+1)); echo "❌ $name 失败"; sed -n '1,40p' /tmp/_accel_out
  else
    echo "✅ $name 通过"
  fi
}

run "接管护栏 patches"        test_patches.py
run "段切分 stream_first"     test_stream_first.py
run "抢先发送 early_sent"     test_early_sent.py
run "流式引擎 stream_engine"  test_stream_engine.py
run "自动思考 auto_thinking"  test_auto_thinking.py
run "工具并行 parallel"       test_parallel.py
run "并行等价性 parallel_eq"  test_parallel_equiv.py
run "壁纸轮换 rotation"      test_rotation.py
run "插件兼容性 compat"       test_compat.py
run "装载与还原 load"         test_load.py
run "前端URL前缀守卫"         test_frontend_api.py
run "★真框架API集成"          test_real_api.py
run "★代理类型透传"           test_proxy_isinstance.py
run "★真流式端到端"           test_stream_real.py
run "★壁纸轮换真逻辑"         test_rotation_live.py
run "★思考注入覆盖面"         test_thinking_providers.py

printf '\n════════════════════════════\n'
if [ "$fail" -eq 0 ]; then echo "🎉 全部通过"; else echo "❌ $fail 个套件失败"; exit 1; fi
