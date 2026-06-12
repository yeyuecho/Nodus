#!/usr/bin/env python3
"""
Nodus CLI — 灵枢

用法:
    nodus setup         一键安装（venv + pip + .env 向导）
    nodus config        查看/修改配置
    nodus doctor        系统自检诊断
    nodus start         启动网关（--test 交互测试 / --serve 服务模式）
    nodus deploy        部署到 VM
    nodus version       查看版本
"""

import argparse
import sys
import os
from pathlib import Path

VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).parent


def ensure_venv():
    """确保在 venv 中运行，给出友好提示"""
    in_venv = hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )
    if not in_venv:
        print("[WARN]  建议在虚拟环境中运行。运行 nodus setup 自动创建。")
        print(f"   或手动: python -m venv venv && venv\\Scripts\\activate")
    return in_venv


def cmd_setup(args):
    """一键安装"""
    import subprocess
    import shutil

    print("=" * 40)
    print("  灵枢 安装向导")
    print("=" * 40)
    print()

    # 1. Python 版本检查
    v = sys.version_info
    print(f"[1/5] Python {v.major}.{v.minor}.{v.micro} — ", end="")
    if v >= (3, 11):
        print("[OK]")
    else:
        print(f"[FAIL] 需要 3.11+，当前 {v.major}.{v.minor}")
        return 1

    # 2. 虚拟环境
    venv_path = PROJECT_ROOT / "venv"
    if not venv_path.exists():
        print("[2/5] 创建虚拟环境...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
        print("   [OK] venv 已创建")
    else:
        print("[2/5] 虚拟环境已存在 [OK]")

    # 3. pip 安装
    print("[3/5] 安装依赖...")
    pip = str(venv_path / "Scripts" / "pip.exe")
    subprocess.run([pip, "install", "-e", str(PROJECT_ROOT), "-q"], check=False)
    subprocess.run([pip, "install", "-r", str(PROJECT_ROOT / "requirements.txt"), "-q"], check=False)
    print("   [OK] 依赖安装完成")

    # 4. .env 配置
    env_path = PROJECT_ROOT / ".env"
    env_example = PROJECT_ROOT / ".env.example"
    if not env_path.exists():
        print("[4/5] 配置 .env...")
        if env_example.exists():
            shutil.copy(env_example, env_path)
        else:
            env_path.write_text(
                "# 灵枢 Nodus 配置\n"
                "DEEPSEEK_API_KEY=***\n"
                "DEEPSEEK_MODEL=deepseek-v4-pro\n"
            )
        print("   [WARN]  请编辑 .env 填入你的 DEEPSEEK_API_KEY")
        print(f"   文件: {env_path}")
    else:
        print("[4/5] .env 已存在 [OK]")

    # 5. 完成
    exe_path = venv_path / "Scripts" / "nodus.exe"
    print(f"[5/5] CLI 已安装: {exe_path}")
    print()
    print("=" * 40)
    print("  安装完成！")
    print()
    print("  下一步:")
    print("    1. 编辑 .env 填入 API Key")
    print("    2. nodus doctor  检查环境")
    print("    3. nodus start   启动测试")
    print("=" * 40)
    return 0


def cmd_config(args):
    """配置管理"""
    env_path = PROJECT_ROOT / ".env"
    config_path = PROJECT_ROOT / "config.json"

    if args.show:
        print("=== .env ===")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, val = line.split("=", 1)
                        masked = val[:8] + "****" if len(val) > 8 else "****"
                        print(f"  {key}={masked}")
                    else:
                        print(f"  {line}")
        else:
            print("  (不存在)")
        print()
        print("=== config.json ===")
        if config_path.exists():
            import json
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            print(json.dumps(cfg, indent=2, ensure_ascii=False))
        else:
            print("  (不存在)")
        return 0

    if args.set:
        key, _, value = args.set.partition("=")
        if not value:
            print(f"[FAIL] 格式: nodus config --set KEY=VALUE")
            return 1
        if not env_path.exists():
            env_path.write_text("", encoding="utf-8")
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

    return 0


def cmd_doctor(args):
    """系统诊断"""
    import subprocess

    OK = "[OK]"
    FAIL = "[FAIL]"

    print("=" * 40)
    print("  灵枢 Nodus 系统诊断")
    print("=" * 40)
    print()

    checks = []

    # Python
    v = sys.version_info
    checks.append(("Python", f"{v.major}.{v.minor}.{v.micro}", v >= (3, 11)))

    # venv
    in_venv = hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )
    checks.append(("虚拟环境", "已激活" if in_venv else "未激活", in_venv))

    # .env
    env_path = PROJECT_ROOT / ".env"
    env_ok = env_path.exists()
    api_key_set = False
    if env_ok:
        content = env_path.read_text(encoding="utf-8")
        api_key_set = "DEEPSEEK_API_KEY" in content and "sk-your-key" not in content
    checks.append((".env", "存在" if env_ok else "缺失", env_ok))
    checks.append(("API Key", "已配置" if api_key_set else "未配置或占位", api_key_set))

    # 依赖
    deps = {"openai": "LLM SDK", "aiohttp": "HTTP 服务", "httpx": "Web 请求", "yaml": "YAML 解析"}
    for mod, desc in deps.items():
        try:
            __import__(mod)
            checks.append((f"  {desc} ({mod})", "已安装", True))
        except ImportError:
            checks.append((f"  {desc} ({mod})", "缺失", False))

    # config.json
    config_path = PROJECT_ROOT / "config.json"
    checks.append(("config.json", "存在" if config_path.exists() else "缺失", config_path.exists()))

    # 目录结构
    for d in ["brain", "gateway", "executor", "shared", "data", "skills"]:
        exists = (PROJECT_ROOT / d).is_dir()
        checks.append((f"目录 {d}/", "存在" if exists else "缺失", exists))

    # 输出
    all_ok = True
    for name, status, ok in checks:
        icon = OK if ok else FAIL
        print(f"  {icon} {name}: {status}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("[OK] 一切正常，可以启动！")
        print("   运行: nodus start")
    else:
        print("[WARN] 发现问题，运行 nodus setup 修复。")

    return 0 if all_ok else 1


def cmd_start(args):
    """启动网关"""
    mode = "serve" if args.serve else "test"

    print(f"灵枢 Nodus 启动中 ({mode} 模式)...")
    print()

    # 快速自检
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists() or "DEEPSEEK_API_KEY" not in env_path.read_text(encoding="utf-8"):
        print("[FAIL] 未配置 DEEPSEEK_API_KEY，请先运行 nodus setup")
        return 1

    # 加载 .env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            if key and not os.getenv(key):
                os.environ[key] = val.strip()

    sys.path.insert(0, str(PROJECT_ROOT))

    import asyncio
    from main import main as run_gateway

    if args.serve:
        sys.argv = [sys.argv[0], "--serve"]

    try:
        asyncio.run(run_gateway())
    except KeyboardInterrupt:
        print("\n再见~")
    return 0


def cmd_deploy(args):
    """部署到 VM"""
    deploy_ps1 = PROJECT_ROOT / "deploy.ps1"
    if not deploy_ps1.exists():
        print("[FAIL] 未找到 deploy.ps1")
        return 1

    import subprocess
    print("灵枢 Nodus 部署到 Hyper-V VM...")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(deploy_ps1)],
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode


def cmd_version(args):
    """版本信息"""
    print(f"灵枢 Nodus v{VERSION}")
    print(f"Python  {sys.version}")
    print(f"路径    {PROJECT_ROOT}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="nodus",
        description="灵枢 Nodus — 统一智能体网关",
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    sub.add_parser("setup", help="一键安装（venv + pip + .env）")

    p_config = sub.add_parser("config", help="配置管理")
    p_config.add_argument("--show", action="store_true", help="显示当前配置")
    p_config.add_argument("--set", metavar="KEY=VALUE", help="设置配置项（写入 .env）")

    sub.add_parser("doctor", help="系统自检诊断")

    p_start = sub.add_parser("start", help="启动网关")
    p_start.add_argument("--serve", action="store_true", help="服务模式（连接真实平台）")

    sub.add_parser("deploy", help="部署到 VM")
    sub.add_parser("version", help="查看版本")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "setup": cmd_setup,
        "config": cmd_config,
        "doctor": cmd_doctor,
        "start": cmd_start,
        "deploy": cmd_deploy,
        "version": cmd_version,
    }

    handler = commands.get(args.command)
    if handler:
        sys.exit(handler(args) or 0)


if __name__ == "__main__":
    main()
