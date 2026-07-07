# Start Point — текущее состояние проекта

Дата: 2026-07-07

## Задание (кратко)

Реализовать сервис рекомендаций фильмов/сериалов с использованием контентных признаков и данных о лайках/дизлайках. Грейдер шлёт запросы на VM, оценивает по метрикам (TOP_K=10):

| Метрика | Мин | Макс | Баллы |
|---------|-----|------|-------|
| precision | 0.02 | 0.15 | 10 |
| ndcg | 0.02 | 0.07 | 10 |
| diversity | 0.1 | 0.4 | 10 |
| coverage | 0.1 | 0.8 | 10 |
| latency (бонус) | 800 ms | 80 ms | 10 (при ≥30 баллов) |

Полное описание: `task_desc.md`

## Архитектура (3 компонента)

```
Грейдер / Web UI
       │
       ├── :5000  Event Collector  ──► RabbitMQ ──► Regular Pipeline ──► Redis
       │                                              (top_items)
       └── :5001  Recommendations Service ◄──────────────────────────────┘
```

### 1. Recommendations Service (`recommendations/main.py`, порт 5001)

| Эндпоинт | Метод | Назначение |
|----------|-------|------------|
| `/healthcheck` | GET | Статус (нужен 200) |
| `/cleanup` | GET | Сброс кэша перед прогоном грейдера |
| `/add_items` | POST | Добавление объектов в каталог |
| `/recs/{user_id}` | GET | Рекомендации для пользователя |

**Текущая логика (baseline):**
- Хранит `unique_item_ids` в памяти процесса
- Читает `top_items` из Redis (JSON) — глобальный топ по лайкам
- С вероятностью `EPSILON=0.05` отдаёт случайные 20 айтемов (exploration)
- `WatchedFilter` подключён, но **не используется** в `/recs`
- Есть лишний `/interact` (дублирует event_collector, грейдер сюда не ходит)

### 2. Event Collector (`event_collector/main.py`, порт 5000)

| Эндпоинт | Метод | Назначение |
|----------|-------|------------|
| `/healthcheck` | GET | Статус |
| `/interact` | POST | Приём like/dislike, публикация в RabbitMQ |

**Текущая логика:**
- Пишет события в exchange `user.interact`, queue `user_interactions`
- Добавляет `timestamp` к событию
- `WatchedFilter` закомментирован

### 3. Regular Pipeline (`regular_pipeline/main.py`)

Фоновый asyncio-скрипт (не HTTP-сервис):
- **collect_messages** — читает RabbitMQ, батчит события каждые 10 сек, пишет в `data/interactions.csv`
- **calculate_top_recommendations** — каждые 10 сек считает топ-100 айтемов по лайкам, кладёт в Redis `top_items`

Нет персонализации, нет учёта dislikes, нет контентных признаков (жанров).

### Вспомогательные модули

| Файл | Назначение |
|------|------------|
| `models.py` | Pydantic-модели: `InteractEvent`, `RecommendationsResponse`, `NewItemsEvent` |
| `watched_filter.py` | Redis-set «пользователь видел айтем» (почти не задействован) |

### Web UI (`webapp/`) — для локального тестирования

- Flask-приложение на порту 8000
- Данные: `static/movies.csv`, `static/links.csv`, постеры в `static/images/`
- Сейчас захардкожен удалённый сервер `135.181.153.151:5000/5001`
- `getUserID()` в JS возвращает `'user_id'` — баг для локального теста

## Инфраструктурные зависимости

Для запуска нужны (не в репозитории, поднимаем отдельно):

- **Redis** — `localhost:6379` (рекомендации + pipeline + watched_filter)
- **RabbitMQ** — `amqp://guest:guest@localhost/` (event_collector ↔ pipeline)

## Зависимости Python

`requirements.txt`: fastapi, uvicorn, redis, aio-pika, polars, flask, numpy, ...

## Замеченные пробелы baseline (для будущих улучшений)

1. **Нет персонализации** — один глобальный `top_items` на всех
2. **Жанры не используются** — в `NewItemsEvent` нет поля `genres`, хотя в задании оно есть
3. **Возвращается 20 айтемов**, грейдер считает TOP_K=10
4. **WatchedFilter не фильтрует** показанные/просмотренные айтемы
5. **Dislikes игнорируются** при ранжировании
6. **Нет cold start** стратегии для новых пользователей/айтемов
7. **Нет docker-compose / скриптов запуска** — всё вручную
8. **Нет Qdrant** (упомянут в задании как рекомендация для векторов)

## Структура репозитория

```
veo_proj_rs/
├── comments/           ← наши заметки (этот каталог)
├── recommendations/    ← сервис рекомендаций (:5001)
├── event_collector/    ← сбор событий (:5000)
├── regular_pipeline/   ← обучение/обновление модели
├── webapp/             ← демо UI для ручного теста
├── models.py
├── watched_filter.py
├── requirements.txt
└── task_desc.md
```
