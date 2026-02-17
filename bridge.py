#!/usr/bin/env python3
"""claude-discord-bridge — 单文件 Discord <-> Claude Code CLI 桥接。"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import discord

# --- Config (env vars) ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
WORKING_DIR = Path(os.environ.get("CLAUDE_CWD", str(Path.home())))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))

# --- State ---
STATE_DIR = Path.home() / ".claude-discord-bridge"
SESSION_FILE = STATE_DIR / "session.json"
MAX_MSG_LEN = 2000

session_id: str | None = None
call_lock = asyncio.Lock()


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
    SESSION_FILE.write_text(json.dumps({"session_id": sid}))


# --- Claude Code CLI ---
async def _run_claude(cmd: list[str]) -> tuple[list[str], int, str]:
    """Run claude CLI, return (text_parts, retcode, stderr)."""
    print(f"[claude] cmd: {' '.join(cmd[:7])}...", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(WORKING_DIR),
    )

    parts: list[str] = []
    assert proc.stdout is not None

    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        line_str = line.decode().strip()
        if not line_str:
            continue
        try:
            data = json.loads(line_str)
            if data.get("type") == "assistant":
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        parts.append(block["text"])
            elif data.get("type") == "result":
                result_text = data.get("result", "")
                if result_text and not parts:
                    parts.append(result_text)
        except (json.JSONDecodeError, KeyError):
            # Non-JSON line, might be plain text output
            print(f"[claude] non-json stdout: {line_str[:200]}", flush=True)
            continue

    stderr_data = await proc.stderr.read() if proc.stderr else b""
    retcode = await proc.wait()
    return parts, retcode, stderr_data.decode()[:500]


async def call_claude(prompt: str) -> str:
    global session_id

    cmd = [CLAUDE_BIN, "-p", "--verbose", "--output-format", "stream-json"]

    if session_id:
        cmd.extend(["-r", session_id])
    else:
        session_id = str(uuid.uuid4())
        cmd.extend(["--session-id", session_id])

    cmd.append(prompt)

    parts, retcode, stderr = await _run_claude(cmd)

    # If resume failed (exit=1), fallback to new session
    if retcode != 0 and session_id:
        print(f"[claude] resume failed (exit={retcode}), retrying with new session", flush=True)
        session_id = str(uuid.uuid4())
        cmd = [CLAUDE_BIN, "-p", "--verbose", "--output-format", "stream-json",
               "--session-id", session_id, prompt]
        parts, retcode, stderr = await _run_claude(cmd)

    save_session(session_id)

    if not parts:
        diag = f"(无响应 | exit={retcode} | stderr={stderr})"
        print(f"[claude] {diag}", flush=True)
        return diag

    return "\n".join(parts)


# --- Discord message splitting ---
async def send_long(channel: discord.abc.Messageable, text: str):
    while text:
        if len(text) <= MAX_MSG_LEN:
            await channel.send(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        await channel.send(text[:cut])
        text = text[cut:].lstrip("\n")


# --- Bot ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    global session_id
    session_id = load_session()
    print(f"[bridge] online as {client.user} | session={session_id} | channel={CHANNEL_ID}")

    # Startup self-test: call Claude and post result to channel
    channel = client.get_channel(CHANNEL_ID)
    if channel and os.environ.get("SELFTEST_ON_START"):
        try:
            response = await asyncio.wait_for(call_claude("请只回复两个字：在线"), timeout=60)
            await channel.send(f"[自检通过] {response}")
        except Exception as e:
            await channel.send(f"[自检失败] {e}")


@client.event
async def on_message(msg: discord.Message):
    global session_id

    if msg.author.bot or msg.channel.id != CHANNEL_ID:
        return

    text = msg.content.strip()
    if not text:
        return

    # --- Commands ---
    if text.lower() == "/new":
        session_id = None
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

    # --- Forward to Claude ---
    if call_lock.locked():
        await msg.add_reaction("\u23f3")

    async with call_lock:
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


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
