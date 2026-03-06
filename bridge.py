#!/usr/bin/env python3
"""codex-discord-bridge — 单文件 Discord <-> Codex CLI 桥接。"""

import asyncio
import json
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

import discord


# --- Config (env vars) ---
def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"[FATAL] 环境变量 {key} 未设置，无法启动。", flush=True)
        sys.exit(1)
    return val


DISCORD_TOKEN = _require_env("DISCORD_TOKEN")
CHANNEL_ID = int(_require_env("CHANNEL_ID"))
WORKING_DIR = Path(os.environ.get("CODEX_CWD", os.environ.get("CLAUDE_CWD", str(Path.home()))))
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "").strip()
CODEX_TIMEOUT = max(30, int(os.environ.get("CODEX_TIMEOUT", os.environ.get("CLAUDE_TIMEOUT", "300"))))
CODEX_FULL_ACCESS = os.environ.get("CODEX_FULL_ACCESS") == "1"
MAX_RESPONSE_SIZE = 50_000

# --- Handoff constants ---
STATE_DIR = Path.home() / ".codex-discord-bridge"
SESSION_FILE = STATE_DIR / "session.json"
HANDOFF_DIR = STATE_DIR / "handoffs"
COLDSTART_BEGIN = "---COLDSTART-BEGIN---"
COLDSTART_END = "---COLDSTART-END---"
MAX_COLDSTART_LEN = 4000

HANDOFF_PROMPT = """\
你即将被新会话替换。请总结当前会话的完整上下文，生成一份交接文档。

## 要求
- 只基于本次会话中实际讨论过的内容，禁止脑补
- 冷启动部分必须自包含（新会话无法访问本次对话历史）

## 输出格式（严格遵守）

# 交接文档

## 当前目标
[一句话]

## 关键上下文与决策
- [已做的重要决策]
- [发现的关键约束]

## 已完成
- [本次会话中做了什么]

## 待办
- [未完成的任务，按优先级排列]

## 已知问题
- [阻塞项或风险]

---COLDSTART-BEGIN---
[将上述所有内容压缩为一段连贯的提示词。新会话收到这段文字后，应能完全理解上下文并继续工作。包含：目标、已完成的工作、关键决策、下一步行动、需要注意的约束。]
---COLDSTART-END---
"""

# --- UUID validation ---
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


# --- State ---
MAX_MSG_LEN = 2000

thread_id: str | None = None
call_lock = asyncio.Lock()
_ready_once = False
_inflight_proc: asyncio.subprocess.Process | None = None
_explicit_session: bool = False


# --- Session persistence ---
def load_session() -> str | None:
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            # Backward-compatible read for legacy field.
            return data.get("thread_id") or data.get("session_id")
        except (json.JSONDecodeError, KeyError, OSError, AttributeError):
            pass
    return None


def save_session(tid: str | None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"thread_id": tid}))
        tmp.rename(SESSION_FILE)
    except OSError as e:
        print(f"[bridge] save_session failed: {e}", flush=True)


# --- Subprocess helpers ---
async def _kill_proc(proc: asyncio.subprocess.Process):
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


def _extract_agent_text(item: dict) -> str:
    if item.get("type") != "agent_message":
        return ""
    text = item.get("text")
    if isinstance(text, str) and text:
        return text

    content = item.get("content")
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"output_text", "text"} and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return "".join(chunks)


def _build_codex_cmd(prompt: str, resume_tid: str | None = None) -> list[str]:
    cmd = [CODEX_BIN, "exec"]
    if resume_tid:
        cmd.append("resume")
    cmd.extend(["--json", "--skip-git-repo-check"])
    if CODEX_FULL_ACCESS:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if CODEX_MODEL:
        cmd.extend(["-m", CODEX_MODEL])
    if resume_tid:
        cmd.extend([resume_tid, prompt])
    else:
        cmd.append(prompt)
    return cmd


async def _run_codex(cmd: list[str]) -> tuple[list[str], int, str, str | None]:
    """Run codex CLI and return (parts, retcode, stderr, started_thread_id)."""
    global _inflight_proc
    print(f"[codex] cmd: {' '.join(cmd[:10])}...", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(WORKING_DIR),
    )
    if proc.stdout is None:
        raise RuntimeError("subprocess stdout is None — cannot read output")
    _inflight_proc = proc

    parts: list[str] = []
    total_size = 0
    started_tid: str | None = None

    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=CODEX_TIMEOUT)
            if not line:
                break

            line_str = line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                # Ignore non-JSON noise lines.
                print(f"[codex] non-json stdout: {line_str[:200]}", flush=True)
                continue

            event_type = data.get("type")
            if event_type == "thread.started":
                tid = data.get("thread_id")
                if isinstance(tid, str) and tid:
                    started_tid = tid
                continue

            if event_type == "item.completed":
                item = data.get("item")
                if isinstance(item, dict):
                    msg_text = _extract_agent_text(item)
                    if msg_text:
                        parts.append(msg_text)
                        total_size += len(msg_text)
                if total_size > MAX_RESPONSE_SIZE:
                    parts.append("\n...(响应过长，已截断)")
                    break
                continue

            if event_type == "agent_message":
                msg_text = _extract_agent_text(data)
                if msg_text:
                    parts.append(msg_text)
                    total_size += len(msg_text)
                if total_size > MAX_RESPONSE_SIZE:
                    parts.append("\n...(响应过长，已截断)")
                    break
    except asyncio.TimeoutError:
        print("[codex] readline timeout, killing proc", flush=True)
        await _kill_proc(proc)
        return parts or ["(子进程读取超时)"], 1, "timeout", started_tid
    except asyncio.CancelledError:
        await _kill_proc(proc)
        raise
    finally:
        _inflight_proc = None

    stderr_data = await proc.stderr.read() if proc.stderr else b""
    retcode = await proc.wait()
    return parts, retcode, stderr_data.decode(errors="replace")[:800], started_tid


async def _run_codex_once(prompt: str, resume_tid: str | None = None) -> tuple[str, int, str, str | None]:
    cmd = _build_codex_cmd(prompt, resume_tid=resume_tid)
    parts, retcode, stderr, started_tid = await _run_codex(cmd)
    return "\n".join(parts), retcode, stderr, started_tid


def _diag_text(retcode: int, stderr: str) -> str:
    return f"(无响应 | exit={retcode} | stderr={stderr[:300]})"


async def call_codex(prompt: str) -> str:
    global thread_id, _explicit_session

    requested_tid = thread_id
    if requested_tid:
        text, retcode, stderr, started_tid = await _run_codex_once(prompt, resume_tid=requested_tid)
        tid_mismatch = bool(started_tid and started_tid != requested_tid)
        resume_failed = retcode != 0 or started_tid is None or tid_mismatch

        if _explicit_session and resume_failed:
            details = [f"会话 `{requested_tid}` resume 失败。"]
            if retcode != 0:
                details.append(f"退出码：`{retcode}`")
            if started_tid is None:
                details.append("Codex 未返回 `thread.started.thread_id`。")
            elif tid_mismatch:
                details.append(
                    f"Codex 返回了不同 thread：`{started_tid}`（预期 `{requested_tid}`）。"
                )
            if stderr:
                details.append(f"stderr: `{stderr[:300]}`")
            details.append("已阻止自动切换。请使用 `/new` 或 `/connect <uuid>`。")
            # Keep explicit thread binding unchanged.
            thread_id = requested_tid
            save_session(thread_id)
            print("[codex] explicit thread resume failed; auto-fallback blocked", flush=True)
            return "\n".join(details)

        if not _explicit_session and resume_failed:
            # Codex may already have auto-started a new thread on resume.
            if retcode == 0 and started_tid:
                thread_id = started_tid
                _explicit_session = False
                save_session(thread_id)
                print(
                    f"[codex] implicit resume switched thread {requested_tid} -> {started_tid}",
                    flush=True,
                )
                return text or _diag_text(retcode, stderr)

            print(f"[codex] resume failed (exit={retcode}), creating new thread", flush=True)
            text, retcode, stderr, started_tid = await _run_codex_once(prompt, resume_tid=None)
            thread_id = started_tid
            _explicit_session = False
            save_session(thread_id)
            return text or _diag_text(retcode, stderr)

        thread_id = started_tid
        save_session(thread_id)
        return text or _diag_text(retcode, stderr)

    text, retcode, stderr, started_tid = await _run_codex_once(prompt, resume_tid=None)
    thread_id = started_tid
    _explicit_session = False
    save_session(thread_id)
    return text or _diag_text(retcode, stderr)


# --- Handoff helpers ---
def _parse_coldstart(text: str) -> tuple[str | None, str]:
    begin = text.find(COLDSTART_BEGIN)
    end = text.find(COLDSTART_END)
    if begin == -1 or end == -1 or end <= begin:
        return None, text
    cs = text[begin + len(COLDSTART_BEGIN):end].strip()
    if len(cs) > MAX_COLDSTART_LEN:
        cs = cs[:MAX_COLDSTART_LEN] + "\n\n[...冷启动文本已截断...]"
    return cs, text


def _save_handoff(old_tid: str, full_text: str, coldstart: str | None):
    try:
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (HANDOFF_DIR / f"{ts}_{old_tid}.md").write_text(full_text, encoding="utf-8")
        if coldstart:
            (HANDOFF_DIR / "latest_coldstart.md").write_text(coldstart, encoding="utf-8")
    except OSError as e:
        print(f"[bridge] _save_handoff failed: {e}", flush=True)


# --- Discord message splitting ---
async def send_long(channel: discord.abc.Messageable, text: str):
    while text:
        if len(text) <= MAX_MSG_LEN:
            await channel.send(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = text.rfind(" ", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        chunk = text[:cut]
        text = text[cut:].lstrip("\n")
        # Fix split code blocks
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            text = "```\n" + text
        await channel.send(chunk)


# --- Bot ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    global thread_id, _ready_once
    if _ready_once:
        print(f"[bridge] reconnected as {client.user}", flush=True)
        return
    _ready_once = True
    thread_id = load_session()
    print(f"[bridge] online as {client.user} | thread={thread_id} | channel={CHANNEL_ID}", flush=True)

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[bridge] WARNING: get_channel({CHANNEL_ID}) returned None", flush=True)
        return
    if os.environ.get("SELFTEST_ON_START") == "1":
        async with call_lock:
            try:
                response = await asyncio.wait_for(call_codex("请只回复两个字：在线"), timeout=60)
                await channel.send(f"[自检通过] {response}")
            except Exception as e:
                await channel.send(f"[自检失败] {e}")


@client.event
async def on_disconnect():
    print("[bridge] disconnected from Discord gateway", flush=True)


@client.event
async def on_resumed():
    print("[bridge] resumed Discord session", flush=True)


@client.event
async def on_error(event, *args, **kwargs):
    print(f"[bridge] event error in {event}: {sys.exc_info()[1]}", flush=True)


@client.event
async def on_message(msg: discord.Message):
    global thread_id, _explicit_session

    if msg.author.bot or msg.channel.id != CHANNEL_ID:
        return

    text = msg.content.strip()
    if not text:
        if msg.attachments:
            await msg.channel.send("暂不支持附件，请发送文字消息。")
        return

    # --- Commands ---
    if text.lower() == "/new":
        thread_id = None
        _explicit_session = False
        save_session(None)
        await msg.channel.send("会话已重置。")
        return

    if text.lower() == "/status":
        await msg.channel.send(
            f"Thread: `{thread_id or 'None'}`\n"
            f"Working dir: `{WORKING_DIR}`\n"
            f"Timeout: `{CODEX_TIMEOUT}s`\n"
            "Backend: `codex`"
        )
        return

    if text.lower() == "/help":
        await msg.channel.send(
            "**可用命令**\n"
            "`/new` — 重置会话\n"
            "`/status` — 查看状态\n"
            "`/connect [thread-id]` — 连接指定会话（无参数则重置）\n"
            "`/handoff` — 交接当前会话（总结→冷启动新会话）\n"
            "`/help` — 显示帮助\n"
            "其他消息 — 转发给 Codex"
        )
        return

    if text.lower().startswith("/connect"):
        parts = text.split(None, 1)
        if len(parts) == 1:
            # /connect with no argument — reset session
            thread_id = None
            _explicit_session = False
            save_session(None)
            await msg.channel.send("会话已重置。")
        else:
            candidate = parts[1].strip()
            if not _is_valid_uuid(candidate):
                await msg.channel.send(
                    f"无效的 thread ID 格式：`{candidate}`\n"
                    "请提供标准 UUID（例如：`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`）。"
                )
            else:
                thread_id = candidate
                _explicit_session = True
                save_session(thread_id)
                await msg.channel.send(f"已连接到 thread `{thread_id}`。")
        return

    if text.lower() == "/handoff":
        if not thread_id:
            await msg.channel.send("当前无活跃会话，无法执行交接。")
            return

        hourglass_added = False
        if call_lock.locked():
            await msg.add_reaction("\u23f3")
            hourglass_added = True

        async with call_lock:
            try:
                async with msg.channel.typing():
                    old_tid = thread_id
                    try:
                        handoff_response = await asyncio.wait_for(
                            call_codex(HANDOFF_PROMPT),
                            timeout=CODEX_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        await msg.channel.send(
                            f"交接超时（{CODEX_TIMEOUT}s）。会话未被清除，可继续使用或稍后重试。"
                        )
                        return
                    except Exception as e:
                        await msg.channel.send(
                            f"交接失败：{str(e)[:1000]}\n会话未被清除，可继续使用。"
                        )
                        return

                    coldstart, full_text = _parse_coldstart(handoff_response)
                    if coldstart is None:
                        await msg.channel.send(
                            "交接文档中未找到冷启动标记，会话未被清除。\n"
                            "以下是 Codex 的响应（供参考）：\n"
                            + handoff_response[:1500]
                        )
                        return

                    _save_handoff(old_tid, full_text, coldstart)

                    # Clear thread so call_codex will create a new one.
                    thread_id = None
                    _explicit_session = False
                    save_session(None)

                    try:
                        await asyncio.wait_for(call_codex(coldstart), timeout=CODEX_TIMEOUT)
                        new_tid = thread_id
                        if not new_tid:
                            raise RuntimeError("Codex 未返回新 thread_id")
                        save_session(new_tid)
                        await msg.channel.send(
                            "会话交接完成。\n"
                            f"旧会话：`{old_tid}`\n"
                            f"新会话：`{new_tid}`\n"
                            "冷启动已注入，可继续工作。"
                        )
                    except Exception as e:
                        # New session failed — restore old thread so user can recover.
                        thread_id = old_tid
                        _explicit_session = True
                        save_session(old_tid)
                        await msg.channel.send(
                            f"新会话启动失败：{str(e)[:800]}\n"
                            f"已恢复旧会话 `{old_tid}`，可用 `/connect {old_tid}` 手动重连。"
                        )
            finally:
                if hourglass_added:
                    try:
                        await msg.remove_reaction("\u23f3", client.user)
                    except discord.errors.HTTPException:
                        pass
        return

    # --- Forward to Codex ---
    hourglass_added = False
    if call_lock.locked():
        await msg.add_reaction("\u23f3")
        hourglass_added = True

    async with call_lock:
        try:
            async with msg.channel.typing():
                try:
                    response = await asyncio.wait_for(call_codex(text), timeout=CODEX_TIMEOUT)
                    await send_long(msg.channel, response)
                except asyncio.TimeoutError:
                    await msg.channel.send(f"超时（{CODEX_TIMEOUT}s）。可用 /new 重置会话后重试。")
                except FileNotFoundError:
                    await msg.channel.send(f"`{CODEX_BIN}` 不在 PATH 中。请检查环境配置。")
                except Exception as e:
                    await msg.channel.send(f"错误：{str(e)[:1500]}")
        finally:
            if hourglass_added:
                try:
                    await msg.remove_reaction("\u23f3", client.user)
                except discord.errors.HTTPException:
                    pass


# --- Entrypoint ---
async def main():
    loop = asyncio.get_running_loop()

    def _shutdown_handler():
        print("[bridge] SIGTERM received, shutting down...", flush=True)
        asyncio.ensure_future(_shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_handler)
        except NotImplementedError:
            pass  # Windows

    async with client:
        await client.start(DISCORD_TOKEN)


async def _shutdown():
    global _inflight_proc
    if _inflight_proc:
        await _kill_proc(_inflight_proc)
        _inflight_proc = None
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
