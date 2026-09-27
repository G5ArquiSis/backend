"""Punto de entrada del backend: ensambla la aplicación FastAPI."""

from fastapi import FastAPI

from app.routers import health

app = FastAPI(title="EnergyShark E1", version="0.1.0")
app.include_router(health.router)
