# Aviasales Price Agent

Мини-агент для личного мониторинга цен через Aviasales Data API от Travelpayouts. Клиент использует публичный endpoint `https://api.travelpayouts.com/v2/prices/latest`.

Он не проверяет live-наличие билета. Агент сохраняет кэшированные цены Aviasales в SQLite и отвечает на практичный вопрос: стала ли цена по направлению заметно ниже, чем в прошлых наблюдениях?

## Быстрый старт

```powershell
Copy-Item routes.example.json routes.json
Copy-Item .env.example .env
# Откройте .env и впишите TRAVELPAYOUTS_TOKEN
python src/main.py run --config routes.json
```

Можно не создавать `.env`, а задать переменную только для текущей PowerShell-сессии:

```powershell
$env:TRAVELPAYOUTS_TOKEN = "ваш_token"
python src/main.py run --config routes.json
```

Проверка без API-токена:

```powershell
python src/main.py run --config routes.example.json --demo
python src/main.py history
```

## Telegram-уведомления

Создайте бота через `@BotFather`, получите bot token, затем напишите вашему боту любое сообщение. Узнайте `chat_id` любым удобным способом, например через `getUpdates` или отдельного Telegram-бота для показа user id.

Добавьте в `.env`:

```env
TELEGRAM_BOT_TOKEN=ваш_telegram_bot_token
TELEGRAM_CHAT_ID=ваш_chat_id
```

Чтобы сообщения видела группа, добавьте бота в группу и укажите `chat_id` группы. Telegram-бот не рассылает сообщения во все чаты автоматически: он пишет только туда, чей id указан в `.env`.

Для отправки сразу в несколько мест используйте:

```env
TELEGRAM_CHAT_IDS=личный_chat_id,групповой_chat_id
```

Групповой `chat_id` обычно отрицательный, часто вида `-100...`. Чтобы узнать его: добавьте бота в группу, напишите в группе сообщение или команду боту, затем откройте:

```text
https://api.telegram.org/botВАШ_ТОКЕН/getUpdates
```

В ответе найдите `message.chat.id` для группы.

Проверка Telegram:

```powershell
python src/main.py test-telegram
```

Запуск с уведомлением только при заметном снижении:

```powershell
python src/main.py run --config routes.json --notify
```

Антиспам включен по умолчанию: один и тот же alert по `route + date + return date + price + alert type` не отправляется повторно в течение 24 часов. Окно можно изменить:

```powershell
python src/main.py run --config routes.json --notify --notify-dedupe-hours 12
```

Для теста сообщения с текущим статусом, даже если цены не снизились:

```powershell
python src/main.py run --config routes.json --notify --notify-always
```

Файл `scripts/run_once.bat` использует этот режим, поэтому при запуске через Windows Task Scheduler Telegram будет получать статус по текущим фильтрам каждые 30 минут.

## Настройка маршрутов

Пример `routes.json`:

```json
{
  "currency": "rub",
  "market": "ru",
  "threshold_percent": 12,
  "absolute_drop": 3000,
  "routes": [
    {
      "origin": "MOW",
      "destination": "IST",
      "departure_at": "2026-08",
      "return_at": "2026-08",
      "one_way": false,
      "direct": true,
      "destination_airports": ["IST"],
      "departure_date_from": "2026-08-06",
      "departure_date_to": "2026-08-06",
      "return_date_from": "2026-08-06",
      "return_date_to": "2026-08-06",
      "min_trip_days": 0,
      "outbound_departure_latest": "12:00",
      "return_departure_earliest": "16:00",
      "require_time_filters": false,
      "price_sources": ["latest", "month_matrix", "direct", "calendar"],
      "monitor_origins": ["MOW", "VKO", "SVO", "DME"],
      "monitor_dates": ["2026-08-05", "2026-08-06", "2026-08-07"],
      "smart_verify": true,
      "duffel_max_offers": 1,
      "duffel_supplier_timeout_ms": 5000,
      "duffel_destinations": ["IST"],
      "cabin_class": "economy",
      "limit": 30,
      "pages": 5
    }
  ]
}
```

Поля:

- `origin`, `destination`: IATA-коды города или аэропорта.
- `departure_at`: месяц или дата вылета, например `2026-09` или `2026-09-12`. Для Data API это используется как начало периода поиска.
- `return_at`: дата или месяц обратного вылета для round trip. Фильтр применяется локально по данным, которые вернул API.
- `origin_airports`, `destination_airports`: локальный allow-list аэропортов из ответа API. Для Istanbul Airport используйте `"destination_airports": ["IST"]`.
- `monitor_origins`: разворачивает один маршрут в несколько проверок по городам/аэропортам. Текущий рабочий режим: `["MOW", "VKO", "SVO", "DME"]`, где `MOW` остается широким радаром, а аэропорты проверяются строго.
- `monitor_dates`: разворачивает один маршрут в несколько точных дат. Текущий рабочий режим: `05.08`, `06.08`, `07.08.2026`.
- `departure_date_from`, `departure_date_to`, `return_date_from`, `return_date_to`: точные даты или диапазоны дат, если нужен не весь месяц.
- `max_price`: необязательная целевая цена для alert-а. Если поле не задано, агент просто сообщает текущие цены без ценового порога.
- `min_trip_days`, `max_trip_days`: минимальная и максимальная длительность поездки. `"min_trip_days": 1` отбрасывает same-day варианты.
- `limit`, `pages`: сколько кэшированных вариантов забирать до локальной фильтрации. Больше страниц полезно, когда первые дешевые варианты не проходят фильтры.
- `outbound_departure_latest`: вылет туда не позже этого времени, например `"12:00"`.
- `return_departure_earliest`: обратный вылет не раньше этого времени, например `"16:00"`.
- `require_time_filters`: если `true`, предложения без времени вылета отбрасываются. Для `v2/prices/latest` обычно нужно оставить `false`, потому что Data API возвращает даты, но не часы.
- `smart_verify`: включает дополнительную проверку Duffel, если задан `DUFFEL_ACCESS_TOKEN`.
- `threshold_percent`: процент снижения против лучшей цены прошлого запуска.
- `absolute_drop`: абсолютное снижение в валюте, например `3000` рублей.

## Как читать результат

Пример:

```text
[ALERT] MOW->IST: best 18420 RUB (2026-09, previous run best 21990 RUB; down 16.2% vs previous run), https://www.aviasales.ru/search/...
```

`ALERT` означает, что сработал хотя бы один критерий:

- цена ниже лучшей цены прошлого запуска на `threshold_percent` или больше;
- цена ниже лучшей цены прошлого запуска на `absolute_drop` или больше;
- офферы по маршруту исчезли после прошлой успешной проверки;
- офферы по маршруту снова появились после проверки без цен.

## Автоматический запуск

Для охоты за хорошей ценой используйте интервал около 30 минут. Это не превращает Data API в live-поиск: данные все равно кэшированные, но агент будет чаще ловить появление новых низких значений в кэше.

Вариант 1: постоянный процесс в открытой консоли:

```powershell
python src/main.py watch --config routes.json --every-minutes 30
```

С Telegram:

```powershell
python src/main.py watch --config routes.json --every-minutes 30 --notify
```

Вариант 2: Windows Task Scheduler. Это надежнее для фонового мониторинга, особенно после перезагрузки:

```powershell
.\scripts\register_windows_task.ps1 -IntervalMinutes 30
```

Если PowerShell запрещает `.ps1`, можно создать задачу через `schtasks.exe`:

```powershell
schtasks /Create /SC MINUTE /MO 30 /TN "Aviasales Price Agent" /TR "%CD%\scripts\run_once.bat" /F
```

Задача будет запускать из папки проекта:

```powershell
python src/main.py run --config routes.json --db data/prices.sqlite --env .env
```

Логи планировщика пишутся в `logs/scheduled.log`.

Код возврата `1` означает, что найдено заметное снижение. Это удобно для будущей интеграции с Telegram, почтой или системными уведомлениями.

## Smart mode: Aviasales + Duffel

`smart_verify: true` в `routes.json` включает второй слой проверки через Duffel Offer Requests. Агент сначала собирает кэшированные цены Travelpayouts/Aviasales, затем, если в `.env` есть Duffel-токен, добавляет live-offers с точными временами вылета.

Добавьте в `.env`:

```env
DUFFEL_ACCESS_TOKEN=ваш_duffel_access_token
```

Текущий маршрут уже настроен на строгий direct-only поиск и временные фильтры:

```json
"smart_verify": true,
"duffel_max_offers": 1,
"duffel_supplier_timeout_ms": 5000,
"duffel_destinations": ["IST"],
"cabin_class": "economy"
```

Duffel не заменяет Aviasales полностью: покрытие поставщиков и авиакомпаний отличается, а live-offers могут иметь короткий срок жизни. Поэтому текущая логика такая: Aviasales работает как широкий ценовой радар, Duffel помогает проверить варианты, где доступны точные сегменты и время рейсов.

Проверить Duffel отдельно:

```powershell
python src/main.py check-duffel --config routes.json --env .env --max-offers 1
```

## Дайджест

Показать сводку лучших цен по активному `routes.json` за последние 12 часов:

```powershell
python src/main.py digest --config routes.json --db data/prices.sqlite --env .env --hours 12
```

Отправить такую сводку в Telegram:

```powershell
python src/main.py digest --config routes.json --db data/prices.sqlite --env .env --hours 12 --notify
```

Для утреннего и вечернего дайджеста можно повесить на Windows Task Scheduler файл:

```powershell
scripts\run_digest.bat
```

## Контроль агента

Команда `status` показывает, жив ли мониторинг по данным SQLite: сколько маршрутов уже проверялось, сколько маршрутов сейчас с ценами, когда была последняя проверка и какая лучшая текущая цена.

```powershell
python src/main.py status --config routes.json --db data/prices.sqlite --env .env
```

Отправить контрольное сообщение в Telegram:

```powershell
python src/main.py status --config routes.json --db data/prices.sqlite --env .env --notify
```

Для отдельного утреннего/вечернего health-check в Windows Task Scheduler можно использовать:

```powershell
scripts\run_status.bat
```

По умолчанию статус считается устаревшим, если последняя сохраненная проверка старше 90 минут. Порог можно изменить:

```powershell
python src/main.py status --stale-minutes 120
```

## Live buy-pager

Для покупки по минимальной цене в моменте используйте отдельный live-режим. Он не читает Aviasales Data API, а открывает конкретную страницу поиска в браузере через Playwright и берет минимальную видимую цену из блока прямых рейсов.

Это более хрупкий режим, потому что он зависит от текущей верстки сайта, но он ближе к реальной цене покупки, чем кэшовый API. Для Aviasales обычно надежнее `headless: false`, то есть проверка в обычном видимом окне браузера.

Live-режим использует постоянный профиль браузера из папки `browser-profile/`. Cookies и сессия сохраняются между запусками, поэтому если сайт попросит доказать, что вы не робот, пройдите проверку руками в открывшемся окне. Поле `manual_check_seconds` задает, сколько секунд агент будет ждать после обнаружения такой проверки.

Установка Playwright:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install playwright
$env:PLAYWRIGHT_BROWSERS_PATH = "browsers\ms-playwright"
.\.venv\Scripts\python.exe -m playwright install chromium
```

Настройка:

```powershell
Copy-Item live_searches.example.json live_searches.json
notepad live_searches.json
```

В `live_searches.json` лучше вставить URL, который вы получили после ручной настройки фильтров на Aviasales: даты, прямые рейсы, нужные времена вылета и возврата. Поле `max_price` задает цену покупки/срочного уведомления. Если точный фильтр не сохраняется в URL, live-режим все равно откроет страницу и даст кнопку для быстрой ручной проверки.

Важные поля:

```json
"headless": false,
"profile_dir": "browser-profile",
"manual_check_seconds": 120
```

Разовая live-проверка:

```powershell
python src/main.py live-check --live-config live_searches.json --db data/prices.sqlite --env .env --notify
```

Если Playwright установлен в `.venv`:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "browsers\ms-playwright"
.\.venv\Scripts\python.exe src/main.py live-check --live-config live_searches.json --db data/prices.sqlite --env .env --notify
```

Запуск в цикле:

```powershell
python src/main.py live-watch --live-config live_searches.json --every-minutes 30 --notify
```

Для Windows Task Scheduler можно использовать:

```powershell
scripts\run_live_once.bat
```

Amadeus Self-Service закрыт с 17 июля 2026 года, поэтому Amadeus Enterprise не используется как основной путь для личного агента.
