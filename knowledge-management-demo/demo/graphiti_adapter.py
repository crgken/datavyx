"""Version-bound Graphiti retrieval, canonical mapping and projection port."""
import json

from .model import DemoError, require


class GraphitiAdapter:
    def __init__(self, store, pipeline, scope):
        self.store, self.pipeline, self.scope = store, pipeline, scope

    async def _legacy_retrieve_context(self, query, limit=8):
        self.scope.check(self.store, "read")
        require(isinstance(query, str) and 0 < len(query) <= 4000 and 1 <= limit <= 20,
                "invalid_memory_query", "记忆检索参数无效。")
        # This version uses the configured database as group_id. It is not an
        # authorization boundary. Episode provenance is checked before any
        # retrieved text is exposed to the model.
        jobs = self.store.read(lambda tx: [dict(r) for r in tx.run(
            "MATCH (p:KDDraft {dataset:$d,status:'committed',projection_status:'ready'}) WHERE p.episode_id IS NOT NULL RETURN p.episode_id AS episode,p.payload AS payload,p.snapshot_hash AS snapshot_hash",
            d=self.store.dataset)])
        allowed = {j["episode"]: json.loads(j["payload"]) for j in jobs if j["snapshot_hash"] and j["episode"] != "no_confirmed_facts"
                   and self.store.projection_snapshot(json.loads(j["payload"]))["hash"] == j["snapshot_hash"]}
        if not allowed:
            return {"status": "not_indexed", "items": [], "complete": False,
                    "scope": "当前数据范围尚无可核验的Graphiti episode；可通过规范数据工具继续查询。", **self.store.health()}
        try:
            async with self.pipeline.lock:
                client = await self.pipeline.graphiti()
                from graphiti_core.search.search_filters import SearchFilters
                records = await client.driver.execute_query(
                    "MATCH ()-[e:RELATES_TO]->() WHERE e.group_id=$group AND size(coalesce(e.episodes,[]))>0 "
                    "AND all(ep IN e.episodes WHERE ep IN $episodes) RETURN e.uuid AS id",
                    params={"group": self.store.config.database, "episodes": list(allowed)})
                rows = records.records if hasattr(records, "records") else records[0]
                edge_ids = [row["id"] for row in rows]
                if not edge_ids:
                    return {"status": "ok", "items": [], "complete": False, "filtered_unverified": 0,
                            "scope": "当前数据范围没有可核验的语义关系；可继续查规范对象。", **self.store.health()}
                edges = await client.search(query, group_ids=[self.store.config.database], num_results=limit,
                                            search_filter=SearchFilters(edge_uuids=edge_ids))
        except Exception as exc:
            raise DemoError("memory_unavailable", "Graphiti记忆检索失败，请稍后重试；不能将失败解释为没有记录。", 503) from exc
        items, filtered = [], 0
        for edge in edges:
            episodes = set(edge.episodes)
            if not episodes or not episodes <= set(allowed) or edge.invalid_at is not None:
                filtered += 1
                continue
            # Recheck after asynchronous search: a concurrent withdrawal wins.
            if any(not any(j["episode"] == ep and j["snapshot_hash"] == self.store.projection_snapshot(allowed[ep])["hash"] for j in jobs) for ep in episodes):
                filtered += 1
                continue
            ids = list(dict.fromkeys(o["canonical_id"] for ep in episodes for o in self.store.projection_snapshot(allowed[ep])["body"]["objects"]))
            snapshots = [self.store.detail(oid, limit=25) for oid in ids]
            source_ids = list(dict.fromkeys(s["id"] for ep in episodes for s in allowed[ep].get("source_messages", [{"id": allowed[ep]["message_id"]}])))
            items.append({"memory_id": edge.uuid, "semantic_hint": edge.fact,
                          "valid_at": str(edge.valid_at) if edge.valid_at else None,
                          "invalid_at": str(edge.invalid_at) if edge.invalid_at else None,
                          "canonical_refs": ids, "canonical_records": snapshots, "source_message_ids": source_ids,
                          "note": "记忆仅供候选理解；当前事实以规范对象记录为准，不能用命中数做完整统计。"})
        return {"status": "ok", "items": items, "complete": False, "filtered_unverified": filtered,
                "scope": "当前数据范围内、具有可核验episode来源的相关性检索", **self.store.health()}

    async def retrieve_context(self, query, limit=8):
        self.scope.check(self.store, 'read')
        require(isinstance(query,str) and 0<len(query)<=4000 and type(limit) is int and 1<=limit<=20,
                'invalid_memory_query','记忆检索参数无效。')
        items,seen,failures,indexing,filtered=[],set(),[],[],0
        from graphiti_core.search.search_filters import SearchFilters
        for lane in ('approved','pending'):
            allowed=self.store.index_bindings(lane)
            if not allowed:
                continue
            pipeline=self.pipeline.for_lane(lane)
            if pipeline.lock.locked():
                indexing.append(lane)
                continue  # A background projection must not hold up canonical queries.
            known=self.store.known_index_episodes(lane)
            try:
                async with pipeline.lock:
                    client=await pipeline.graphiti()
                    records=await client.driver.execute_query('MATCH ()-[e:RELATES_TO]->() WHERE e.group_id=$group AND size(coalesce(e.episodes,[]))>0 AND all(ep IN e.episodes WHERE ep IN $known) AND any(ep IN e.episodes WHERE ep IN $episodes) RETURN e.uuid AS id',params={'group':pipeline.config.database,'episodes':list(allowed),'known':list(known)})
                    rows=records.records if hasattr(records,'records') else records[0]
                    edge_ids=[r['id'] for r in rows]
                    if not edge_ids:
                        continue
                    edges=await client.search(query,group_ids=[pipeline.config.database],num_results=limit,search_filter=SearchFilters(edge_uuids=edge_ids))
                current=self.store.index_bindings(lane)
                for edge in edges:
                    episodes=set(edge.episodes)
                    support=episodes & allowed.keys()
                    if edge.uuid not in edge_ids or not support or edge.invalid_at is not None or not episodes <= known or any(ep not in current or current[ep]['hash']!=allowed[ep]['hash'] for ep in support):
                        filtered+=1
                        continue
                    ids=sorted({allowed[ep]['body']['root_id'] for ep in support})
                    fresh=[oid for oid in ids if oid not in seen]
                    if not fresh:
                        continue
                    snapshots=[self.store.detail(oid,limit=100) for oid in fresh]
                    latest=self.store.index_bindings(lane)
                    if any(ep not in latest or latest[ep]['hash']!=allowed[ep]['hash'] for ep in support):
                        filtered+=1
                        continue
                    seen.update(fresh)
                    items.append({'memory_id':edge.uuid,'lane':lane,'semantic_hint':edge.fact if episodes<=allowed.keys() else '相关对象候选；以当前规范记录为准。','canonical_refs':fresh,
                        'canonical_records':snapshots,'source_message_ids':sorted({s['id'] for ep in support for s in allowed[ep]['body']['sources']}),
                        'note':'语义内容仅作为候选；事实以规范记录的确认状态和当前版本为准。未确认信息不得当作已确认事实。'})
            except Exception:
                failures.append(lane)
        health=self.store.health()
        return {'status':'partial_error' if failures else 'indexing' if indexing else 'ok' if items else 'not_indexed' if health['projection_pending'] else 'ok',
                'items':items[:limit],'complete':False,'filtered_unverified':filtered,'failed_indexes':failures,
                'indexing_indexes':indexing,
                'warning':'部分 Graphiti 索引检索失败，请使用规范数据查询核验。' if failures else '',
                'scope':'已确认与待确认隔离索引的候选检索；不代表完整清单或统计。',**health}
