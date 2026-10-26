"""Graphiti 0.30.1 client and confirmed semantic projection.

Pydantic AI owns conversation execution. Graphiti retains its native add_episode
and search behavior; only reviewed canonical snapshots enter the projection.
"""
import asyncio
import importlib.metadata
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone

from .model import encoded, require

EXTRACTION = """识别开放业务领域中实际提到的具体对象，不限合同、项目或设备类型。
金额、时间、状态、类型名称和模糊数量是属性，不要将它们当作业务对象。
保留对象原文名称；不自动把同名、简称、编号近似的对象合并。
用户说可能、大概、估计时保留这种不确定性，不推断精确金额或具体对象数量。
只提取提供的任务原始用户消息明确表达的事实；其他历史上下文只能用于理解指代，不能当作新来源。
"""

class Pipeline:
    def __init__(self, config, lane="approved"):
        self.config, self.client = config, None
        self.lock = asyncio.Lock()
        self.lane = lane
        self.pending = None
        if lane == "approved":
            require(config.uri != config.pending_uri,"index_isolation","待确认索引必须使用独立的 Neo4j 实例。",503)
            self.pending = Pipeline(replace(config,uri=config.pending_uri),lane="pending")

    def for_lane(self,lane):
        return self.pending if lane=="pending" else self

    async def graphiti(self):
        if self.client is not None:
            return self.client
        version = importlib.metadata.version("graphiti-core")
        require(version == "0.30.1", "graphiti_version", f"需要 graphiti-core 0.30.1，当前为 {version}。请使用 Demo 依赖环境。", 503)
        require(os.getenv("LLM_API_KEY"), "llm_not_configured", "请在本地环境配置 LLM_API_KEY。", 503)
        from graphiti_core import Graphiti
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
        from openai import AsyncOpenAI

        llm_config = LLMConfig(api_key=os.environ["LLM_API_KEY"],
                               base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
                               model=os.getenv("LLM_MODEL", "deepseek-chat"), temperature=0, max_tokens=8192)
        llm = OpenAIGenericClient(config=llm_config, max_tokens=8192, structured_output_mode="json_object",
                                  client=AsyncOpenAI(api_key=llm_config.api_key, base_url=llm_config.base_url, timeout=90, max_retries=1))
        embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
            api_key=os.getenv("EMBEDDING_API_KEY") or os.environ["LLM_API_KEY"],
            base_url=os.getenv("EMBEDDING_ENDPOINT", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            embedding_dim=int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))))
        driver = Neo4jDriver(self.config.uri, self.config.user, self.config.password, database=self.config.database)
        client = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder,
                          cross_encoder=OpenAIRerankerClient(config=llm_config, client=llm.client))
        try:
            if driver._init_task:
                await driver._init_task
            await client.build_indices_and_constraints()
        except BaseException:
            await client.close()
            raise
        self.client = client
        return self.client

    async def project(self, job, store):
        async with self.lock:
            from graphiti_core.nodes import EpisodeType
            graphiti = await self.graphiti()
            payload = json.loads(job["payload"])
            snapshot = store.projection_snapshot(payload)
            body = snapshot["body"]
            if not body["objects"]:
                store.projection_result(job["id"], "no_confirmed_facts", snapshot["hash"])
                return
            name = "kd:" + job["id"] + ":" + snapshot["hash"][:16]
            records = await graphiti.driver.execute_query("MATCH (e:Episodic {name:$name,group_id:$group}) RETURN e.uuid AS uuid ORDER BY e.created_at,e.uuid", params={"name": name, "group": self.config.database})
            rows = records.records if hasattr(records, "records") else records[0]
            # group_id must equal configured database for this upstream version;
            # arbitrary dataset names would cause a database clone.
            result = await graphiti.add_episode(name=name, uuid=rows[0]["uuid"] if rows else None,
                episode_body=encoded(body), source=EpisodeType.json,
                source_description="Human-confirmed Demo records. Canonical IDs are opaque, not business objects.",
                reference_time=datetime.fromtimestamp(job["committed_at"], timezone.utc), group_id=self.config.database,
                custom_extraction_instructions=EXTRACTION + "只提取已明确确认的值。correction 表示当前值更正；不得更改 KD 标签数据。")
            store.projection_result(job["id"], result.episode.uuid, snapshot["hash"])

    async def worker(self, store, stop):
        while not stop.is_set():
            try:
                for job in await asyncio.to_thread(store.index_jobs):
                    try:
                        await self.for_lane(job["lane"]).project_index(job,store)
                    except Exception:
                        logging.getLogger("demo").warning("semantic_projection_unavailable")
                        await asyncio.to_thread(store.index_result,job["id"])
            except Exception:
                logging.getLogger("demo").warning("projection_queue_unavailable")
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def project_index(self,job,store):
        body=json.loads(job['snapshot'])
        if not body['objects']:
            await asyncio.to_thread(store.index_result,job['id'],'')
            return
        async with self.lock:
            from graphiti_core.nodes import EpisodeType
            client=await self.graphiti()
            name='kd-index:'+job['id']
            records=await client.driver.execute_query('MATCH (e:Episodic {name:$name,group_id:$group}) RETURN e.uuid AS uuid ORDER BY e.created_at,e.uuid',params={'name':name,'group':self.config.database})
            rows=records.records if hasattr(records,'records') else records[0]
            # 0.30.1 treats a supplied uuid as an EXISTING episode to update.
            # Retry reuses the episode found by durable job name; first insert
            # must pass None. A single worker owns each local service instance.
            result=await client.add_episode(name=name,uuid=rows[0]['uuid'] if rows else None,episode_body=encoded(body),
                source=EpisodeType.json,source_description='Canonical projection; lane='+self.lane+'. Pending is NOT human approved; endpoint context is not an assertion.',
                reference_time=datetime.fromtimestamp(job['created_at'],timezone.utc),group_id=self.config.database,
                custom_extraction_instructions=EXTRACTION+'保留 review_status 和不确定性。objects 中已确认端点仅作上下文；proposals 是尚未生效的提议。不可将 pending 写成已确认事实。')
            await asyncio.to_thread(store.index_result,job['id'],result.episode.uuid)

    async def close(self):
        if self.client:
            await self.client.close()
        if self.pending:
            await self.pending.close()
