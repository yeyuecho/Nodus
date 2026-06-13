@echo off
chcp 65001 >nul
echo ========================================
echo   灵枢 Nodus — 一键部署
echo ========================================
echo.

:: 1. Python 检查
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [FAIL] 未找到 Python，请先安装 Python 3.11+
    pause & exit /b 1
)
echo [OK] Python 就绪

:: 2. 虚拟环境
if not exist venv (
    echo [..] 创建虚拟环境...
    python -m venv venv
)
echo [OK] venv 就绪

:: 3. 安装依赖
echo [..] 安装依赖...
venv\Scripts\pip install -r requirements.txt -q
echo [OK] 依赖安装完成

:: 4. 配置文件
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo [WARN] 请编辑 .env 填入 DEEPSEEK_API_KEY
    )
)
if not exist config.json (
    if exist config.example.json (
        copy config.example.json config.json >nul
        echo [WARN] 请编辑 config.json 填入通道凭证
    )
)
echo [OK] 配置文件就绪

:: 5. 创建目录
mkdir data logs skills 2>nul
echo [OK] 目录结构就绪

echo.
echo ========================================
echo   部署完成！
echo.
echo   下一步:
echo     1. 编辑 .env        填入 DEEPSEEK_API_KEY
echo     2. 编辑 config.json  填入通道凭证
echo     3. venv\Scripts\python main.py  启动
echo ========================================
pause