"""Глобальна конфігурація та змінні середовища (Enterprise Core)."""

import os

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-super-secret-jwt-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = 15
JWT_REFRESH_EXPIRATION_DAYS = 7
BACKEND_VERSION = "2.0.0"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-webhook-secret-change-in-production")