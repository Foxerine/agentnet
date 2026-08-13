#!/usr/bin/env python
"""AgentNet 安装器 —— 把启动器放进 PATH，可选地接上 Claude Code 钩子。

    python install.py            # 安装启动器
    python install.py --hooks    # 顺便接上 Claude Code 的 SessionStart / Stop 钩子
    python install.py --check    # 只检查现状，不改任何东西

为什么需要一个安装器而不是"把文件拷过去"：两个平台各有一个不显眼但会致命的坑，
手工拷贝几乎必然踩中其一（我们都踩过）：

1. **bash 不认 PATHEXT** —— 它只找名为 ``agentnet`` 的文件。只放 ``agentnet.cmd``
   的话，PowerShell 里一切正常，而从 Git Bash 调用全部 ``exit 127``。
   偏偏后台轮询器就是经 bash 启动的，于是整套唤醒机制静默失效。
2. **``.cmd`` 不是可执行映像** —— ``CreateProcess`` 起不了它，必须由 ``cmd.exe`` 解释；
   而 ``.cmd`` 文件本身只能用 ASCII 与 CRLF，否则 cmd.exe 按 OEM 码页解码会把注释
   拆成乱命令。

依赖：仅标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HOME = Path(os.environ.get('AGENTNET_HOME') or (Path.home() / '.agentnet'))
REPO = Path(__file__).resolve().parent
BIN = REPO / 'bin'
SCRIPT = REPO / 'scripts' / 'agentnet.py'

CLAUDE_SETTINGS = Path.home() / '.claude' / 'settings.json'
SKILL_SOURCE = REPO / 'skill' / 'SKILL.md'
SKILL_TARGET = Path.home() / '.claude' / 'skills' / 'agentnet' / 'SKILL.md'

SAFE_SUBCOMMANDS = (
    'register', 'charter', 'whoami', 'who', 'workspaces', 'send', 'reply', 'poll',
    'drain', 'thread', 'log', 'lock', 'sweep', 'exit', 'restore', 'archive',
    'reconcile', 'readme', 'dashboard',
)
"""可以免确认放行的子命令。

**刻意不包含** ``run`` / ``spawn`` / ``kill`` / ``reset``：它们能转发任意命令或控制进程。
绝不要用 ``Bash(agentnet:*)`` 这种通配规则——``agentnet run -- <任意命令>`` 会让它
等价于一个免确认的通用 shell。通配规则的危险不在于它匹配了什么，
而在于被匹配的程序自身有多大的转发能力。
"""


def target_bin_dir() -> Path:
    """挑一个已经在 PATH 上的目录放启动器。"""
    candidates: list[Path] = []
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA')
        if appdata:
            candidates.append(Path(appdata) / 'npm')          # npm 全局 bin，通常已在 PATH
        candidates.append(Path.home() / '.local' / 'bin')
    else:
        candidates += [Path.home() / '.local' / 'bin', Path('/usr/local/bin')]
    path_dirs = {os.path.normcase(p) for p in os.environ.get('PATH', '').split(os.pathsep) if p}
    for candidate in candidates:
        if os.path.normcase(str(candidate)) in path_dirs and candidate.is_dir():
            return candidate
    fallback = candidates[-1]
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[WARN] 没有候选目录在 PATH 上，用 {fallback}；请自行把它加进 PATH。")
    return fallback


def install_launchers(bin_dir: Path) -> list[Path]:
    written: list[Path] = []
    # POSIX 启动器：LF 行尾，否则 bash 把 shebang 读成 `/bin/sh\r`
    posix = bin_dir / 'agentnet'
    posix.write_text((BIN / 'agentnet').read_text(encoding='utf-8'),
                     encoding='utf-8', newline='\n')
    posix.chmod(0o755)
    written.append(posix)
    if os.name == 'nt':
        # Windows 启动器：CRLF 是 cmd.exe 的原生行尾
        win = bin_dir / 'agentnet.cmd'
        win.write_text((BIN / 'agentnet.cmd').read_text(encoding='utf-8'),
                       encoding='ascii', newline='\r\n')
        written.append(win)
    return written


def hook_command() -> str:
    python = sys.executable.replace('\\', '/')
    script = str(SCRIPT).replace('\\', '/')
    return f'"{python}" "{script}"'


def install_hooks() -> None:
    """把 SessionStart / Stop 钩子接进 Claude Code 的用户设置。

    钩子只是**便利路径**：它调的与 LLM 手动调的是同一批幂等命令，
    所以没有钩子的 harness 手动调一遍效果完全相同。
    """
    if not CLAUDE_SETTINGS.exists():
        print(f"[SKIP] 找不到 {CLAUDE_SETTINGS}，跳过钩子接线。")
        return
    raw = CLAUDE_SETTINGS.read_text(encoding='utf-8')
    settings = json.loads(raw)
    backup = CLAUDE_SETTINGS.with_suffix(f'.json.bak-agentnet')
    backup.write_text(raw, encoding='utf-8')

    base = hook_command()
    hooks = settings.setdefault('hooks', {})
    hooks['SessionStart'] = [{
        'matcher': 'startup|resume|fork',
        'hooks': [{'type': 'command', 'command': f'{base} hook session-start', 'timeout': 15}],
    }]
    hooks['Stop'] = [{
        'hooks': [{'type': 'command', 'command': f'{base} hook stop', 'timeout': 15}],
    }]

    allow = settings.setdefault('permissions', {}).setdefault('allow', [])
    for sub in SAFE_SUBCOMMANDS:
        rule = f'Bash(agentnet {sub}:*)'
        if rule not in allow:
            allow.append(rule)

    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + '\n',
                               encoding='utf-8')
    print(f"[OK] 钩子已接线，原设置备份于 {backup.name}")
    print(f"     已放行 {len(SAFE_SUBCOMMANDS)} 个安全子命令；"
          f"run / spawn / kill / reset 仍需人工确认（它们能转发任意命令或控制进程）。")


def report() -> None:
    print(f"AGENTNET_HOME : {HOME}")
    print(f"仓库          : {REPO}")
    print(f"脚本          : {SCRIPT}  {'存在' if SCRIPT.exists() else '**缺失**'}")
    found = shutil.which('agentnet')
    print(f"PATH 上的启动器: {found or '**未找到**'}")
    if CLAUDE_SETTINGS.exists():
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding='utf-8'))
        events = list((settings.get('hooks') or {}).keys())
        wired = [e for e in ('SessionStart', 'Stop')
                 if any('agentnet' in json.dumps(h) for h in (settings['hooks'].get(e) or []))]
        print(f"Claude 钩子   : 已配置事件 {events or '无'}；AgentNet 已接入 {wired or '无'}")
    print(f"Agent skill   : {skill_state()}")


def install_skill() -> None:
    """把 SKILL.md 装进 ``~/.claude/skills/agentnet/``。

    装的是**副本**而不是符号链接：Windows 上建符号链接需要开发者模式或管理员权限，
    在一个"给别人克隆就能用"的安装器里不能假设有。副本会漂移，所以 ``--check``
    会比对两份内容并在不一致时说出来。
    """
    if not SKILL_SOURCE.exists():
        raise SystemExit(f'[ERR] 找不到 {SKILL_SOURCE}')
    SKILL_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL_SOURCE, SKILL_TARGET)
    print(f"[OK] 已安装 skill → {SKILL_TARGET}")


def skill_state() -> str:
    if not SKILL_TARGET.exists():
        return '未安装（`python install.py --skill`）'
    same = SKILL_TARGET.read_bytes() == SKILL_SOURCE.read_bytes()
    return '已安装，与仓库一致' if same else '**已安装但与仓库不一致** —— 重跑 --skill 覆盖'


def main() -> None:
    parser = argparse.ArgumentParser(description='安装 AgentNet 启动器（可选：接上 Claude Code 钩子）')
    parser.add_argument('--hooks', action='store_true', help='顺便接上 SessionStart / Stop 钩子')
    parser.add_argument('--skill', action='store_true',
                        help='顺便把 SKILL.md 装进 ~/.claude/skills/agentnet/')
    parser.add_argument('--check', action='store_true', help='只报告现状，不做任何改动')
    args = parser.parse_args()

    if not SCRIPT.exists():
        raise SystemExit(f'[ERR] 找不到 {SCRIPT} —— 请在仓库根目录运行本脚本')

    if args.check:
        report()
        return

    if REPO.resolve() != HOME.resolve():
        print(f"[WARN] 仓库不在 {HOME}。启动器会按 AGENTNET_HOME 找脚本，")
        print(f"       所以请设置 AGENTNET_HOME={REPO}，或把仓库克隆到 {HOME}。")

    for path in install_launchers(target_bin_dir()):
        print(f"[OK] 已安装 {path}")
    if args.hooks:
        install_hooks()
    if args.skill:
        install_skill()

    print()
    print('接下来：')
    print('  agentnet register --topics "你负责的主题"')
    print('  agentnet drain            # 先领信')
    print('  agentnet poll &           # 再后台常驻')
    print('  agentnet dashboard --open # 看板')


if __name__ == '__main__':
    main()
