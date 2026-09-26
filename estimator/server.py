"""HTTP endpoint for the browser extension and the upload web app.

    python -m uvicorn server:app --port 8000             # keys and settings come from estimator/.env

    POST /v1/menu/parse   pasted menu text -> list of dishes (no model call)
    POST /v1/estimate     one dish -> calorie range
    POST /v1/estimate/batch  up to 20 dishes from one restaurant -> one model request
    GET  /health          which provider and model are answering

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
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cache import EstimateCache
from estimator import MAX_BATCH, Estimate, Estimator, MenuItem
from menu_parse import MAX_ITEMS, parse_menu_text
from providers import (ProviderAuthError, ProviderError, ProviderModelUnavailable,
                       ProviderRateLimited, Suppressed)

app = FastAPI(title="MenuLens estimator")

origins = [o.strip() for o in os.environ.get("MENULENS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])

estimator = Estimator()  # provider and model come from MENULENS_PROVIDER / MENULENS_MODEL
cache = EstimateCache()


class ItemIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=500)
    section: str = Field(default="", max_length=80)


class RestaurantIn(BaseModel):
    name: str = Field(default="", max_length=120)
    cuisine: str = Field(default="", max_length=60)
    price_tier: int | None = Field(default=None, ge=1, le=4)
    sourcing_signals: list[str] = []


class EstimateRequest(BaseModel):
    item: ItemIn
    restaurant: RestaurantIn = RestaurantIn()


class BatchRequest(BaseModel):
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_BATCH)
    restaurant: RestaurantIn = RestaurantIn()


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "provider": estimator.provider.name, "model": estimator.provider.model}


@app.post("/v1/menu/parse")
def parse_menu(req: ParseRequest) -> dict:
    """Split pasted menu text into dishes. No model call, so it's free and instant."""
    items = parse_menu_text(req.text)
    return {"items": [{"name": i.name, "description": i.description, "section": i.section}
                      for i in items],
            "truncated": len(items) >= MAX_ITEMS}


def to_menu_item(item: ItemIn, restaurant: RestaurantIn) -> MenuItem:
    return MenuItem(name=item.name, description=item.description, section=item.section,
                    restaurant=restaurant.name, cuisine=restaurant.cuisine,
                    price_tier=restaurant.price_tier, sourcing_signals=restaurant.sourcing_signals)


def estimate_body(est: Estimate) -> dict:
    return {
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


def new_id() -> str:
    return f"est_{uuid.uuid4().hex[:12]}"


@contextmanager
def model_errors_as_http():
    """Errors that affect the whole request, turned into HTTP answers without leaking details."""
    try:
        yield
    except ProviderRateLimited as exc:
        if exc.daily:
            raise HTTPException(429, {"code": "daily_quota",
                                      "message": "Today's free estimate limit is used up. Try again tomorrow."})
        raise HTTPException(429, {"code": "rate_limited",
                                  "message": "Too many estimates right now. Try again in a minute.",
                                  "retry_after_s": round(exc.retry_after or 20)})
    except (ProviderAuthError, ProviderModelUnavailable):
        # A server misconfiguration, not the user's fault: don't echo key details to the client.
        raise HTTPException(503, {"code": "upstream_unavailable",
                                  "message": "The estimation service isn't configured correctly."})
    except ProviderError:
        raise HTTPException(503, {"code": "upstream_unavailable",
                                  "message": "The estimation model is unavailable. Try again."})


@app.post("/v1/estimate")
def estimate(req: EstimateRequest) -> dict:
    item = to_menu_item(req.item, req.restaurant)
    key = cache.key(estimator.provider.name, item.to_prompt())
    if cached := cache.get(key):
        status, body = cached
        if status != 200:
            raise HTTPException(status, body)
        return {"estimate_id": new_id(), **body, "cached": True}

    with model_errors_as_http():
        try:
            est = estimator.estimate(item)
        except Suppressed as exc:
            detail = {"code": "insufficient_signal", "message": str(exc)}
            cache.put(key, 422, detail)
            raise HTTPException(422, detail)

    body = estimate_body(est)
    cache.put(key, 200, body)
    return {"estimate_id": new_id(), **body, "cached": False}


@app.post("/v1/estimate/batch")
def estimate_batch(req: BatchRequest) -> dict:
    """Up to MAX_BATCH dishes from one restaurant, answered by one model request.

    Dishes already in the cache don't reach the model. Each result has a status:
    "ok" (with the same fields as /v1/estimate), "insufficient_signal" (too vague to
    estimate), or "error" (the model skipped or garbled this dish; ask again). If the
    whole request fails, for example on a rate limit, the answer is the same HTTP error
    /v1/estimate gives.
    """
    items = [to_menu_item(i, req.restaurant) for i in req.items]
    keys = [cache.key(estimator.provider.name, i.to_prompt()) for i in items]
    results: list[dict | None] = [None] * len(items)
    for n, key in enumerate(keys):
        if cached := cache.get(key):
            status, body = cached
            results[n] = ({"status": "ok", "estimate_id": new_id(), **body, "cached": True}
                          if status == 200 else {"status": body["code"], **body, "cached": True})

    todo = [n for n, r in enumerate(results) if r is None]
    if todo:
        with model_errors_as_http():
            answers = estimator.estimate_batch([items[n] for n in todo])
        for n, answer in zip(todo, answers):
            if isinstance(answer, Estimate):
                body = estimate_body(answer)
                cache.put(keys[n], 200, body)
                results[n] = {"status": "ok", "estimate_id": new_id(), **body, "cached": False}
            elif isinstance(answer, Suppressed):
                detail = {"code": "insufficient_signal", "message": str(answer)}
                cache.put(keys[n], 422, detail)
                results[n] = {"status": "insufficient_signal", **detail, "cached": False}
            else:
                results[n] = {"status": "error", "message": "The estimator skipped this dish. Try again."}
    return {"results": results, "model_requests": 1 if todo else 0}
