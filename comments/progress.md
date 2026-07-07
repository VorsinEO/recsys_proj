# Progress — журнал прогресса

## 2026-07-07 — Старт проекта

### Сделано
- [x] Изучено условие задания (`task_desc.md`)
- [x] Разобран код baseline: 3 сервиса + webapp
- [x] Создана директория `comments/` с документацией
- [x] Зафиксированы текущее состояние и пробелы baseline (`start_point.md`)

### Текущий статус
**Фаза 0 — подготовка.** Код baseline на месте, сервисы ещё не поднимали локально.

### Следующие шаги
1. **Поднять инфраструктуру локально** — Redis, RabbitMQ
2. **Запустить 3 компонента:**
   - `uvicorn recommendations.main:app --port 5001` (из корня с PYTHONPATH)
   - `uvicorn event_collector.main:app --port 5000`
   - `python regular_pipeline/main.py`
3. **Потыкать руками** — healthcheck, add_items, recs, interact
4. **Собрать первичные логи** — что пишется в Redis, CSV, как меняются рекомендации после лайков
5. **Починить webapp для локального теста** — URL на localhost, user_id из сессии/cookie
6. Зафиксировать наблюдения здесь

### Открытые вопросы
- Есть ли на машине Docker / как удобнее поднять Redis+RabbitMQ?
- Какой Python/venv используем?
- Нужен ли Qdrant сразу или начнём с Redis-only?

### Метрики грейдера
Пока нет — ждём первый submission.
