from __future__ import annotations

import os

from celery import Celery

celery_app = Celery(
    "bidmaster",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2"),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "news-monitor": {
            "task": "services.celery_app.run_news_monitor",
            "schedule": 3600,
        },
        "news-crawl-hourly": {
            "task": "services.celery_app.crawl_news",
            "schedule": 3600,
            "args": [""],
        },
    },
)


@celery_app.task(
    name="services.celery_app.crawl_news",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,          # 指数退避
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=3,
)
def crawl_news_task(self, keywords: str = "", sites: list[str] | None = None, max_pages: int = 5):
    """真实业务任务：多源招标资讯抓取（roadmap P0-4）。失败自动重试，终态可经 backend 查询。"""
    import asyncio

    from core.skill_engine.base import SkillContext
    from services.news.skills.news_crawler_skill import NewsCrawlerSkill

    async def _run():
        skill = NewsCrawlerSkill()
        ctx = SkillContext(
            project_id="",
            db=None,
            llm=None,
            parameters={
                "keywords": keywords,
                "sites": sites or [],
                "max_pages": max_pages,
            },
        )
        result = await skill.safe_execute(ctx)
        if not result.success:
            raise RuntimeError(result.error or "资讯抓取失败")
        return {
            "task_id": self.request.id,
            "retries": self.request.retries,
            **result.data,
        }

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(name="services.celery_app.run_news_monitor", bind=True)
def run_news_monitor(self):
    import asyncio

    from sqlalchemy import select

    from services.database import async_session
    from services.models import MonitoringTask

    async def _run():
        async with async_session()() as db:
            result = await db.execute(select(MonitoringTask).where(MonitoringTask.enabled.is_(True)))
            tasks = result.scalars().all()
            results = []
            for task in tasks:
                results.append({"task_id": str(task.id), "name": task.name, "status": "processed"})
            return results

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(name="services.celery_app.run_full_check", bind=True)
def run_full_check(self, project_id: str):
    import asyncio

    async def _run():
        from core.skill_engine.base import SkillContext
        from services.check.skills.selfcheck_list_skill import SelfcheckListSkill
        from services.database import async_session
        from services.llm_factory import get_llm_gateway
        from services.routers.check import _get_tender_and_bid_text

        async with async_session()() as db:
            _, tender_text, bid_text = await _get_tender_and_bid_text(project_id, db)
            if not tender_text or not bid_text:
                return {"error": "招标文件或投标文件内容为空"}

            gateway = get_llm_gateway()
            skill = SelfcheckListSkill()
            ctx = SkillContext(
                project_id=project_id,
                db=db,
                llm=gateway,
                parameters={"tender_text": tender_text, "bid_text": bid_text},
            )
            result = await skill.safe_execute(ctx)
            return {"success": result.success, "data": result.data}

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@celery_app.task(name="services.celery_app.run_batch_format", bind=True)
def run_batch_format(self, file_paths: list[str], template: str = "default"):
    import asyncio

    async def _run():
        from core.skill_engine.base import SkillContext
        from services.format.skills.docx_format_skill import DocxFormatSkill
        from services.llm_factory import get_llm_gateway

        gateway = get_llm_gateway()
        skill = DocxFormatSkill()
        results = []
        for fp in file_paths:
            ctx = SkillContext(
                project_id="",
                db=None,
                llm=gateway,
                parameters={"file_path": fp, "template": template},
            )
            result = await skill.safe_execute(ctx)
            results.append(
                {
                    "file": fp,
                    "success": result.success,
                    "output": result.data.get("output_path") if result.success else result.error,
                }
            )
        return results

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
