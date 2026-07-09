# Improvements v2 — состояние после 58%+ и план на вечер

Дата: 2026-07-09  
Цель грейдера (из v1): precision≥0.15, ndcg≥0.07, diversity≥0.4, coverage≥0.8, latency бонус ≤80ms.

## 1. Траектория скора

| Прогон | Score | P@10 | NDCG | Cov | Div | Latency |
|--------|------:|-----:|-----:|----:|----:|--------:|
| Ранние (pipeline/cold) | ~22% | 0.03 | ~0 | 0.08 | 0.81 | ok |
| Online + popular/co-like | 49.6% | 0.28 | 0.030 (19%) | 0.30 | 0.75 | 71ms |
| Forced head + soft co-like | 56.4% | 0.36 | 0.043 (47%) | 0.34 | 0.68 | 49ms |
| Explore/unseen (заявл.) | 58.4% | 0.415 | 0.049 (58%) | 0.338 | 0.650 | 40ms |
| UC explore=0 + 2nd-tier | ~82.4%* | 0.474 (100%) | 0.057 (73%) | 0.371 (39%) | 0.643 (100%) | 34ms (20/20) |
| **Slim pipeline (no dirty UC)** | **~96%**** | **0.932 (100%)** | **0.103 (100%)** | **0.657 (80%)** | **0.767 (100%)** | **29ms (20/20)** |

\*34.4+28+20 = 82.4. \*\*40+36+20 = 96. Остаток: coverage 0.66→0.80 (−4 балла).

Баллы на прогоне ~96%:
- `test_metrics`: **40 / 40**
- `test_extra_metrics`: **36 / 40** (coverage 80% шкалы, diversity max)
- `test_response_time`: **20 / 20** (mean 29ms)

### Coverage push (следующий прогон)
- Было: explore из long-tail global pool (~220) → unique 16073 / ~24460 = 0.66
- Нужно ~19.5k unique (+3.5k)
- Фикс: `SRANDMEMBER catalog:all` на explore; cold=3 слота, UC=1 слот (P/NDCG с огромным запасом)

### Latency note (прогон ~82%)
- Грейдер mean **/recs = 34ms** — бонус полный; «долго» ≠ медленный `/recs`.
- Wall-time `test_metrics` ~1080s — длительность сценария грейдера (~18 мин, ~52 /recs/s), не наша latency.
- Лог: `/recs` p50≈5ms, p95≈24ms, max≈161ms; к концу прогона p50 растёт до ~8ms (Redis/impressions).
- Cold path был тяжелее из‑за per-request HMGET в rare-explore (~27ms bench) — **убран**, explore = rotate long-tail.
- `/interact` online_update ~25ms p50 — ок; 1 worker uvicorn на collector.

### Pipeline bottleneck (главный «долго» в логе)
Лог `dirty_left=900→1058`, `40 users in 3–10s`:
- Flush добавлял всех interact-юзеров в `dirty_users`.
- Recompute читал **весь** CSV, строил профили/каталог и пересчитывал UC **без co-like** → перетирал online_update.
- При ~36 interact/s очередь dirty росла быстрее, чем 40/цикл.

**Фикс:** pipeline больше не делает per-user dirty recompute. Роли:
- `/interact` online_update → `user_candidates` (источник правды)
- pipeline → append CSV + co-like на multi-like flush + periodic `global_candidates` / full co-like rebuild

**Запас по «бесплатным» метрикам:** precision и diversity уже 100% баллов — ими можно жертвовать ради NDCG и coverage.

## 2. Что реально работает (не ломать)

### Инфра
- `/recs` p50 ~1–1.5ms, mean ~40–70ms — стабильно.
- Ingestion: `interact` log ≈ `interactions.csv`, очередь RabbitMQ в конце 0.
- Online update на collector `/interact`: 100% `updated`, lag interact→UC p50 **~0.5s**.
- Всегда 10 айтемов в ответе.

### Алгоритм
- **Hit-heavy cold start** (top popular в голове) — главный драйвер precision.
- **Online personalization** (профиль жанров + `user_candidates` сразу после like).
- **Co-like / soft co-like** + popular-in-genre.
- **Shown-filter** критичен: без него NDCG на UC проседает (хиты уже показаны в cold).

### Паттерн грейдера
- ~18k unique `/recs` users, ~3 recs/user типично.
- Частые паттерны: `RIRR`, `RRRI`, `RIRIR` — после like часто ещё 1–2 `/recs`.
- Feedback растёт от прогона к прогону (2k → 21k → 30k → 39k interact) — положительная петля popular/co-like.

## 3. Диагноз текущего потолка (~58%)

### NDCG (главный недобор в `test_metrics`)
На прогоне 58.4% (до explore/unseen-head):
- Cold: лайки сконцентрированы в rank 1–5 (хорошо для NDCG).
- UC: **top3_share только ~27%**, много лайков на позициях 6–10.
- Причина: cold уже показал mega-hits → они в `shown` → UC всё равно тащил их в голову / размазывал порядок → hit уезжал вниз списка.

На **свежем прогоне с новым кодом** (есть `explore_slots=2`, `head_unseen`, interact=39523; **официальный scoreboard в чат не приложен, цифры 58.4% совпадают с предыдущим**):
- `head_size=5` почти всегда, `head_unseen=5` почти всегда — unseen-head сработал.
- UC proxy: item-hit **0.066**, list-hit **0.39**, top3% **43.6%**, top5% **69%** (лучше, чем 27% top3 раньше).
- Cold proxy: top5% **92.6%** — голова cold всё ещё сильнее UC.
- Unique items **10175** (было ~8.3k) — explore-слоты расширяют coverage-кандидатность.
- Но top10 share выдачи всё ещё **~25%** — mega-hits доминируют.

### Coverage (~0.34, 34% баллов)
- **Не из-за короткого top-K** (всегда 10/10).
- Coverage = доля уникальных айтемов каталога (~24k) хоть раз попавших в выдачу.
- Hit-bias: одни и те же 5–10 фильмов получают ~18k показов каждый → precision↑, coverage потолок.
- Explore в хвосте top-10 помогает, но 2 слота × 55k recs при сильном overlap всё ещё мало для цели 0.8.

### Diversity (100% баллов при ~0.65)
- Ещё есть запас снизить diversity ради NDCG/coverage.
- Не цель оптимизировать вверх.

## 4. Архитектура сейчас (кратко)

```
add_items → Redis catalog + genre index + cheap global_candidates
/recs     → user_candidates? else rotated hit-heavy global; last 2 slots explore
/interact → online_update (profile, popular_likes, soft/co-like, unseen head=5)
         → RabbitMQ → pipeline flush CSV + dirty recompute (secondary)
```

Ключевые файлы:
- `online_update.py` — мгновенная персонализация
- `recommendations/service.py` — `/recs`, cold/explore
- `regular_pipeline/main.py` — CSV + co-like rebuild + dirty users
- `catalog_store.py` — genres + inverted index
- `scoring.py` — offline scoring / MMR≈off

## 5. План на вечер (приоритет)

### Сделано 2026-07-09 вечер (пробуем следующий прогон)
1. **UC explore_slots=0** — top-10 целиком по ранжированию (NDCG); explore только на cold (2 слота).
2. **Rare explore** — cold explore берёт low-impression / long-tail, не mega-hits.
3. **Second-tier mid** — cold mid ротирует popular ranks ~5–80; UC mid скипает top-8 mega-hits.
4. **Сильнее co-like** (`CO_LIKE_BOOST=18`, слабее pop в UC) — порядок UC ближе к item-CF.
5. Багфикс: `_pick_with_explore` при slots=0 больше не раздувает список.

### P0 — добить NDCG (ожидаемый +score)
1. ~~UC top-5 unseen~~ + **explore только на cold** ← в прогоне.
2. **Не ротировать UC head** (уже убрано) — не возвращать.
3. **Per-user порядок среди top hits на cold** оставить; на UC — стабильный score order.
4. Проверить: доля лайков на rank1–3 у UC ≥ 55–60% (было proxy ~44%).

### P1 — coverage без убийства precision
1. ~~Rare explore на cold~~ ← в прогоне.
2. ~~Second-tier popular в mid~~ ← в прогоне.
3. Цель реалистичная на hit-based системе: coverage **0.45–0.55**, не 0.8.

### P2 — не трогать без нужды
- Latency path (`add_items` batch, fast global candidates).
- Online update на collector (не переносить тяжёлое в `/recs`).
- Append-only CSV + dirty recompute.

### P3 — диагностика после каждого прогона
```bash
.venv/bin/python scripts/analyze_requests.py data/grader_requests.jsonl
# плюс ad-hoc: source mix, UC like positions, head_unseen, unique items, top10 share
```
Смотреть обязательно:
- `source` cold vs UC
- UC like position histogram (1..10)
- `head_unseen` / `head_co_like`
- unique items + top10 share
- interact count vs csv rows

## 6. Гипотезы «что даст следующий +5–15%»

| Идея | Зачем | Риск |
|------|-------|------|
| Explore=1 вместо 2, или explore только в cold | NDCG меньше размывается | coverage− |
| Second-tier popular (не top-5) в mid | coverage + чуть NDCG variety | precision− |
| Item-CF только из сильных co-like (≥N) | NDCG top-3 | мало рёбер в начале |
| Штраф overexposure в выдаче (не в candidates) | меньше одинаковых rank1 | precision− |
| Отдельный cold pool по «свежим» likes окна | быстрее подхватывать emerging hits | сложность |

## 7. Перед коммитом / вечером

Перезапущены и соответствуют коду с `head_unseen` + `explore_slots`:
- `recommendations` :5001
- `event_collector` :5000
- `regular_pipeline`

Если коммитите текущее дерево — в сообщении зафиксировать:
- online update + co-like/soft-link
- hit-heavy cold + unseen UC head
- explore slots в хвосте top-10
- scoreboard peak **58.4%** (и отдельно: прогон с explore уже на диске, дождаться/сверить официальные метрики если отличаются)

## 8. Короткий вердикт

Система прошла путь от «cold random / pipeline lag» до **hit + online personalization** с max precision/diversity и mean latency 40ms.  
Остаток скора почти целиком в **NDCG (порядок)** и **coverage (unique catalog exposure)**.  
Вечером: не усложнять модель; крутить **unseen head / explore budget / second-tier hits**, мерить position histogram на UC.
