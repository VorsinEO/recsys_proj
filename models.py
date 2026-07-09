import time
from typing import List, Optional, Literal

from pydantic import BaseModel, Field, model_validator

from config import TOP_K


class RecommendationsResponse(BaseModel):
    item_ids: List[str] = Field([], description="list of recommended items")


class InteractEvent(BaseModel):
    user_id: str = Field(description="identifier of user")
    item_ids: List[str] = Field(description="identifiers of interacted items")
    actions: List[Literal['like', 'dislike']] = Field(description="positive or negative reaction for items")
    timestamp: Optional[float] = Field(time.time(), description="timestamp of event")


class NewItemsEvent(BaseModel):
    item_ids: List[str] = Field(description="identifiers of new items")
    genres: List[List[str]] = Field(default_factory=list, description="genres per item")

    @model_validator(mode='after')
    def validate_genres_length(self):
        if self.genres and len(self.genres) != len(self.item_ids):
            raise ValueError('genres length must match item_ids length')
        if not self.genres:
            self.genres = [[] for _ in self.item_ids]
        return self
