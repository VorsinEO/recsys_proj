# SSH Handoff For New Cursor Chat

## Контекст

Это финальный проект по курсу `recsys`.

Задание: сервис рекомендаций фильмов/сериалов с:
- `recommendations` сервисом на `:5001`
- `event_collector` сервисом на `:5000`
- `regular_pipeline` для обработки фидбека и обновления рекомендаций

Основное описание задачи лежит в `task_desc.md`.

Дополнительные заметки:
- `comments/start_point.md` — обзор baseline и архитектуры
- `comments/plan.md` — план работ
- `comments/progress.md` — журнал прогресса

## Репозиторий и окружение

Локальный репозиторий:
- `E:\DS\hardml_rs\RS\veo_proj_rs`

GitHub:
- `https://github.com/VorsinEO/recsys_proj`

Удалённая VM:
- host: `213.219.214.233`
- user: `ubuntu`
- project dir: `/home/ubuntu/recsys_proj`

SSH-ключ на текущей машине:
- `C:\Users\vorsi\.ssh\karpov_id_rsa`

На VM уже установлено:
- `python3`
- `python3-venv`
- `redis-server`
- `rabbitmq-server`
- `tmux`

Redis и RabbitMQ на VM уже ставились и запускались.

## Рекомендуемый режим работы

Работать лучше **прямо в Cursor SSH на VM** и разделить ответственность так:

- **чат**:
  - читает код
  - ищет причины ошибок
  - правит файлы
  - предлагает точечные команды проверки
  - анализирует логи, CSV, ответы API и состояние пайплайна

- **пользователь**:
  - сам запускает и перезапускает долгоживущие процессы в терминалах
  - сам держит открытыми логи сервисов
  - сам тыкает `webapp` в браузере
  - по просьбе чата присылает вывод терминалов или наблюдения

Идея: **чат не управляет постоянно живущими процессами автоматически**, а занимается кодом и диагностикой. Это делает отладку прозрачнее.

## Какие терминалы открыть в Cursor SSH

Рекомендуется открыть **4-5 терминалов**.

### Терминал 1 — `recs`

```bash
cd /home/ubuntu/recsys_proj
source .venv/bin/activate
uvicorn recommendations.main:app --host 0.0.0.0 --port 5001
```

### Терминал 2 — `collector`

```bash
cd /home/ubuntu/recsys_proj
source .venv/bin/activate
uvicorn event_collector.main:app --host 0.0.0.0 --port 5000
```

### Терминал 3 — `pipeline`

Запускать отдельно, чтобы traceback был сразу виден:

```bash
cd /home/ubuntu/recsys_proj
source .venv/bin/activate
python regular_pipeline/main.py
```

Если он падает, **не прятать в background**, а чинить по traceback прямо в этом терминале.

### Терминал 4 — `checks`

Для быстрых ручных проверок:

```bash
cd /home/ubuntu/recsys_proj
curl http://127.0.0.1:5000/healthcheck
curl http://127.0.0.1:5001/healthcheck
sudo rabbitmqctl list_queues name messages consumers
redis-cli ping
ls -l data
tail -n 20 data/interactions.csv
```

### Терминал 5 — `webapp` (опционально)

Нужен только если тестируется UI:

```bash
cd /home/ubuntu/recsys_proj/webapp
../.venv/bin/python app.py
```

## Как именно должен работать новый чат

Новый чат должен:

1. Прочитать этот файл и `comments/progress.md`
2. Проверить состояние кода и понять, что уже было исправлено
3. Не запускать самовольно долгоживущие сервисы в фоне без явной необходимости
4. Давать пользователю команды:
   - какой терминал перезапустить
   - какой вывод прислать
   - что проверить в браузере
5. Основной фокус держать на:
   - стабилизации `regular_pipeline`
   - подтверждении end-to-end потока событий
   - последующем улучшении рекомендательной логики

## Что не должен делать новый чат без необходимости

- Не брать на себя постоянное управление всеми сервисами
- Не плодить фоновые процессы без явной пользы
- Не скрывать traceback `pipeline`, если тот падает
- Не смешивать редактирование кода и неявный деплой без пояснения пользователю

## Что уже сделано

### 1. Базовая инфраструктура

- Создан `.gitignore`
- Создан `.env` локально
- Проект запушен в GitHub
- Репозиторий склонирован на VM в `/home/ubuntu/recsys_proj`
- На VM создано `.venv`
- Установлены зависимости из `requirements.txt`

### 2. Webapp

`webapp` поднимался и открывался в браузере по:
- `http://213.219.214.233:8000/`

Были загружены картинки на VM.

В `webapp` уже внесены правки:
- `webapp/app.py`
- `webapp/templates/index.html`

Что изменено:
- убран хардкод старого IP
- `recommendations` читаются локально с `127.0.0.1:5001`
- лайки/дизлайки идут через Flask-роут `/interact`, а не напрямую из браузера в `127.0.0.1:5000`
- в шаблон передаётся реальный `user_id`
- Flask слушает `0.0.0.0:8000`

### 3. Recommendations service

В `recommendations/main.py` уже внесён фикс:
- baseline использовал RedisJSON (`JSON.GET/SET`), а на VM обычный `redis-server`
- хранение `top_items` переведено на обычный Redis key/value с `json.dumps/json.loads`
- `cleanup()` переведён на `flushdb()`
- добавлена защита от пустого `unique_item_ids`

### 4. Pipeline

В `regular_pipeline/main.py` уже внесены частичные фиксы:
- путь к `data/interactions.csv` переведён на абсолютный через `Path(__file__)`
- добавлен `normalize_interactions()` с приведением типов:
  - `user_id -> Utf8`
  - `item_id -> Utf8`
  - `action -> Utf8`
  - `timestamp -> Float64`

Также был создан каталог:
- `/home/ubuntu/recsys_proj/data`

## Что уже проверено

### Работает

1. `webapp` открывается
2. карточки и кнопки отображаются
3. `event_collector` принимает события
4. `webapp -> /interact -> event_collector` работает
5. RabbitMQ получает сообщения

Подтверждение было такое:
- в логах `webapp` были `POST /interact 200`
- в логах `event_collector` были `POST /interact HTTP/1.1 200 OK`

### Работает нестабильно / не доведено

`regular_pipeline` пока не доведён до стабильного фонового запуска на VM.

Из-за этого:
- очередь RabbitMQ иногда накапливается
- `consumer` у очереди периодически становится `0`
- новые события не всегда попадают в `data/interactions.csv`
- рекомендации после лайков пока не обновляются стабильно

## Последняя точно найденная ошибка

В одном из запусков `regular_pipeline/main.py` была зафиксирована ошибка:

`polars.exceptions.ShapeError: unable to vstack, dtypes for column "item_id" don't match: i64 and str`

Эту ошибку уже попытались исправить через `normalize_interactions()`.

Но проблема полностью ещё не закрыта: pipeline всё ещё не был стабильно подтверждён как долгоживущий процесс на VM.

## На чём остановились

Нужно продолжить уже **прямо в Cursor SSH на VM**, чтобы убрать прослойку Windows/PowerShell.

Главная ближайшая цель:

1. Подключиться к VM по SSH в Cursor
2. Открыть `/home/ubuntu/recsys_proj`
3. Проверить текущее состояние процессов:
   - `recommendations`
   - `event_collector`
   - `regular_pipeline`
4. Добить стабильный запуск `regular_pipeline`
5. Подтвердить end-to-end цепочку:
   - `webapp click`
   - `event_collector`
   - `RabbitMQ`
   - `regular_pipeline`
   - `data/interactions.csv`
   - `top_items` в Redis
   - изменение `/recs/{user_id}`

## Что стоит проверить первым делом в новом SSH-чате

Выполнить на VM:

```bash
cd /home/ubuntu/recsys_proj
git status
ls
ls data
```

Проверить процессы:

```bash
ps -ef | grep -E "uvicorn|regular_pipeline" | grep -v grep
tmux ls
```

Проверить сервисы:

```bash
curl http://127.0.0.1:5000/healthcheck
curl http://127.0.0.1:5001/healthcheck
sudo rabbitmqctl list_queues name messages consumers
redis-cli ping
```

Проверить данные:

```bash
ls -l data
tail -n 20 data/interactions.csv
```

## Наиболее вероятная следующая работа

Если `regular_pipeline` снова падает:

1. запускать его в foreground прямо на VM:
```bash
cd /home/ubuntu/recsys_proj
.venv/bin/python -u regular_pipeline/main.py
```

2. снять traceback
3. починить код
4. повторить контрольный тест:
   - получить `/recs/test-user`
   - отправить 2-3 лайка через `POST /interact`
   - подождать 10-15 секунд
   - проверить `data/interactions.csv`
   - проверить изменился ли `top_items`
   - проверить изменилась ли выдача `/recs/test-user`

## Важное наблюдение по baseline

Даже когда pipeline работает, baseline логика очень слабая:
- персонализации почти нет
- `recommendations` в основном опирается на `top_items`
- просмотренное не фильтруется нормально
- `genres` из задания по сути не используются

То есть после стабилизации baseline следующий шаг — уже не только чинить запуск, но и улучшать сам алгоритм.
