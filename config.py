from pathlib import Path

TOP_K = 10
CANDIDATE_POOL_SIZE = 50
FLUSH_INTERVAL_SEC = 3
RECOMPUTE_INTERVAL_SEC = 3
EXPLORATION_RATE = 0.07
MMR_LAMBDA = 0.7

DATA_DIR = Path(__file__).resolve().parent / 'data'
INTERACTIONS_PATH = DATA_DIR / 'interactions.csv'

CATALOG_ALL_KEY = 'catalog:all'
CATALOG_ITEM_PREFIX = 'catalog:item:'
USER_CANDIDATES_PREFIX = 'user_candidates:'
USER_DISLIKED_PREFIX = 'user_disliked:'
GLOBAL_CANDIDATES_KEY = 'global_candidates'
RABBITMQ_QUEUE_NAME = 'user_interactions'
