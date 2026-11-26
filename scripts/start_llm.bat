@echo off
chcp 65001 >nul
rem Запуск локального LLM-сервера llama.cpp для filldocx.
rem 1. Скачайте сборку llama.cpp для Windows с CUDA (llama-*-bin-win-cuda-*.zip) с
rem    https://github.com/ggml-org/llama.cpp/releases и распакуйте в models\llama.cpp
rem 2. Положите модель GGUF в models\llm (см. README, раздел «Модели»).
rem Сервер слушает только 127.0.0.1 — наружу ничего не уходит.

setlocal
set ROOT=%~dp0..
set LLAMA=%ROOT%\models\llama.cpp\llama-server.exe
set MODEL=%ROOT%\models\llm\Qwen3-4B-Instruct-2507-Q4_K_M.gguf

if not exist "%LLAMA%" (
  echo Не найден %LLAMA%
  exit /b 1
)
if not exist "%MODEL%" (
  echo Не найдена модель %MODEL%
  exit /b 1
)

rem -ngl 99    — все слои на видеокарту (для 4 ГБ VRAM и модели 4B Q4 помещается)
rem -c 8192    — контекст: промпт шаблона + диктовка 3 мин укладываются с запасом
"%LLAMA%" -m "%MODEL%" --host 127.0.0.1 --port 8080 -ngl 99 -c 8192 --temp 0
