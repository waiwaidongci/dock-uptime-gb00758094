"""保养整批排程的提交、幂等复用与查询。

每次排程绑定确定的数据版本（data_revisions.checksum）与规范化后的保养单批次，
以两者内容的 SHA-256 作为计划版本键：
- 同一版本键重复提交直接返回既有计划，不新增结果；
- 排程中断后重试仍读到原计划记录，不会产生第二条安排；
- 整批排不下时只保存冲突清单，state 标记为 conflicted，不产生任何部分安排。
"""

import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, maintenance_plans
from src.services.scheduling import SchedulingError, schedule_maintenance

PLANNED_STATE = 'scheduled'
CONFLICT_STATE = 'conflicted'


def _plan_checksum(dataset_checksum: str, month: str, orders: list[dict]) -> str:
    payload = json.dumps(
        {'dataset': dataset_checksum, 'month': month,
         'orders': sorted(orders, key=lambda item: item['record_id'])},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def submit_plan(body, actor):
    month = body.month.strip()
    orders = [item.model_dump() for item in body.orders]
    now = time.time()
    try:
        with engine.begin() as connection:
            dataset = fetch_one(connection, datasets, body.dataset_id)
            if dataset is None:
                raise HTTPException(404, '数据版本不存在。')
            checksum = _plan_checksum(dataset['checksum'], month, orders)
            existing = connection.execute(
                select(maintenance_plans).where(maintenance_plans.c.checksum == checksum)
            ).mappings().one_or_none()
            if existing is not None:
                return {'id': existing['id'], 'created': False, 'state': existing['state'],
                        'feasible': existing['result']['feasible']}
            try:
                result = schedule_maintenance(dataset['records'], orders, {'month': month})
            except SchedulingError as error:
                # 输入前提不满足：不落任何计划，交由调用方按 422 提示修正。
                raise HTTPException(422, str(error)) from error
            state = PLANNED_STATE if result['feasible'] else CONFLICT_STATE
            # 并发竞态时唯一约束会在此抛出 IntegrityError，随事务回滚后在外层兜底。
            plan_id = connection.execute(insert(maintenance_plans).values(
                dataset_id=body.dataset_id, checksum=checksum, month=month, orders=orders,
                state=state, result=result, owner=actor, created=now)).inserted_primary_key[0]
            add_event(connection, actor, 'schedule_plan', plan_id, now)
        return {'id': plan_id, 'created': True, 'state': state, 'feasible': result['feasible']}
    except IntegrityError:
        # 并发重复提交：复用另一事务已落库的同版本计划，不新增结果。
        with engine.connect() as connection:
            found = connection.execute(
                select(maintenance_plans).where(maintenance_plans.c.checksum == checksum)
            ).mappings().one()
        return {'id': found['id'], 'created': False, 'state': found['state'],
                'feasible': found['result']['feasible']}


def list_plans():
    with engine.connect() as connection:
        rows = connection.execute(
            select(maintenance_plans.c.id, maintenance_plans.c.dataset_id, maintenance_plans.c.month,
                   maintenance_plans.c.state, maintenance_plans.c.owner, maintenance_plans.c.created,
                   maintenance_plans.c.checksum)
            .order_by(maintenance_plans.c.id.desc()).limit(100)
        ).mappings().all()
    return [dict(row) for row in rows]


def get_plan(key):
    with engine.connect() as connection:
        row = fetch_one(connection, maintenance_plans, key)
    if row is None:
        raise HTTPException(404, '保养排程不存在。')
    return dict(row)
