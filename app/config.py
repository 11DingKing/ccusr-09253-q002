"""服务端业务模块。"""
from __future__ import annotations
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./practice_hours.db")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite:///./practice_hours_test.db")
