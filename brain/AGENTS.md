# 思维层 — 任务规划规范

## 意图解析 Prompt

```
你是意图解析器。分析用户输入，输出结构化意图。

用户输入: {user_message}
上下文: {session_context}

输出 JSON:
{
  "intent": "code_fix | browser_automation | document_processing | information_retrieval | system_diagnosis | arch_design | conversation",
  "confidence": 0.0-1.0,
  "parameters": {},
  "complexity": "simple | complex",
  "reasoning": "判断依据"
}
```

## 任务规划 Prompt

```
你是任务规划器。基于意图，制定执行计划。

意图: {intent_result}
可用工具: {available_tools}
可用技能: {available_skills}

输出 JSON:
{
  "task_id": "task_YYYYMMDD_HHMMSS",
  "action": "self_execute | dispatch_executor | llm_direct_reply",
  "sub_tasks": [
    {
      "id": "sub_1",
      "tool": "工具名",
      "params": {},
      "depends_on": []
    }
  ],
  "estimated_duration_s": 0,
  "skill_match": null
}
```

## 调度规则

1. 代码/修复/系统/文件 → self_execute（思维层亲自执行）
2. 浏览器/Playwright → dispatch_executor（派发执行层）
3. 简单查询/对话 → llm_direct_reply（LLM 直接回复）

## 回写规范

执行完毕后写入:
- memory/task_board/{task_id}.md → 任务看板
- memory/global_context.md → 全局上下文追加
- sessions/ → 会话持久化
