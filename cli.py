#!/usr/bin/env python3
"""
Nodus CLI — 灵枢

用法:
    nodus setup              一键安装
    nodus config --show      查看配置
    nodus doctor             系统诊断
    nodus start              启动
    nodus status             运行状态
    nodus version            版本信息
"""

import argparse
import sys
import os
import logging
from pathlib import Path

VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).parent


def setup_logging(level: str = "warn"):
    levels = {"silent": 100, "error": logging.ERROR, "warn": logging.WARNING,
              "info": logging.INFO, "debug": logging.DEBUG}
    logging.basicConfig(level=levels.get(level, logging.WARNING),
                        format="%(levelname)s: %(message)s")


def cmd_setup(args):
    """一键安装"""
    import subprocess, shutil

    print("=" * 40)
    print("  灵枢 Nodus 安装向导")
    print("=" * 40)

    v = sys.version_info
    print(f"[1/5] Python {v.major}.{v.minor}.{v.micro} — ",
          "[OK]" if v >= (3, 11) else "[FAIL] 需要 3.11+")
    if v < (3, 11):
        return 1

    venv_path = PROJECT_ROOT / "venv"
    if not venv_path.exists():
        print("[2/5] 创建虚拟环境...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        print("   [OK]")
    else:
        print("[2/5] 虚拟环境已存在 [OK]")

    print("[3/5] 安装依赖...")
    pip = str(venv_path / "Scripts" / "pip.exe")
    subprocess.run([pip, "install", "-r", str(PROJECT_ROOT / "requirements.txt")], check=False)
    subprocess.run([pip, "install", "-e", str(PROJECT_ROOT)], check=False)
    print("   [OK] nodus 命令已注册")

    env_path = PROJECT_ROOT / ".env"
    config_path = PROJECT_ROOT / "config.json"
    print("[4/5] 配置文件...")
    if not env_path.exists():
        env_example = PROJECT_ROOT / ".env.example"
        shutil.copy(env_example, env_path) if env_example.exists() else env_path.write_text(
            "# 灵枢 Nodus\nDEEPSEEK_API_KEY=***\nDEEPSEEK_MODEL=deepseek-v4-pro\n")
        print(f"   [WARN] 请编辑 .env 填入 DEEPSEEK_API_KEY: {env_path}")
    else:
        print("   .env [OK]")
    if not config_path.exists():
        example = PROJECT_ROOT / "config.example.json"
        shutil.copy(example, config_path) if example.exists() else None
        print(f"   [WARN] 请编辑 config.json 填入通道凭证: {config_path}")
    else:
        print("   config.json [OK]")

    print("[5/5] CLI 就绪")
    print()
    print("  下一步:")
    print("    1. 编辑 .env / config.json 填入凭证")
    print("    2. nodus doctor  检查环境")
    print("    3. nodus start   启动")
    return 0


def cmd_config(args):
    """配置管理"""
    if args.show:
        import json
        env_path = PROJECT_ROOT / ".env"
        config_path = PROJECT_ROOT / "config.json"

        if env_path.exists():
            print("=== .env ===")
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    masked = val[:8] + "****" if len(val) > 10 else "****"
                    print(f"  {key}={masked}")
        else:
            print(".env 不存在 — 运行 nodus setup")

        print()
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            for key in ("providers", "channels"):
                if key in cfg:
                    print(f"=== config.json → {key} ===")
                    print(json.dumps(cfg[key], indent=2, ensure_ascii=False))
        else:
            print("config.json 不存在")
        return 0

    if args.set:
        key, _, value = args.set.partition("=")
        if not value:
            print("[FAIL] 格式: nodus config --set KEY=VALUE")
            return 1
        env_path = PROJECT_ROOT / ".env"
        if not env_path.exists():
            env_path.write_text("")
        lines = env_path.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[OK] {key} 已更新")
        return 0

    print("用法: nodus config --show 或 nodus config --set KEY=value")
    return 0


def cmd_doctor(args):
    """系统诊断"""
    OK, FAIL = "[OK]", "[FAIL]"
    checks = []

    v = sys.version_info
    checks.append(("Python", f"{v.major}.{v.minor}.{v.micro}", v >= (3, 11)))

    in_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
    checks.append(("虚拟环境", "已激活" if in_venv else "未激活", in_venv))

    for mod, desc in [("openai", "LLM SDK"), ("aiohttp", "HTTP"), ("httpx", "HTTPX"), ("yaml", "YAML")]:
        try:
            __import__(mod)
            checks.append((f"  {desc}", "已安装", True))
        except ImportError:
            checks.append((f"  {desc}", "缺失", False))

    try:
        __import__("dingtalk_stream")
        checks.append(("  dingtalk-stream", "已安装", True))
    except ImportError:
        checks.append(("  dingtalk-stream", "缺失（钉钉通道需要）", False))

    config_path = PROJECT_ROOT / "config.json"
    checks.append(("config.json", "存在" if config_path.exists() else "缺失", config_path.exists()))

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        has_key = "DEEPSEEK_API_KEY" in content and "***" not in content
        checks.append(("API Key", "已配置" if has_key else "缺失", has_key))
    else:
        checks.append(("API Key", "缺失 (.env 不存在)", False))

    print("=" * 40)
    print("  灵枢 Nodus 系统诊断")
    print("=" * 40)
    all_ok = True
    for name, status, ok in checks:
        print(f"  {OK if ok else FAIL} {name}: {status}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("[OK] 一切正常 — nodus start 启动")
    else:
        print("[WARN] 发现问题 — nodus setup 修复")
    return 0 if all_ok else 1


def cmd_start(args):
    """启动"""
    env_path = PROJECT_ROOT / ".env"
    config_path = PROJECT_ROOT / "config.json"

    # 加载配置
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                if key and not os.getenv(key):
                    os.environ[key] = val.strip()

    if not os.getenv("DEEPSEEK_API_KEY"):
        import json
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            key = cfg.get("providers", {}).get("deepseek", {}).get("apiKey", "")
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[FAIL] 未配置 DEEPSEEK_API_KEY")
        print("  nodus config --set DEEPSEEK_API_KEY=sk-you...-key")
        return 1

    sys.path.insert(0, str(PROJECT_ROOT))
    import asyncio
    from main import main as run_gateway

    try:
        asyncio.run(run_gateway())
    except KeyboardInterrupt:
        print("\n再见~")
    return 0


def cmd_status(args):
    """运行状态"""
    import subprocess
    port = int(os.getenv("GATEWAY_PORT", "18800"))
    result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    running = any(f":{port}" in line and "LISTENING" in line for line in result.stdout.splitlines())

    print(f"  Nodus v{VERSION}")
    print(f"  端口 {port}: {'运行中' if running else '未运行'}")
    if running:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        except Exception:
            pass
    return 0


def cmd_version(args):
    """版本信息"""
    print(f"灵枢 Nodus v{VERSION}")
    print(f"Python  {sys.version.split()[0]}")
    print(f"路径    {PROJECT_ROOT}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="nodus",
        description="灵枢 Nodus — 统一智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  nodus setup\n  nodus doctor\n  nodus start\n  nodus status",
    )
    parser.add_argument("-V", "--version", action="store_true", help="显示版本")
    parser.add_argument("--log-level", default="warn",
                        choices=["silent", "error", "warn", "info", "debug"],
                        help="日志级别 (默认: warn)")

    sub = parser.add_subparsers(dest="command", help="命令")

    sub.add_parser("setup", help="一键安装")
    p = sub.add_parser("config", help="配置管理")
    p.add_argument("--show", action="store_true", help="显示配置")
    p.add_argument("--set", metavar="KEY=VALUE", help="设置配置项")
    sub.add_parser("doctor", help="系统诊断")
    sub.add_parser("start", help="启动")
    sub.add_parser("status", help="运行状态")
    sub.add_parser("version", help="版本信息")

    args = parser.parse_args()

    if args.version:
        cmd_version(args)
        return 0

    setup_logging(args.log_level)

    commands = {
        "setup": cmd_setup, "config": cmd_config, "doctor": cmd_doctor,
        "start": cmd_start, "status": cmd_status, "version": cmd_version,
    }

    if not args.command:
        parser.print_help()
        return 0

    handler = commands.get(args.command)
    if handler:
        sys.exit(handler(args) or 0)


if __name__ == "__main__":
    main()
