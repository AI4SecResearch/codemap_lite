"""Prompt builder for repair agent subprocess invocations."""
from __future__ import annotations


def build_repair_prompt(source_id: str) -> str:
    """Build the prompt passed to the CLI agent for repairing a specific source point."""
    from codemap_lite.analysis.repair_orchestrator import _safe_dirname

    safe_id = _safe_dirname(source_id)
    icsl_dir = f".icslpreprocess_{safe_id}"
    return f"""你是一个修复间接调用的 agent，当前正在处理 source point {source_id}。
请全程使用中文输出你的分析和推理过程。

操作步骤：

1. 执行: python {icsl_dir}/icsl_tools.py query-reachable --source {source_id}
   获取可达子图及所有 UnresolvedCall。

2. 对每个 UnresolvedCall：
   - 阅读调用位置的源文件
   - 分析变量类型、赋值和上下文
   - 确定正确的调用目标
   - 执行: python {icsl_dir}/icsl_tools.py write-edge --caller <caller_id> --callee <callee_id> --call-type <indirect|virtual> --call-file <file> --call-line <line> --llm-response "<你的分析摘要>" --reasoning-summary "<一句话理由>"

3. 处理完当前所有 UnresolvedCall 后，再次执行 query-reachable。
   新发现的可达节点可能带来新的 UnresolvedCall。
   重复直到没有新的 UnresolvedCall。

4. 完成后，编排器会执行 check-complete 验证。

注意事项：
- 在决定目标前先查看 {icsl_dir}/counter_examples.md — 其中包含已知的错误解析
- 跳过已存在的边
- 每次 write-edge 都必须传 --llm-response 和 --reasoning-summary
- 遇到系统/标准库函数时停止（无源码可查）
- 在代码库中找不到实现时停止
- 检测并打断调用链中的环
- 所有输出（包括 reasoning-summary 和 llm-response）请使用中文
"""
