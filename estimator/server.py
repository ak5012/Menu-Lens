"""HTTP endpoint for the browser extension and upload flow.

    uvicorn server:app --port 8000

    curl -X POST localhost:8000/v1/estimate -H "Content-Type: application/json" -d '{
      "item": {"name": "Chicken Alfredo", "description": "Grilled chicken over fettuccine alfredo"},
      "restaurant": {"name": "Olive Garden", "cuisine": "italian", "price_tier": 2}
    }'
"""

from __future__ import annotations

import uuid

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from estimator import DEFAULT_MODEL, Estimator, MenuItem, Suppressed

app = FastAPI(title="Menu Calorie Estimator")
estimator = Estimator(DEFAULT_MODEL)


class ItemIn(BaseModel):
    name: str = Field(min_length=2)
    description: str = ""
    section: str = ""


class RestaurantIn(BaseModel):
    name: str = ""
    cuisine: str = ""
    price_tier: int | None = Field(default=None, ge=1, le=4)
    sourcing_signals: list[str] = []


class EstimateRequest(BaseModel):
    item: ItemIn
    restaurant: RestaurantIn = RestaurantIn()


@app.post("/v1/estimate")
def estimate(req: EstimateRequest) -> dict:
    item = MenuItem(
        name=req.item.name,
        description=req.item.description,
        section=req.item.section,
        restaurant=req.restaurant.name,
        cuisine=req.restaurant.cuisine,
        price_tier=req.restaurant.price_tier,
        sourcing_signals=req.restaurant.sourcing_signals,
    )
    try:
        est = estimator.estimate(item)
    except Suppressed as exc:
        raise HTTPException(422, {"code": "insufficient_signal", "message": str(exc)})
    except anthropic.RateLimitError:
        raise HTTPException(429, {"code": "rate_limited", "message": "Try again shortly."})
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        raise HTTPException(503, {"code": "upstream_unavailable",
                                  "message": "The estimation model is unavailable. Try again."})

    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:12]}",
        "calories": {"low": est.low, "high": est.high, "midpoint": est.midpoint},
        "confidence": {"band": est.band},
        "rationale": est.rationale,
        "basis": {
            "portion_assumption_g": est.portion_assumption_g,
            "range_widened": est.widened,
            "model": est.model,
        },
        "disclaimer": "Estimated range, not a nutrition label.",
    }
