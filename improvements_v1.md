# Задача: довести recsys до прохождения грейдера

## Цель

Реализовать минимально достаточные улучшения baseline, чтобы претендовать на **максимальные баллы** грейдера:

| Метрика | Цель |
|---------|------|
| precision | до 0.15 |
| ndcg | до 0.07 |
| diversity | до 0.4 |
| coverage | до 0.8 |
| latency (бонус) | до 80 ms при ≥30 баллов по остальным |

Грейдер использует `TOP_K = 10`. Работает только с API на `:5000` и `:5001`. `webapp` для сдачи не нужен.

## Как работает грейдер

- Нет аутентификации — пользователь = строка `user_id`
- Много разных `user_id` (до 50 параллельно)
- У каждого пользователя несколько циклов: `recs` → пауза → `interact` (like/dislike) → снова `recs`
- Перед прогоном: `GET /cleanup`, новые id
- Каталог: `POST /add_items` с `item_ids` и `genres`
- Уже показанные айтемы нужно не рекомендовать повторно
- Один и тот же объект в новой сессии не считается релевантным — нужно исследовать интересы, а не залипать на одном топе

## Текущее состояние (на VM)

### Уже сделано

- [x] E2E поток: `webapp/interact` → `collector` → RabbitMQ → `pipeline` → `data/interactions.csv` → `top_items` → `/recs`
- [x] `regular_pipeline`: `normalize_interactions()`, периодический flush, стабильный consumer
- [x] `recommendations`: Redis key/value вместо RedisJSON, защита от пустого каталога
- [x] `webapp`: локальные URL, прокси `/interact`, реальный `user_id`

### Ещё не сделано

- [ ] Персонализация per `user_id`
- [ ] `genres` в `add_items` и в scoring
- [ ] `WatchedFilter` / shown-items в выдаче
- [ ] dislikes в ранжировании
- [ ] Ровно 10 айтемов в ответе
- [ ] Полный `cleanup` (архивация CSV, сброс shown, чистый старт)
- [ ] Shared catalog в Redis (pipeline должен видеть жанры)
- [ ] Deploy / автозапуск на VM

## План работ (по приоритету)

### Шаг 1 — Стабильный pipeline

**Файлы:** `regular_pipeline/main.py`, `data/`

- [x] Pipeline живёт постоянно, consumer RabbitMQ = 1
- [x] События пишутся в `data/interactions.csv`
- [x] `normalize_interactions()` для единых типов (`item_id` как str)
- [ ] Интервал flush и пересчёта: **2–5 сек** (сейчас 10) — чтобы успевать за грейдером
- [ ] Убрать зависимость от глобального `top_items` как финальной выдачи (переход к per-user ключам на шаге 3)

### Шаг 1.5 — Полный cleanup + архивация interactions

**Файлы:** `recommendations/main.py` (+ опционально `utils/cleanup.py`)

Грейдер вызывает только `GET /cleanup` на `:5001`. Всё сброс состояния — там.

**Поведение при cleanup:**

1. `redis.flushdb()` — очистка `top_items`, `user_recs:*`, `user_candidates:*`, `catalog:*`, `shown:*`
2. Сброс in-memory `unique_item_ids` в `recommendations`
3. **Архивация** `data/interactions.csv` (не удалять):
   - если файл существует и не пустой → переименовать в  
     `data/interactions_YYYYMMDD_HHMMSS.csv`
   - пример: `interactions_20260709_093045.csv`
   - новый прогон начинается с чистого `data/interactions.csv` (файл создаётся pipeline при первом событии)
4. Опционально: purge очереди `user_interactions` в RabbitMQ (если остались старые сообщения)

**Зачем архивация:** после прогона грейдера можно анализировать реальные like/dislike паттерны и понимать, куда крутить алгоритм (жанры, cold start, coverage).

**Проверка cleanup:**

```bash
curl -s http://127.0.0.1:5001/cleanup
ls -l data/interactions*.csv   # старый архив + нет нового interactions.csv (или пустой после первого flush)
redis-cli DBSIZE               # 0
```

### Шаг 2 — Контракт API + shared catalog

**Файлы:** `models.py`, `recommendations/main.py`

- [ ] Константа `TOP_K = 10` в одном месте (`models.py` или `config.py`)
- [ ] `NewItemsEvent`: добавить `genres: List[List[str]]` (параллельно `item_ids`)
- [ ] `add_items`: сохранять каталог в Redis (**блокер для pipeline**):
  - `catalog:item:{item_id}` → JSON со списком жанров
  - `catalog:all` → Redis set всех `item_id`
- [ ] Не полагаться только на in-memory `unique_item_ids` — pipeline не видит память процесса `recs`
- [ ] `/recs/{user_id}`: возвращать **ровно 10** айтемов (см. контракт выдачи ниже)

### Шаг 2.5 — Контракт выдачи: пул кандидатов + shown

**Проблема:** если в кэше ровно 10 id и часть уже показана, фильтр shown вернёт < 10.

**Решение:**

| Redis-ключ | Содержимое |
|------------|------------|
| `user_candidates:{user_id}` | пул 30–50 id (ранжированные кандидаты) |
| `shown:{user_id}` | Redis set уже показанных id |

**Логика `/recs/{user_id}`:**

1. Прочитать `user_candidates:{user_id}` (или cold-start fallback, если ключа нет)
2. Отфильтровать `shown:{user_id}` и disliked items (если храним отдельно)
3. Взять первые **10** из оставшихся
4. Добавить их в `shown:{user_id}`
5. Если после фильтра < 10 — добрать из global fallback (популярное / exploration по жанрам)

**Логика pipeline:** пишет `user_candidates:{user_id}`, не финальные 10.

### Шаг 3 — Персонализация (precision / ndcg)

**Файлы:** `regular_pipeline/main.py` (+ опционально `scoring.py`)

- [ ] Профиль пользователя из likes/dislikes по жанрам (читать каталог из Redis)
- [ ] Score айтема: similarity жанров + небольшой popularity prior из interactions
- [ ] Исключать: disliked items (последнее действие dislike по паре user+item)
- [ ] Cold start (нет истории): популярное, размазанное по жанрам
- [ ] Пересчитывать кандидатов для пользователей из CSV (`unique user_id`) + пользователей с недавними events
- [ ] Писать в Redis: `user_candidates:{user_id}`

### Шаг 4 — Diversity / coverage

- [ ] MMR (или жанровое разнообразие) при сборке пула кандидатов
- [ ] Небольшой exploration (5–10%), не ломая релевантность
- [ ] Не залипать на одном жанре / топ-3 айтемах — важно для coverage и diversity метрик

### Шаг 5 — Latency

**Файлы:** `recommendations/main.py`

- [ ] `GET /recs/{user_id}` = чтение из Redis + лёгкий фильтр shown + mark shown
- [ ] Без тяжёлых вычислений (polars, scoring, MMR) в HTTP-ручке — всё в pipeline
- [ ] Цель: < 100 ms на запрос

### Шаг 6 — WatchedFilter / shown-items

**Файлы:** `watched_filter.py`, `recommendations/main.py`

- [ ] Переписать на Redis set per user: `shown:{user_id}`
- [ ] Помечать shown **при выдаче в `/recs`**, не в `event_collector`
- [ ] `add(user_id, item_id)` / `get_shown(user_id)` / `clear_all()` в cleanup
- [ ] Не использовать `delete('*')` — в Redis это не работает как glob
- [ ] `recommendations/interact` — не используется грейдером; можно не трогать или починить отдельно

### Шаг 7 — Deploy на VM

**Файлы:** `scripts/` или systemd unit-файлы (опционально)

- [ ] После `git pull` — перезапуск T1/T2/T3
- [ ] Pipeline обязан быть жив во время прогона грейдера (часы)
- [ ] Варианты: tmux-сессии (сейчас) или systemd/supervisor для автоподъёма после ребута
- [ ] Порты 5000, 5001 открыты наружу

## Файлы, которые трогать

| Файл | Зачем |
|------|-------|
| `models.py` | `genres`, `TOP_K = 10` |
| `recommendations/main.py` | catalog в Redis, cleanup+архивация, per-user recs, shown |
| `regular_pipeline/main.py` | scoring, MMR, `user_candidates`, интервал 2–5 сек |
| `watched_filter.py` | shown per user (Redis set) |
| `event_collector/main.py` | обычно без изменений |

Не обязательно для грейдера: `webapp/`, картинки, Qdrant.

Новые зависимости для v1 **не нужны** — хватает `redis`, `polars`, `numpy`.

## Как проверять локально (на VM)

Работа в Cursor SSH, проект: `/home/ubuntu/recsys_proj`.

### Терминалы (пользователь держит, чат не прячет в background)

```bash
# T1 — recs
cd /home/ubuntu/recsys_proj && source .venv/bin/activate
uvicorn recommendations.main:app --host 0.0.0.0 --port 5001

# T2 — collector
uvicorn event_collector.main:app --host 0.0.0.1 --port 5000

# T3 — pipeline (foreground, смотреть traceback)
python -u regular_pipeline/main.py

# T4 — checks (ручные curl/redis/rabbitmq)
```

Инфра: `redis-server` и `rabbitmq-server` должны быть active.

### Smoke test (T4)

```bash
cd /home/ubuntu/recsys_proj

# 1. Health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/healthcheck
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/healthcheck

# 2. Cleanup (архивирует старый CSV)
curl -s http://127.0.0.1:5001/cleanup
ls -l data/interactions*.csv

# 3. Add items
curl -s -X POST http://127.0.0.1:5001/add_items \
  -H "Content-Type: application/json" \
  -d '{"item_ids":["1","2","3"],"genres":[["Action","Sci-Fi"],["Comedy"],["Drama","Romance"]]}'

# 4. Recs для нового пользователя
curl -s http://127.0.0.1:5001/recs/test-user-1 | python3 -m json.tool

# 5. Like
curl -s -X POST http://127.0.0.1:5000/interact \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test-user-1","item_ids":["1"],"actions":["like"]}'

# 6. Подождать pipeline (2-15 сек)
sleep 8

# 7. Проверить цепочку
sudo rabbitmqctl list_queues name messages consumers
tail -n 10 data/interactions.csv
redis-cli GET user_candidates:test-user-1
curl -s http://127.0.0.1:5001/recs/test-user-1 | python3 -m json.tool
```

### Критерии «готово» для smoke test

- [ ] Очередь RabbitMQ: `messages=0`, `consumers=1`
- [ ] В `interactions.csv` появилась строка с `test-user-1`
- [ ] В Redis есть `user_candidates:test-user-1`
- [ ] Повторный `recs` возвращает **10** id
- [ ] Повторный `recs` **не содержит** уже показанные (если есть запас в каталоге)
- [ ] После like выдача смещается в сторону похожих жанров (хотя бы эвристически)
- [ ] После `cleanup` старый CSV лежит в `data/interactions_*.csv`, новый прогон с чистого листа

### Анализ после прогона грейдера

```bash
ls -lt data/interactions_*.csv | head
# смотреть паттерны: какие жанры лайкают, где cold start, где залипание
```

### Опционально — UI

```bash
cd webapp && ../.venv/bin/python app.py
# http://<VM_IP>:8000/
```

Только для ручного тыка; грейдер UI не использует.

## Зона ответственности

**Чат:**
- читает `ssh_hadnoff.md`, этот файл, `task_desc.md`
- правит код
- предлагает команды проверки
- анализирует traceback / CSV / Redis / ответы API

**Чат не делает без необходимости:**
- не управляет всеми сервисами в скрытом background
- не полагается на Windows/PowerShell — работа на VM по SSH

**Пользователь:**
- запускает/перезапускает терминалы T1–T4
- присылает вывод при ошибках
- тыкает webapp в браузере при необходимости

## Порядок сдачи

1. Локально на VM пройти smoke test
2. Работа в ветке `feat/baseline-improvements`, merge в `master` когда стабильно
3. `git commit` + `git push`
4. На VM `git pull`, перезапуск сервисов
5. Submission IP + порты 5000/5001
6. После прогона — архив `interactions_*.csv` + логи грейдера → следующая итерация

## Definition of Done

- [ ] Все 3 сервиса стабильно работают на VM
- [ ] E2E: interact → RabbitMQ → pipeline → CSV → Redis → recs
- [ ] `cleanup` архивирует CSV и полностью сбрасывает runtime-состояние
- [ ] Персонализация по `user_id`, жанры, dislikes, shown filter
- [ ] Ответ `/recs` = 10 айтемов, быстрый (<100 ms чтение из Redis)
- [ ] Smoke test проходит без ручных костылей
