# Christina — Telegram AI Assistant

Личный AI-ассистент в Telegram на базе **Google Gemini API**, с интеграцией Gmail и автоматическим мониторингом портала DGIST.

## 🚀 Основные возможности

1. **Google Gemini AI (`gemini-3.5-flash-lite`):**
   * **Голосовые сообщения:** прямое распознавание аудио `audio/ogg` без промежуточной конвертации.
   * **Мультимодальное зрение:** анализ фотографий и документов по текстовым запросам.
   * **Умный чат:** изолированный контекст диалога для каждого пользователя.

2. **Работа с Gmail:**
   * Автоматическое распознавание намерений написать письмо (Structured Outputs с Pydantic).
   * Поиск адресов получателей по истории переписки.
   * Генерация черновика и редактирование прямо через Telegram (Inline-режим или Telegram Mini App).
   * Мгновенная отправка или отмена в один клик.

3. **Мониторинг портала DGIST:**
   * Фоновый мониторинг объявлений каждые 11 часов.
   * Автоматический вход через Playwright Chromium с обработкой 2FA OTP через IMAP.
   * Нейросетевая суммаризация новых важных объявлений на русском языке.
   * Ручная проверка по команде `/checkportal`.
   * Безопасное хранение учётных данных с шифрованием Fernet (`AES-128-CBC`).

## 🛠 Технологический стек

* **Python 3.10+**
* **Telethon** (Telegram Client API)
* **FastAPI + Uvicorn** (OAuth PKCE & WebApp backend)
* **google-genai** (Google Gemini API)
* **Playwright** (Headless browser automation)
* **SQLAlchemy** (SQLite storage)
* **Cryptography** (Fernet encryption)

## 📦 Установка и запуск

1. Клонируйте репозиторий:
   ```bash
   git clone https://github.com/altairmurat/telegram_christina.git
   cd telegram_christina
   ```

2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Настройте конфигурацию:
   Скопируйте `.env.example` в `.env` и укажите ваши ключи:
   ```bash
   cp .env.example .env
   ```

4. Запустите приложение:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   или через Docker:
   ```bash
   docker build -t telegram_christina .
   docker run -p 8000:8000 --env-file .env telegram_christina
   ```
