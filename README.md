# 灵枢 Nodus — 统一智能体

Hermes（思维）+ NanoBot（通讯）+ OpenClaw（执行）= 单一 Python 进程。

## 快速开始

```bash
git clone git@github.com:yeyuecho/Nodus.git
cd Nodus
python cli.py setup           # 首次：装环境 + 注册 nodus
nodus config --set DEEPSEEK_API_KEY=*** start  # 之后全用 nodus
```

首次 `python cli.py setup` 做三件事：创建 venv → 安装依赖 → 注册 `nodus` 命令。之后就全是 `nodus` 了。

## 命令

| 命令 | 说明 |
|------|------|
| `nodus doctor` | 系统诊断 |
| `nodus config --show` | 查看配置 |
| `nodus config --set K=V` | 修改配置 |
| `nodus start` | 启动（交互） |
| `nodus start --serve` | 启动（服务） |
| `nodus status` | 运行状态 |
| `nodus version` | 版本 |

## 项目结构

```
Nodus/
├── cli.py               # CLI 入口
├── main.py              # 网关入口
├── brain/               # 思维层
├── gateway/             # 网关层
├── executor/            # 执行层
├── shared/              # 共享层
├── config/              # 默认配置
├── SOUL.md / MEMORY.md / RULES.md / USER.md
```
