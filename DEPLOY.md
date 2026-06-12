     1|# 灵枢 部署指南
     2|
     3|> 版本 0.1.0 | 统一智能体网关（NanoBot + Hermes + OpenClaw 合体）
     4|
     5|---
     6|
     7|## 一、环境要求
     8|
     9|| 项目 | 最低版本 |
    10||------|---------|
    11|| Python | 3.11+ |
    12|| pip | 最新 |
    13|| 系统 | Windows 10/11（VM 或物理机） |
    14|| 网络 | 能访问 api.deepseek.com |
    15|
    16|---
    17|
    18|## 二、首次安装
    19|
    20|### 方式 A：一键安装（推荐）
    21|
    22|```cmd
    23|cd C:\Users\admin\nodus
    24|python cli.py setup
    25|```
    26|
    27|这会自动完成：创建 venv → 安装依赖 → 提示配置 .env
    28|
    29|### 方式 B：手动安装
    30|
    31|```cmd
    32|cd C:\Users\admin\nodus
    33|
    34|# 1. 创建虚拟环境
    35|python -m venv venv
    36|
    37|# 2. 激活
    38|venv\Scripts\activate
    39|
    40|# 3. 安装依赖
    41|pip install -r requirements.txt
    42|pip install -e .
    43|
    44|# 4. 配置 API Key
    45|copy .env.example .env
    46|notepad .env    # 填入 DEEPSEEK_API_KEY
    47|```
    48|
    49|---
    50|
    51|## 三、配置
    52|
    53|### 查看当前配置
    54|
    55|```cmd
    56|python cli.py config --show
    57|```
    58|
    59|### 修改 .env 配置项
    60|
    61|```cmd
    62|python cli.py config --set DEEPSEEK_API_KEY=sk-xxx
    63|python cli.py config --set DEEPSEEK_MODEL=deepseek-v4-pro
    64|```
    65|
    66|### 通道配置
    67|
    68|通道配置在 `config/config.json` 中，包括钉钉、微信、飞书的凭证。
    69|
    70|编辑后生效，无需重启：
    71|
    72|```cmd
    73|notepad config\config.json
    74|```
    75|
    76|---
    77|
    78|## 四、自检诊断
    79|
    80|启动前先跑一遍诊断：
    81|
    82|```cmd
    83|python cli.py doctor
    84|```
    85|
    86|检查项：
    87|- Python 版本
    88|- 虚拟环境状态
    89|- .env 是否存在、API Key 是否配置
    90|- 依赖是否安装（openai, aiohttp, httpx, yaml）
    91|- config.json 是否存在
    92|- 目录结构（brain, gateway, executor, shared, data, skills）
    93|
    94|全部 [OK] 才能正常启动。
    95|
    96|---
    97|
    98|## 五、启动
    99|
   100|### 测试模式（控制台交互）
   101|
   102|```cmd
   103|python cli.py start
   104|```
   105|
   106|进入交互式对话，输入 `quit` 退出。
   107|
   108|预期行为：
   109|1. 输入消息 → 秒回 ACK（如"让我看看你的电脑~"）
   110|2. 思维层处理（意图识别 → 任务规划 → 工具调用）
   111|3. 翻译官把结果包装成自然语言回复
   112|
   113|### 服务模式（连接真实平台）
   114|
   115|```cmd
   116|python cli.py start --serve
   117|```
   118|
   119|启动后监听 `0.0.0.0:18791`，接入钉钉/微信/飞书 webhook。
   120|
   121|---
   122|
   123|## 六、从宿主机部署到 VM
   124|
   125|在宿主机（DESKTOP-CICB9QD）上：
   126|
   127|```cmd
   128|cd F:\nodus
   129|python cli.py deploy
   130|```
   131|
   132|或直接：
   133|
   134|```cmd
   135|F:\nodus\deploy.bat
   136|```
   137|
   138|部署脚本自动：
   139|1. 连接 Hyper-V VM
   140|2. 清理旧文件
   141|3. 同步全部 46 个文件（排除 venv/.env/缓存）
   142|4. 验证关键文件
   143|
   144|---
   145|
   146|## 七、常用命令速查
   147|
   148|| 命令 | 说明 |
   149||------|------|
   150|| `python cli.py setup` | 一键安装 |
   151|| `python cli.py doctor` | 系统诊断 |
   152|| `python cli.py config --show` | 查看配置 |
   153|| `python cli.py config --set K=V` | 修改配置 |
   154|| `python cli.py start` | 启动测试模式 |
   155|| `python cli.py start --serve` | 启动服务模式 |
   156|| `python cli.py deploy` | 部署到 VM |
   157|| `python cli.py version` | 版本信息 |
   158|
   159|安装完成后，可以用 `nodus` 替代 `python cli.py`：
   160|
   161|```cmd
   162|venv\Scripts\activate
   163|nodus doctor
   164|nodus start
   165|```
   166|
   167|---
   168|
   169|## 八、架构速览
   170|
   171|```
   172|用户消息
   173|  │
   174|  ├─ 网关层 (gateway)
   175|  │   ├─ EmotionDetector — 情绪感知（μs 级，不调 LLM）
   176|  │   └─ 智能 ACK — 根据关键词选有温度的秒回
   177|  │
   178|  ├─ 思维层 (brain)
   179|  │   ├─ Persona — 统一人设（柒月）
   180|  │   ├─ IntentParser → TaskPlanner — 意图 → 规划
   181|  │   ├─ 工具调用 → shell_exec / file_read / web_search
   182|  │   └─ 翻译官 — 原始数据 → 人话
   183|  │
   184|  ├─ 执行层 (executor)
   185|  │   └─ Shell / File / Browser / Search / Sandbox
   186|  │
   187|  └─ 进化层
   188|      └─ SelfSkillEngine — 自动生成技能 + 偏好学习
   189|```
   190|
   191|---
   192|
   193|## 九、故障排查
   194|
   195|### API Key 401 错误
   196|```cmd
   197|python cli.py config --show                    # 确认 Key 已配置
   198|python cli.py config --set DEEPSEEK_API_KEY=sk-xxx  # 重新设置
   199|```
   200|
   201|### ModuleNotFoundError
   202|```cmd
   203|pip install -e .                               # 重新安装
   204|pip install -r requirements.txt
   205|python cli.py doctor                           # 检查依赖
   206|```
   207|
   208|### 启动后无 ACK 回复
   209|检查 gateway 层：`gateway/__init__.py` 中 EmotionDetector 和 MessageRouter 是否正常加载。
   210|查看 VM 终端日志中的 `[qiyue.gateway]` 行。
   211|
   212|### VM 文件不更新
   213|```cmd
   214|# 在宿主机上
   215|python cli.py deploy
   216|```
   217|
   218|如果 deploy 失败，手动运行：
   219|```powershell
   220|powershell -File F:\nodus\deploy.ps1
   221|```
   222|