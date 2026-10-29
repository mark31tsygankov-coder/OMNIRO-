# OMNIRO VPN bot

Файлы: main.py, requirements.txt, render.yaml, .env.example

## Render
1. Залей папку в GitHub-репозиторий (НЕ клади туда .env).
2. Render → New → Blueprint (подхватит render.yaml) или New Web Service: Build `pip install -r requirements.txt`, Start `python main.py`.
3. В Environment задай: BOT_TOKEN, ADMIN_IDS, ROLLYPAY_API_KEY, ROLLYPAY_SIGNING_SECRET, H1CLOUD_API_TOKEN, SUPPORT_USERNAME.
4. Для базы нужен Disk (mount /data) — иначе при каждом деплое база стирается. Бесплатный план засыпает — бери Starter.
5. После старта бот сам ставит Telegram-webhook и callback_url в RollyPay:
   https://ТВОЙ-СЕРВИС.onrender.com/rollypay/webhook
   (если автоустановка не прошла — впиши этот URL в настройках кассы вручную).
6. Открой бота → /admin → «Диагностика H1Cloud» и проверь ответ панели.
