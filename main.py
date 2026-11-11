"""Entry point for `uvicorn main:app` and Vercel's Python runtime."""
from app.boot import make_app

app = make_app()
