@echo off
cd /d "%~dp0"
if not exist node_modules (
  echo 正在安装依赖...
  call npm install
)
call npm start
