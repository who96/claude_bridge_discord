#!/usr/bin/env python3
"""claude-discord-bridge — 单文件 Discord <-> Claude Code CLI 桥接。"""

import asyncio
import json
import os
import re
import signal
import sys
import uuid
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
WORKING_DIR = Path(os.environ.get("CLAUDE_CWD", str(Path.home())))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_SKIP_PERMS = os.environ.get("CLAUDE_SKIP_PERMISSIONS") == "1"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
CLAUDE_TIMEOUT = max(30, int(os.environ.get("CLAUDE_TIMEOUT", "300")))
MAX_RESPONSE_SIZE = 50_000

# --- Handoff constants ---
HANDOFF_DIR = Path.home() / ".claude-discord-bridge" / "handoffs"
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
STATE_DIR = Path.home() / ".claude-discord-bridge"
SESSION_FILE = STATE_DIR / "session.json"
MAX_MSG_LEN = 2000

session_id: str | None = None
call_lock = asyncio.Lock()
_ready_once = False
_inflight_proc: asyncio.subprocess.Process | None = None
_explicit_session: bool = False


# --- Session persistence ---
def load_session() -> str | None:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text()).get("session_id")
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def save_session(sid: str | None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"session_id": sid}))
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


# --- Claude Code CLI ---
async def _run_claude(cmd: list[str]) -> tuple[list[str], int, str]:
    """Run claude CLI, return (text_parts, retcode, stderr)."""
    global _inflight_proc
    print(f"[claude] cmd: {' '.join(cmd[:7])}...", flush=True)

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
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=CLAUDE_TIMEOUT)
            if not line:
                break
            line_str = line.decode(errors="replace").strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
                if data.get("type") == "assistant":
                    for block in data.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            parts.append(block["text"])
                            total_size += len(block["text"])
                elif data.get("type") == "result":
                    result_text = data.get("result", "")
                    if result_text and not parts:
                        parts.append(result_text)
                        total_size += len(result_text)
            except (json.JSONDecodeError, KeyError):
                print(f"[claude] non-json stdout: {line_str[:200]}", flush=True)
            if total_size > MAX_RESPONSE_SIZE:
                parts.append("\n...(响应过长，已截断)")
                break
    except asyncio.TimeoutError:
        print("[claude] readline timeout, killing proc", flush=True)
        await _kill_proc(proc)
        return parts or ["(子进程读取超时)"], 1, "timeout"
    except asyncio.CancelledError:
        await _kill_proc(proc)
        raise
    finally:
        _inflight_proc = None

    stderr_data = await proc.stderr.read() if proc.stderr else b""
    retcode = await proc.wait()
    return parts, retcode, stderr_data.decode(errors="replace")[:500]


async def call_claude(prompt: str) -> str:
    global session_id, _explicit_session

    cmd = [CLAUDE_BIN]
    if CLAUDE_SKIP_PERMS:
        cmd.append("--dangerously-skip-permissions")
    if CLAUDE_MODEL:
        cmd.extend(["--model", CLAUDE_MODEL])
    cmd.extend(["-p", "--verbose", "--output-format", "stream-json"])

    if session_id:
        cmd.extend(["-r", session_id])
    else:
        session_id = str(uuid.uuid4())
        cmd.extend(["--session-id", session_id])

    cmd.extend(["--", prompt])

    parts, retcode, stderr = await _run_claude(cmd)

    # If resume failed (exit=1), fallback behaviour depends on _explicit_session
    if retcode != 0 and session_id:
        if _explicit_session:
            # User explicitly connected to this session — do NOT silently fall back
            diag = (
                f"会话 `{session_id}` resume 失败（exit={retcode}）。\n"
                "该会话可能已过期或不存在。\n"
                "使用 `/new` 开启新会话，或 `/connect <uuid>` 连接其他会话。"
            )
            print(f"[claude] explicit session resume failed, not falling back", flush=True)
            return diag

        print(f"[claude] resume failed (exit={retcode}), retrying with new session", flush=True)
        session_id = str(uuid.uuid4())
        cmd = [CLAUDE_BIN]
        if CLAUDE_SKIP_PERMS:
            cmd.append("--dangerously-skip-permissions")
        if CLAUDE_MODEL:
            cmd.extend(["--model", CLAUDE_MODEL])
        cmd.extend(["-p", "--verbose", "--output-format", "stream-json",
                    "--session-id", session_id, "--", prompt])
        parts, retcode, stderr = await _run_claude(cmd)

    save_session(session_id)

    if not parts:
        diag = f"(无响应 | exit={retcode} | stderr={stderr})"
        print(f"[claude] {diag}", flush=True)
        return diag

    return "\n".join(parts)


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


def _save_handoff(old_sid: str, full_text: str, coldstart: str | None):
    try:
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (HANDOFF_DIR / f"{ts}_{old_sid}.md").write_text(full_text, encoding="utf-8")
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
    global session_id, _ready_once
    if _ready_once:
        print(f"[bridge] reconnected as {client.user}", flush=True)
        return
    _ready_once = True
    session_id = load_session()
    print(f"[bridge] online as {client.user} | session={session_id} | channel={CHANNEL_ID}", flush=True)

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[bridge] WARNING: get_channel({CHANNEL_ID}) returned None", flush=True)
        return
    if os.environ.get("SELFTEST_ON_START") == "1":
        async with call_lock:
            try:
                response = await asyncio.wait_for(call_claude("请只回复两个字：在线"), timeout=60)
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
    global session_id, _explicit_session

    if msg.author.bot or msg.channel.id != CHANNEL_ID:
        return

    text = msg.content.strip()
    if not text:
        if msg.attachments:
            await msg.channel.send("暂不支持附件，请发送文字消息。")
        return

    # --- Commands ---
    if text.lower() == "/new":
        session_id = None
        _explicit_session = False
        save_session(None)
        await msg.channel.send("会话已重置。")
        return

    if text.lower() == "/status":
        await msg.channel.send(
            f"Session: `{session_id or 'None'}`\n"
            f"Working dir: `{WORKING_DIR}`\n"
            f"Timeout: `{CLAUDE_TIMEOUT}s`"
        )
        return

    if text.lower() == "/help":
        await msg.channel.send(
            "**可用命令**\n"
            "`/new` — 重置会话\n"
            "`/status` — 查看状态\n"
            "`/connect [session-id]` — 连接指定会话（无参数则重置）\n"
            "`/handoff` — 交接当前会话（总结→冷启动新会话）\n"
            "`/help` — 显示帮助\n"
            "其他消息 — 转发给 Claude"
        )
        return

    if text.lower().startswith("/connect"):
        parts = text.split(None, 1)
        if len(parts) == 1:
            # /connect with no argument — reset session
            session_id = None
            _explicit_session = False
            save_session(None)
            await msg.channel.send("会话已重置。")
        else:
            candidate = parts[1].strip()
            if not _is_valid_uuid(candidate):
                await msg.channel.send(
                    f"无效的 session ID 格式：`{candidate}`\n"
                    "请提供标准 UUID（例如：`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`）。"
                )
            else:
                session_id = candidate
                _explicit_session = True
                save_session(session_id)
                await msg.channel.send(f"已连接到会话 `{session_id}`。")
        return

    if text.lower() == "/handoff":
        if not session_id:
            await msg.channel.send("当前无活跃会话，无法执行交接。")
            return

        hourglass_added = False
        if call_lock.locked():
            await msg.add_reaction("\u23f3")
            hourglass_added = True

        async with call_lock:
            try:
                async with msg.channel.typing():
                    old_sid = session_id
                    try:
                        handoff_response = await asyncio.wait_for(
                            call_claude(HANDOFF_PROMPT),
                            timeout=CLAUDE_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        await msg.channel.send(
                            f"交接超时（{CLAUDE_TIMEOUT}s）。会话未被清除，可继续使用或稍后重试。"
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
                            "以下是 Claude 的响应（供参考）：\n"
                            + handoff_response[:1500]
                        )
                        return

                    _save_handoff(old_sid, full_text, coldstart)

                    # Clear session so call_claude will create a new one
                    session_id = None
                    _explicit_session = False

                    try:
                        await asyncio.wait_for(
                            call_claude(coldstart),
                            timeout=CLAUDE_TIMEOUT,
                        )
                        new_sid = session_id
                        save_session(new_sid)
                        await msg.channel.send(
                            "会话交接完成。\n"
                            f"旧会话：`{old_sid}`\n"
                            f"新会话：`{new_sid}`\n"
                            "冷启动已注入，可继续工作。"
                        )
                    except Exception as e:
                        # New session failed — restore old sid so user can recover
                        session_id = old_sid
                        _explicit_session = True
                        save_session(old_sid)
                        await msg.channel.send(
                            f"新会话启动失败：{str(e)[:800]}\n"
                            f"已恢复旧会话 `{old_sid}`，可用 `/connect {old_sid}` 手动重连。"
                        )
            finally:
                if hourglass_added:
                    try:
                        await msg.remove_reaction("\u23f3", client.user)
                    except discord.errors.HTTPException:
                        pass
        return

    # --- Forward to Claude ---
    hourglass_added = False
    if call_lock.locked():
        await msg.add_reaction("\u23f3")
        hourglass_added = True

    async with call_lock:
        try:
            async with msg.channel.typing():
                try:
                    response = await asyncio.wait_for(
                        call_claude(text),
                        timeout=CLAUDE_TIMEOUT,
                    )
                    await send_long(msg.channel, response)
                except asyncio.TimeoutError:
                    await msg.channel.send(f"超时（{CLAUDE_TIMEOUT}s）。可用 /new 重置会话后重试。")
                except FileNotFoundError:
                    await msg.channel.send(f"`{CLAUDE_BIN}` 不在 PATH 中。请检查环境配置。")
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
