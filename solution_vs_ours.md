# Сравнение: наше решение vs solution платформы

Дата: 2026-07-09  
Наш пик: **~96%** (P/NDCG/diversity/latency max, coverage 0.66 → 80% шкалы)  
Официальный `solution/`: эталонный подход авторов курса (LightFM + top + random).

---

## 1. Архитектура в одном взгляде

```
ОФИЦИАЛЬНОЕ                          НАШЕ
─────────────                        ────
/add_items → in-memory set           /add_items → Redis catalog + genres + global pool
/interact  → только RabbitMQ         /interact  → online_update (UC сразу) + RabbitMQ
pipeline   → CSV каждые 10s          pipeline   → append CSV + co-like + light global refresh
           → LightFM каждые 30s                 (без per-user dirty recompute)
           → top_items каждые 5s
           → unseen_random каждые 5s
/recs      → blend Redis JSON        /recs      → UC? else hit-heavy cold + explore slots
             LightFM / top / random             + shown-filter
```

**Главный философский разрыв:**  
официальное — *batch collaborative filtering* (модель учится в pipeline, `/recs` только читает);  
наше — *online heuristics* (персонализация на like за ~25ms, pipeline вторичен).

---

## 2. Event collector (`/interact`)

| | Solution | Мы |
|--|----------|-----|
| Роль | Публикация в RabbitMQ | RabbitMQ **+** мгновенный `update_user_from_interact` |
| Redis на interact | Нет | Профиль жанров, `popular_likes`, `co_like:*`, `user_candidates` |
| Latency interact | Минимальная | ~25ms (приемлемо при mean `/recs` 29ms) |

**Вывод:** у авторов collector «тонкий»; у нас — место, где живёт персонализация. Это и дало скачок NDCG после отказа от dirty pipeline overwrite.

---

## 3. Recommendations service (`/recs`)

### 3.1 Официальный blending

Из `solution/recommendations/main.py`:

1. Если есть `lightfm_recommendations:{user}` → взять **8** айтемов LightFM, добить **unseen_random**.
2. Иначе если есть `top_items` → взять **5** топ-лайков, добить unseen_random.
3. Иначе → только unseen_random.
4. С вероятностью **ε=0.05** — **полный random** из `unique_item_ids` (coverage/diversity).
5. Если всё ещё `<10` — random fill из in-memory каталога.

Флаги: `ENABLE_LIGHTFM_RECS`, `ENABLE_TOP_RECS`, `ENABLE_UNSEEN_RANDOM_RECS`.

### 3.2 Наш blending

1. Есть `user_candidates:{user}` → top-10 по online-скору, **1 explore-слот** из полного каталога.
2. Иначе cold: ротация mega-hits в голове + second-tier mid + **3 explore** через `SRANDMEMBER catalog:all`.
3. Всегда фильтр `shown` / `disliked`.
4. Нет ε-random «перезаписать весь список».

### 3.3 Ключевые отличия `/recs`

| Аспект | Solution | Мы |
|--------|----------|-----|
| Модель на запросе | Нет (только Redis read) | Нет (кандидаты уже в Redis) |
| Cold start | `top_items` (5) + random | Hit-heavy popular + second-tier + explore |
| Exploration | ε=5% full random + unseen_random slots | Фиксированные explore-слоты из всего каталога |
| Shown-filter | Почти не используется в `/recs`* | Жёсткий `SADD shown:{user}` |
| Контент (genres) | **Не используются** | Ядро online-профиля |
| Метрики | Prometheus (`/metrics`, latency hist) | JSONL `grader_requests.jsonl` |
| `add_items` | Только `unique_item_ids` в RAM | Redis catalog + genre index + refresh global |

\*В solution `WatchedFilter` пишется в `/interact` на **recommendations** (`request.item_id` — похоже на баг/устаревший API), а в blending `/recs` shown не читается. Unseen считается в pipeline по CSV interactions.

---

## 4. Regular pipeline / обучение

### 4.1 Solution (`recs.py`) — три периодические задачи

| Задача | Период | Что пишет в Redis |
|--------|--------|-------------------|
| `calculate_top_items` | 5s | `top_items` — топ-10 по лайкам |
| `update_unseen_random_items` | 5s | `unseen_random_items:{user}` ×10 на **каждого** юзера |
| `calculate_lightfm_recommendations` | 30s | `lightfm_recommendations:{user}` |

**LightFM (ядро качества у авторов):**
- loss = **WARP**
- `no_components=38`
- `item_alpha = user_alpha ≈ 8.55e-5`
- **13 epochs** на полной user–item матрице (like=+1, dislike=−1)
- LabelEncoder user/item → sparse CSR → `model.predict` → top-K в Redis JSON

**Ingestion:** раз в **10 секунд** drain RabbitMQ → **полная перезапись** `interactions.csv` (concat + `write_csv`).

### 4.2 Наш pipeline

| Задача | Что делает |
|--------|------------|
| Flush | Append-only CSV + `record_co_likes` на multi-like |
| Recompute | Только `refresh_global_candidates` + периодический rebuild co-like графа |
| Per-user UC | **Не трогает** (раньше dirty recompute лагал и перетирал online) |

**Вывод:** авторы кладут качество в **тяжёлую CF-модель каждые 30s**; мы — в **лёгкий online item–item/genre** на каждый like. Оба валидны; наш путь быстрее реагирует на feedback грейдера (короткие паузы между `/recs`).

---

## 5. Данные и признаки

| | Solution | Мы |
|--|----------|-----|
| Genres из `/add_items` | Модель `NewItemsEvent` **без genres** | `genres: List[List[str]]` + Redis index |
| Сигнал | Только like/dislike матрица | Жанры + popular + co-like + soft-link |
| Каталог | In-memory `unique_item_ids` | Redis `catalog:all` + `catalog:item:*` |
| Redis | **redis-stack** + JSON API | Обычный Redis (string/set/zset) |

Официальное решение — чистый **collaborative filtering**.  
Наше — **content + popularity + item–item**, без матричной факторизации.

---

## 6. Coverage / Diversity / NDCG — как бьют метрики

### Solution (задумка авторов)

- **Coverage:** ε-random 5% + unseen_random добор + random fill → широкий охват каталога.
- **Diversity:** random/unseen размывают жанровые кластеры.
- **Precision/NDCG:** LightFM WARP учит ранжирование по всей истории; top_items страхует cold.
- **Latency:** `/recs` почти только Redis GET → легко уложиться в бонус.

### Мы (что сработало на ~96%)

- **Precision:** hit-heavy cold (mega-hits в rank 1–5) — главный драйвер ранних лайков.
- **NDCG:** после like — unseen head + co-like, UC без размытия explore; отказ от pipeline overwrite.
- **Diversity:** explore + second-tier + жанровый random fill (уже 100% баллов).
- **Coverage:** узкое место — explore из маленького global pool; фикс: `SRANDMEMBER` по всему каталогу + 3/1 слота.
- **Latency:** 29–34ms mean — полный бонус.

---

## 7. Инфра и ops

| | Solution | Мы |
|--|----------|-----|
| Деплой | `docker compose` (recs, collector, pipeline, redis-stack, rabbitmq, prometheus, grafana) | Процессы на VM + локальные Redis/RabbitMQ |
| Observability | Prometheus + Grafana dashboard | Request JSONL + ad-hoc `analyze_requests.py` |
| Cleanup | `DELETE *` / JSON delete (хрупко) | `FLUSHDB` + archive CSV/log |
| Зависимости | `lightfm`, `scipy`, `sklearn`, redis JSON | polars, redis, fastapi (без LightFM) |

У авторов сильнее **production-like** обвязка (метрики, compose). У нас сильнее **итерации под грейдер** (логи запросов, быстрые эвристики).

---

## 8. Замеченные шероховатости в solution (для понимания, не «ошибки курса»)

1. **`/interact` на recommendations** использует `request.item_id`, тогда как модель — `item_ids` (список). Похоже на мёртвый/устаревший код; основной interact — на collector :5000.
2. **`NewItemsEvent` без genres** — контент в эталоне сознательно не используется.
3. **Flush CSV полной перезаписью** каждые 10s — на большом логе дороже нашего append.
4. **LightFM каждые 30s на всех юзерах** — при росте interactions может стать bottleneck (у нас аналогичная боль была с dirty UC).
5. **`WatchedFilter` key = `{user}-{item}`** по одному ключу на пару — не set; в `/recs` не фильтрует выдачу.

---

## 9. Что у нас лучше / что у них сильнее

### Наше преимущество (под этот грейдер)

- Мгновенная реакция на like (секунды, не ждать 30s LightFM).
- Явный контроль rank 1–5 → NDCG/Precision выжаты в max.
- Shown-filter реально влияет на следующие `/recs`.
- Не зависим от тяжёлого fit на полном CSV под нагрузкой.

### Сильнее у solution

- Принципиально более «правильная» CF-модель (WARP LightFM) — лучше обобщает сложные вкусы при достаточной истории.
- Готовый exploration policy (ε-greedy + per-user unseen random) — coverage «из коробки».
- Observability и docker-compose как образец сдачи.
- Простой `/recs` (только чтение) — меньше риска убить latency.

---

## 10. Чему учит сравнение (практические takeaways)

1. **Два рабочих пути к высоким баллам:** batch CF (они) vs online heuristics (мы). Грейдер с короткими паузами между запросами благоволит online.
2. **Coverage почти всегда = exploration budget × размер пула.** У них — random по всему `unique_item_ids`; у нас долго был пул ~300 → потолок ~0.66.
3. **Pipeline не должен перетирать хороший online state** — наш главный урок вечера (~82%→96% после slim pipeline).
4. **Жанры в задании есть** — мы их использовали; эталон их игнорирует в пользу CF. Оба ок, разные ставки.
5. Если догонять 100% «по-ихнему» — логичный гибрид: **оставить online cold/UC**, добавить **ε-random или LightFM в фоне** только как coverage/quality booster, не блокируя `/recs`.

---

## 11. Краткая таблица «кто за какую метрику»

| Метрика | Solution рычаг | Наш рычаг |
|---------|----------------|-----------|
| Precision | LightFM + top_items | Hit-heavy cold + popular/co-like |
| NDCG | WARP ranking | Unseen UC head + score order, no UC explore blur |
| Diversity | Random / unseen | Explore slots + genre spread |
| Coverage | ε=5% + unseen_random + fill | Catalog `SRANDMEMBER` explore (push) |
| Latency | Thin `/recs` | Thin `/recs` + cheap online on interact |

---

## 12. Итог

Официальный solution — **классический industrial baseline**: события → CSV → периодический LightFM/top/unseen → Redis JSON → тонкий blending на `/recs` + ε-greedy.  

Наше решение — **online-first под грейдер**: like сразу строит кандидатов (жанры + co-like + popular), cold даёт хиты, explore добивает coverage, pipeline только копит статистику.  

На ~96% мы уже в зоне «максимум эвристик»; оставшийся gap coverage закрывается тем же приёмом, что у авторов заложен явно — **рандом/сэмпл по всему каталогу**, а не по узкому candidate pool. Дальнейший потолок качества «как у LightFM» имел бы смысл только если online перестанет хватать на сложных пользователях — на текущем грейдере online уже выжал P и NDCG в 100%.
