"""Tool call history formatter for IM channels.

This module provides utilities to extract and format tool call history
from LangGraph message state for display in IM channels.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

TOOL_ICONS: dict[str, str] = {
    "web_search": "🔍",
    "image_search": "🖼️",
    "web_fetch": "🌐",
    "ls": "📁",
    "read_file": "📖",
    "write_file": "📝",
    "str_replace": "✏️",
    "bash": "⌨️",
    "ask_clarification": "❓",
    "write_todos": "📋",
    "present_files": "📎",
    "task": "🤖",
}

DEFAULT_ICON = "🔧"


def _get_tool_icon(name: str) -> str:
    return TOOL_ICONS.get(name, DEFAULT_ICON)


def _truncate(text: str, max_len: int = 50) -> str:
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _format_args_summary(args: dict[str, Any], max_len: int = 100) -> str:
    if not args:
        return ""
    priority_keys = ["query", "url", "path", "command", "description", "prompt"]
    for key in priority_keys:
        if key in args:
            value = args[key]
            if isinstance(value, str):
                return _truncate(value, max_len)
            elif isinstance(value, (list, dict)):
                return _truncate(json.dumps(value, ensure_ascii=False), max_len)
    for key, value in args.items():
        if isinstance(value, str):
            return _truncate(value, max_len)
        elif isinstance(value, (list, dict)):
            return _truncate(json.dumps(value, ensure_ascii=False), max_len)
    return ""


def extract_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("type") == "human":
            last_human_idx = i
            break
    start_idx = last_human_idx + 1 if last_human_idx >= 0 else 0
    history: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    for msg in messages[start_idx:]:
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        if msg_type == "ai":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                if isinstance(tc, dict):
                    tc_id = tc.get("id") or tc.get("tool_call_id")
                    call_info = {"name": tc.get("name", "unknown"), "args": tc.get("args", {})}
                    if tc_id:
                        history.append({"id": tc_id, "name": call_info["name"], "args": call_info["args"], "status": "pending"})
                        pending_calls[tc_id] = len(history) - 1
                    else:
                        history.append({"name": call_info["name"], "args": call_info["args"], "status": "pending"})
        elif msg_type == "tool":
            tool_call_id = msg.get("tool_call_id")
            content = msg.get("content", "")
            if tool_call_id and tool_call_id in pending_calls:
                idx = pending_calls.pop(tool_call_id)
                history[idx]["result"] = content
                history[idx]["status"] = "complete"
    return history


def extract_last_tool_call(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    history = extract_tool_history(messages)
    if not history:
        return None
    return history[-1]


def _get_label_md(name: str, args: dict[str, Any]) -> str:
    """Get label in markdown format."""
    args_summary = _format_args_summary(args, max_len=80)
    if name == "web_search":
        return f"搜索网页: **{args_summary}**" if args_summary else "搜索网页"
    if name == "image_search":
        return f"搜索图片: **{args_summary}**" if args_summary else "搜索图片"
    if name == "web_fetch":
        return f"获取网页: **{args_summary}**" if args_summary else "获取网页"
    if name == "ls":
        return f"列出目录: **{args_summary}**" if args_summary else "列出目录"
    if name == "read_file":
        return f"读取文件: **{args_summary}**" if args_summary else "读取文件"
    if name == "write_file":
        return f"写入文件: **{args_summary}**" if args_summary else "写入文件"
    if name == "str_replace":
        return f"编辑文件: **{args_summary}**" if args_summary else "编辑文件"
    if name == "bash":
        desc = args.get("description")
        if desc:
            return f"**{desc}**"
        if args.get("command"):
            return f"执行命令: `{_truncate(args['command'], 60)}`"
        return "执行命令"
    if name == "task":
        return f"**{args.get('description') or '执行子任务'}**"
    if args.get("description"):
        return f"**{args['description']}**"
    return f"使用 **{name}**"


def _extract_error_message(result_text: str, parsed: dict | None) -> str:
    if parsed and isinstance(parsed, dict) and parsed.get("error"):
        return _truncate(str(parsed["error"]), 80)
    text = result_text.lower()
    for pattern in ["error:", "exception:", "failed"]:
        if pattern in text:
            idx = text.find(pattern)
            return _truncate(result_text[idx:idx + 80].strip(), 80)
    return _truncate(result_text[:80], 80)


def _format_detailed_result_md(name: str, result: str | dict | None, args: dict[str, Any] | None = None) -> list[str]:
    """Format detailed result in markdown format."""
    if not result:
        return []
    lines = []
    args = args or {}
    parsed = None
    if isinstance(result, dict):
        parsed = result
    elif isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            pass
    result_text = str(result) if result else ""
    is_error = (
        result_text.lower().startswith("error") or
        "exception" in result_text.lower()[:100] or
        "traceback" in result_text.lower()[:100] or
        (isinstance(parsed, dict) and parsed.get("error"))
    )
    
    if name == "web_search":
        if is_error:
            lines.append(f"   ❌ 搜索失败: {_extract_error_message(result_text, parsed)}")
        elif isinstance(parsed, list):
            count = len(parsed)
            lines.append(f"   ✓ 找到 {count} 个结果")
            for idx, item in enumerate(parsed[:5]):
                if isinstance(item, dict):
                    title = _truncate(item.get("title", "无标题"), 50)
                    url = item.get("url", "")
                    if url:
                        lines.append(f"      {idx + 1}. [{title}]({url})")
                    else:
                        lines.append(f"      {idx + 1}. {title}")
            if count > 5:
                lines.append(f"      ... 还有 {count - 5} 个结果")
        else:
            lines.append("   ✓ 搜索完成")
    elif name in ("read_file", "write_file", "str_replace"):
        action = {"read_file": "读取", "write_file": "写入", "str_replace": "编辑"}[name]
        path = args.get("path", "")
        if is_error:
            lines.append(f"   ❌ {action}失败: {_extract_error_message(result_text, parsed)}")
        else:
            lines.append(f"   ✓ {action}成功")
        if path:
            lines.append(f"      文件: `{_truncate(path, 60)}`")
    elif name == "bash":
        if is_error:
            lines.append(f"   ❌ 执行失败: {_extract_error_message(result_text, parsed)}")
        else:
            lines.append("   ✓ 执行成功")
        output = _truncate(result_text.replace("\n", " ").strip(), 80)
        if output and output != "Tool ran without output or errors":
            lines.append(f"      输出: {output}")
    else:
        if is_error:
            lines.append(f"   ❌ 失败: {_extract_error_message(result_text, parsed)}")
        else:
            lines.append("   ✓ 完成")
    return lines


def format_tool_history_markdown(history: list[dict[str, Any]]) -> str:
    """Format tool history as standard markdown.
    
    Uses blockquote (>) to wrap the entire tool section,
    creating visual distinction from final response.
    """
    if not history:
        return ""
    
    lines = []
    total = len(history)
    
    for i, item in enumerate(history):
        name = item.get("name", "unknown")
        args = item.get("args", {})
        result = item.get("result")
        status = item.get("status", "complete")
        
        icon = _get_tool_icon(name)
        is_last = (i == total - 1)
        connector = "└─" if is_last else "├─"
        label = _get_label_md(name, args)
        
        # Title line
        lines.append(f"{connector} {icon} {label}")
        
        # Result lines
        if result:
            lines.extend(_format_detailed_result_md(name, result, args))
        elif status == "pending":
            lines.append("   ⏳ 执行中...")
        
        # Add blank line between tools
        if not is_last:
            lines.append("")
    
    # Wrap entire section in blockquote for visual distinction
    content = "\n".join(lines)
    # Prepend "> " to each line for blockquote format
    quoted = "\n".join(f"> {line}" for line in content.split("\n"))
    return quoted
