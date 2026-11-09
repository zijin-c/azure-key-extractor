"""网页任务的身份和产品范围；ContextVar 随 asyncio 子任务传递，不在线程间共享。"""
import os
from contextlib import contextmanager
from contextvars import ContextVar

_USER = ContextVar("task_user", default=None)
_PRODUCTS = ContextVar("task_products", default=None)


def current_task_user():
    return _USER.get()


def current_task_products():
    return _PRODUCTS.get()


def scoped_data_path(base):
    user = current_task_user()
    return os.path.join(base, f"user_{user}") if user is not None else base


@contextmanager
def task_scope(user_id, products=None):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("任务需要有效的用户身份")
    user_token = _USER.set(user_id)
    products_token = _PRODUCTS.set(tuple(products) if products is not None else None)
    try:
        yield
    finally:
        _PRODUCTS.reset(products_token)
        _USER.reset(user_token)


@contextmanager
def system_task_scope():
    user_token = _USER.set(None)
    products_token = _PRODUCTS.set(None)
    try:
        yield
    finally:
        _PRODUCTS.reset(products_token)
        _USER.reset(user_token)
