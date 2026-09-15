"""HTTP endpoint for the browser extension and the upload web app.

    $env:MENULENS_PROVIDER = "gemini"          # or anthropic, or mock (free, fake numbers)
    $env:GEMINI_API_KEY = "..."                # not needed for mock
    uvicorn server:app --port 8000

    curl -X POST localhost:8000/v1/estimate -H "Content-Type: application/json" -d '{
      "item": {"name": "Chicken Alfredo", "description": "Grilled chicken over fettuccine alfredo"},
      "restaurant": {"name": "Olive Garden", "cuisine": "italian", "price_tier": 2}
    }'

Browsers only let a web page call this server if its address is allowed. List the
allowed pages in MENULENS_ALLOWED_ORIGINS, comma-separated, for example the Lovable
preview URL. The extension doesn't need this: extensions call servers they have
host permission for directly.
"""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from estimator import Estimator, MenuItem
from providers import (ProviderAuthError, ProviderError, ProviderModelUnavailable,
                       ProviderRateLimited, Suppressed)

app = FastAPI(title="MenuLens estimator")

origins = [o.strip() for o in os.environ.get("MENULENS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST"],
                       allow_headers=["Content-Type"])

estimator = Estimator()  # provider and model come from MENULENS_PROVIDER / MENULENS_MODEL


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


@app.get("/health")
def health() -> dict:
    return {"ok": True, "provider": estimator.provider.name, "model": estimator.provider.model}


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
    except ProviderRateLimited:
        raise HTTPException(429, {"code": "rate_limited",
                                  "message": "Too many estimates right now. Try again in a minute."})
    except (ProviderAuthError, ProviderModelUnavailable):
        # A server misconfiguration, not the user's fault: don't echo key details to the client.
        raise HTTPException(503, {"code": "upstream_unavailable",
                                  "message": "The estimation service isn't configured correctly."})
    except ProviderError:
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
