# Nodus Agent -- Qiyue

Based on Nous Research Nodus framework.
Multi-platform messaging (DingTalk/Feishu/WeChat)
and smart home control (Xiaomi WiFi direct).

## Quick Deploy

### Prerequisites
- Windows 11, Python 3.11+
- uv (recommended) or pip
- Git Bash / MSYS2

### Install

  git clone git@github.com:yeyuecho/Nodus.git
  cd Nodus
  uv sync

### Configure

1. Create .env file with your API keys
2. Edit ~/.nodus/config.yaml or run: nodus setup
3. Optional: configure Xiaomi devices

### Run

  nodus gateway          (foreground, debug)
  nodus gateway start    (background service)
  nodus gateway stop

Default HTTP port: 18789

## Redeploy from GitHub (from zero to running)

  git clone git@github.com:yeyuecho/Nodus.git
  cd Nodus
  uv sync

  cp .env.example .env
  # Edit .env and fill in your DEEPSEEK_API_KEY

  # Copy your existing config backup, or create fresh:
  # nodus setup

  nodus gateway

## Supported Platforms

- DingTalk (enterprise app webhook)
- Feishu (custom app webhook)
- WeChat (official account dev server)

## Skills

- mijia-control: Xiaomi WiFi smart home control
- dingtalk-document: DingTalk knowledge base (via dws CLI)

---

Powered by Nous Research
