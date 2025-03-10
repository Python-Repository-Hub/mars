# Copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import functools
import logging
import operator
import pprint
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .... import oscar as mo
from ....core import ExecutionError
from ....core.graph import DAG
from ....core.operand import Fetch, FetchShuffle
from ....lib.aio import alru_cache
from ....metrics import Metrics
from ....oscar.errors import MarsError
from ....storage import StorageLevel
from ....utils import dataslots, get_chunk_key_to_data_keys, wrap_exception
from ...cluster import ClusterAPI
from ...meta import MetaAPI
from ...storage import StorageAPI
from ...subtask import Subtask, SubtaskAPI, SubtaskResult, SubtaskStatus
from .workerslot import BandSlotManagerActor
from .quota import QuotaActor

logger = logging.getLogger(__name__)

# the default times to run subtask.
DEFAULT_SUBTASK_MAX_RETRIES = 0


@dataslots
@dataclass
class SubtaskExecutionInfo:
    aio_task: asyncio.Task
    band_name: str
    supervisor_address: str
    result: SubtaskResult = field(default_factory=SubtaskResult)
    cancelling: bool = False
    max_retries: int = 0
    num_retries: int = 0
    slot_id: Optional[int] = None
    kill_timeout: Optional[int] = None


async def _retry_run(
    subtask: Subtask, subtask_info: SubtaskExecutionInfo, target_async_func, *args
):
    assert subtask_info.num_retries >= 0
    assert subtask_info.max_retries >= 0

    while True:
        try:
            return await target_async_func(*args)
        except (OSError, MarsError) as ex:
            if subtask_info.num_retries < subtask_info.max_retries:
                logger.error(
                    "Rerun[%s/%s] the %s of subtask %s due to %s.",
                    subtask_info.num_retries,
                    subtask_info.max_retries,
                    target_async_func,
                    subtask.subtask_id,
                    ex,
                )
                subtask_info.num_retries += 1
                continue
            if subtask_info.max_retries > 0:
                message = (
                    f"Exceed max rerun[{subtask_info.num_retries}/{subtask_info.max_retries}]:"
                    f" {target_async_func} of subtask {subtask.subtask_id} due to {ex}."
                )
                logger.error(message)

                raise wrap_exception(ex, wrap_name="_ExceedMaxRerun", message=message)
            else:
                raise ex
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            if subtask_info.max_retries > 0:
                message = (
                    f"Failed to rerun the {target_async_func} of subtask {subtask.subtask_id}, "
                    f"num_retries: {subtask_info.num_retries}, max_retries: {subtask_info.max_retries} "
                    f"due to unhandled exception: {ex}."
                )
                logger.error(message)

                raise wrap_exception(
                    ex, wrap_name="_UnhandledException", message=message
                )
            else:
                raise ex


def _fill_subtask_result_with_exception(
    subtask: Subtask, subtask_info: SubtaskExecutionInfo
):
    _, exc, tb = sys.exc_info()
    if isinstance(exc, ExecutionError):
        exc = exc.nested_error
        tb = exc.__traceback__

    exc_info = (type(exc), exc, tb)
    if isinstance(exc, asyncio.CancelledError):
        status = SubtaskStatus.cancelled
        logger.exception(
            "Cancel run subtask %s on band %s",
            subtask.subtask_id,
            subtask_info.band_name,
            exc_info=exc_info,
        )
    else:
        status = SubtaskStatus.errored
        logger.exception(
            "Failed to run subtask %s on band %s",
            subtask.subtask_id,
            subtask_info.band_name,
            exc_info=exc_info,
        )
    subtask_info.result.status = status
    subtask_info.result.progress = 1.0
    subtask_info.result.error = exc
    subtask_info.result.traceback = tb


class SubtaskExecutionActor(mo.StatelessActor):
    _subtask_info: Dict[str, SubtaskExecutionInfo]

    def __init__(
        self,
        subtask_max_retries: int = DEFAULT_SUBTASK_MAX_RETRIES,
        enable_kill_slot: bool = True,
        data_prepare_timeout: int = 600,
    ):
        self._cluster_api = None
        self._global_resource_ref = None
        self._subtask_max_retries = subtask_max_retries
        self._enable_kill_slot = enable_kill_slot
        self._data_prepare_timeout = data_prepare_timeout

        self._subtask_info = dict()
        self._submitted_subtask_count = Metrics.counter(
            "mars.band.submitted_subtask_count",
            "The count of submitted subtasks to the current band.",
            ("band",),
        )
        self._finished_subtask_count = Metrics.counter(
            "mars.band.finished_subtask_count",
            "The count of finished subtasks of the current band.",
            ("band",),
        )

    async def __post_create__(self):
        self._cluster_api = await ClusterAPI.create(self.address)

    @alru_cache(cache_exceptions=False)
    async def _get_slot_manager_ref(
        self, band: str
    ) -> mo.ActorRefType[BandSlotManagerActor]:
        return await mo.actor_ref(
            BandSlotManagerActor.gen_uid(band), address=self.address
        )

    @alru_cache(cache_exceptions=False)
    async def _get_band_quota_ref(self, band: str) -> mo.ActorRefType[QuotaActor]:
        return await mo.actor_ref(QuotaActor.gen_uid(band), address=self.address)

    async def _prepare_input_data(self, subtask: Subtask, band_name: str):
        queries = []
        shuffle_queries = []
        storage_api = await StorageAPI.create(
            subtask.session_id, address=self.address, band_name=band_name
        )
        chunk_key_to_data_keys = get_chunk_key_to_data_keys(subtask.chunk_graph)
        for chunk in subtask.chunk_graph:
            if chunk.key in subtask.pure_depend_keys:
                continue
            if chunk.op.gpu:  # pragma: no cover
                to_fetch_band = band_name
            else:
                to_fetch_band = "numa-0"
            if isinstance(chunk.op, Fetch):
                queries.append(
                    storage_api.fetch.delay(chunk.key, band_name=to_fetch_band)
                )
            elif isinstance(chunk.op, FetchShuffle):
                for key in chunk_key_to_data_keys[chunk.key]:
                    shuffle_queries.append(
                        storage_api.fetch.delay(
                            key, band_name=to_fetch_band, error="ignore"
                        )
                    )
        if queries:
            await storage_api.fetch.batch(*queries)
        if shuffle_queries:
            # TODO(hks): The batch method doesn't accept different error arguments,
            #  combine them when it can.

            # shuffle data fetch from remote won't be recorded in meta,
            # thus they are not tracked by lifecycle service,
            # here return remote mapper keys to remove them later.
            return await storage_api.fetch.batch(*shuffle_queries)

    async def _collect_input_sizes(
        self, subtask: Subtask, supervisor_address: str, band_name: str
    ):
        graph = subtask.chunk_graph
        sizes = dict()

        fetch_keys = list(
            set(
                n.key
                for n in graph.iter_indep()
                if isinstance(n.op, Fetch) and n.key not in subtask.pure_depend_keys
            )
        )
        if not fetch_keys:
            return sizes

        storage_api = await StorageAPI.create(
            subtask.session_id, address=self.address, band_name=band_name
        )
        meta_api = await MetaAPI.create(subtask.session_id, address=supervisor_address)

        fetch_metas = await meta_api.get_chunk_meta.batch(
            *(
                meta_api.get_chunk_meta.delay(k, fields=["memory_size", "store_size"])
                for k in fetch_keys
            )
        )
        data_infos = await storage_api.get_infos.batch(
            *(storage_api.get_infos.delay(k) for k in fetch_keys)
        )

        # compute memory quota size. when data located in shared memory, the cost
        # should be differences between deserialized memory cost and serialized cost,
        # otherwise we should take deserialized memory cost
        for key, meta, infos in zip(fetch_keys, fetch_metas, data_infos):
            level = functools.reduce(operator.or_, (info.level for info in infos))
            if level & StorageLevel.MEMORY:
                mem_cost = max(0, meta["memory_size"] - meta["store_size"])
            else:
                mem_cost = meta["memory_size"]
            sizes[key] = (meta["store_size"], mem_cost)

        return sizes

    @classmethod
    def _estimate_sizes(cls, subtask: Subtask, input_sizes: Dict):
        size_context = dict(input_sizes.items())
        graph = subtask.chunk_graph

        key_to_ops = defaultdict(set)
        chunk_key_to_sizes = defaultdict(lambda: 0)
        for n in graph:
            key_to_ops[n.op.key].add(n.op)
            chunk_key_to_sizes[n.key] += 1
        key_to_ops = {k: list(v) for k, v in key_to_ops.items()}

        # condense op key graph
        op_key_graph = DAG()
        for n in graph.topological_iter():
            if n.key in subtask.pure_depend_keys:
                continue
            if n.op.key not in op_key_graph:
                op_key_graph.add_node(n.op.key)
            for succ in graph.iter_successors(n):
                if succ.op.key not in op_key_graph:
                    op_key_graph.add_node(succ.op.key)
                op_key_graph.add_edge(n.op.key, succ.op.key)

        key_stack = list(op_key_graph.iter_indep())
        pred_ref_count = {k: op_key_graph.count_predecessors(k) for k in op_key_graph}
        succ_ref_count = {k: op_key_graph.count_successors(k) for k in op_key_graph}

        visited_op_keys = set()
        total_memory_cost = 0
        max_memory_cost = sum(calc_size for _, calc_size in size_context.values())
        while key_stack:
            key = key_stack.pop()
            op = key_to_ops[key][0]

            if not isinstance(op, Fetch):
                op.estimate_size(size_context, op)

            calc_cost = sum(size_context[out.key][1] for out in op.outputs)
            total_memory_cost += calc_cost
            max_memory_cost = max(total_memory_cost, max_memory_cost)

            if not isinstance(op, Fetch):
                # when calculation result is stored, memory cost of calculation
                #  can be replaced with result memory cost
                result_cost = sum(size_context[out.key][0] for out in op.outputs)
                total_memory_cost += result_cost - calc_cost

            visited_op_keys.add(key)

            for succ_op_key in op_key_graph.iter_successors(key):
                pred_ref_count[succ_op_key] -= 1
                if pred_ref_count[succ_op_key] == 0:
                    key_stack.append(succ_op_key)

            for pred_op_key in op_key_graph.iter_predecessors(key):
                succ_ref_count[pred_op_key] -= 1
                if succ_ref_count[pred_op_key] == 0:
                    pred_op = key_to_ops[pred_op_key][0]
                    outs = key_to_ops[pred_op_key][0].outputs
                    for out in outs:
                        chunk_key_to_sizes[out.key] -= 1
                    # when clearing fetches, subtract memory size, otherwise subtract store size
                    account_idx = 1 if isinstance(pred_op, Fetch) else 0
                    pop_result_cost = 0
                    for out in outs:
                        # corner case exist when a fetch op and another op has same chunk key
                        # but their op keys are different
                        if chunk_key_to_sizes[out.key] == 0:
                            pop_result_cost += size_context.pop(out.key, (0, 0))[
                                account_idx
                            ]
                        else:
                            pop_result_cost += size_context.get(out.key, (0, 0))[
                                account_idx
                            ]
                    total_memory_cost -= pop_result_cost
        return sum(t[0] for t in size_context.values()), max_memory_cost

    @classmethod
    def _check_cancelling(cls, subtask_info: SubtaskExecutionInfo):
        if subtask_info.cancelling:
            raise asyncio.CancelledError

    async def _remove_mapper_data(
        self, session_id: str, band_name: str, remote_mapper_keys: List
    ):
        storage_api = await StorageAPI.create(
            session_id, address=self.address, band_name=band_name
        )
        logger.debug("Delete mapper data %s", remote_mapper_keys)
        await storage_api.delete.batch(
            *[
                storage_api.delete.delay(key, error="ignore")
                for key in remote_mapper_keys
            ]
        )

    async def internal_run_subtask(self, subtask: Subtask, band_name: str):
        subtask_api = SubtaskAPI(self.address)
        subtask_info = self._subtask_info[subtask.subtask_id]
        subtask_info.result = SubtaskResult(
            subtask_id=subtask.subtask_id,
            session_id=subtask.session_id,
            task_id=subtask.task_id,
            stage_id=subtask.stage_id,
            status=SubtaskStatus.pending,
        )
        try:
            logger.debug("Preparing data for subtask %s", subtask.subtask_id)
            prepare_data_task = asyncio.create_task(
                _retry_run(
                    subtask, subtask_info, self._prepare_input_data, subtask, band_name
                )
            )
            remote_mapper_keys = await asyncio.wait_for(
                prepare_data_task, timeout=self._data_prepare_timeout
            )

            input_sizes = await self._collect_input_sizes(
                subtask, subtask_info.supervisor_address, band_name
            )
            _store_size, calc_size = await asyncio.to_thread(
                self._estimate_sizes, subtask, input_sizes
            )
            self._check_cancelling(subtask_info)

            batch_quota_req = {(subtask.session_id, subtask.subtask_id): calc_size}
            logger.debug("Start actual running of subtask %s", subtask.subtask_id)
            subtask_info.result = await self._retry_run_subtask(
                subtask, band_name, subtask_api, batch_quota_req
            )
            if remote_mapper_keys:
                await self.ref()._remove_mapper_data.tell(
                    subtask.session_id, band_name, remote_mapper_keys
                )
        except:  # noqa: E722  # pylint: disable=bare-except
            _fill_subtask_result_with_exception(subtask, subtask_info)
        finally:
            # make sure new slot usages are uploaded in time
            try:
                slot_manager_ref = await self._get_slot_manager_ref(band_name)
                await slot_manager_ref.upload_slot_usages(periodical=False)
            except:  # noqa: E722  # pylint: disable=bare-except
                _fill_subtask_result_with_exception(subtask, subtask_info)
            finally:
                # pop the subtask info at the end is to cancel the job.
                self._subtask_info.pop(subtask.subtask_id, None)
        return subtask_info.result

    async def _retry_run_subtask(
        self, subtask: Subtask, band_name: str, subtask_api: SubtaskAPI, batch_quota_req
    ):
        quota_ref = await self._get_band_quota_ref(band_name)
        slot_manager_ref = await self._get_slot_manager_ref(band_name)
        subtask_info = self._subtask_info[subtask.subtask_id]
        assert subtask_info.num_retries >= 0
        assert subtask_info.max_retries >= 0

        async def _run_subtask_once():
            aiotask = None
            slot_id = None
            try:
                await quota_ref.request_batch_quota(batch_quota_req)
                self._check_cancelling(subtask_info)

                slot_id = await slot_manager_ref.acquire_free_slot(
                    (subtask.session_id, subtask.subtask_id)
                )
                subtask_info.slot_id = slot_id
                self._check_cancelling(subtask_info)

                subtask_info.result.status = SubtaskStatus.running
                aiotask = asyncio.create_task(
                    subtask_api.run_subtask_in_slot(band_name, slot_id, subtask)
                )
                return await asyncio.shield(aiotask)
            except asyncio.CancelledError as ex:
                try:
                    if aiotask is not None:
                        logger.info(
                            "Start to cancel subtask %s in slot %s on band %s.",
                            subtask.subtask_id,
                            slot_id,
                            band_name,
                        )
                        await asyncio.wait_for(
                            asyncio.shield(
                                subtask_api.cancel_subtask_in_slot(band_name, slot_id)
                            ),
                            subtask_info.kill_timeout,
                        )
                except asyncio.TimeoutError:
                    logger.info(
                        "Wait for subtask to cancel timed out (%s). "
                        "Start killing slot %d",
                        subtask_info.kill_timeout,
                        slot_id,
                    )
                    await slot_manager_ref.kill_slot(slot_id)
                    sub_pool_address = await slot_manager_ref.get_slot_address(slot_id)
                    await mo.wait_actor_pool_recovered(sub_pool_address, self.address)
                except:  # pragma: no cover
                    logger.exception("Unexpected errors raised when handling cancel")
                    raise
                finally:
                    raise ex
            except (OSError, MarsError) as ex:
                if slot_id is not None:
                    # may encounter subprocess memory error
                    sub_pool_address = await slot_manager_ref.get_slot_address(slot_id)
                    await mo.wait_actor_pool_recovered(sub_pool_address, self.address)
                raise ex
            finally:
                # make sure allocated slots are traced
                if slot_id is None:  # pragma: no cover
                    slot_id = await slot_manager_ref.get_subtask_slot(
                        (subtask.session_id, subtask.subtask_id)
                    )
                logger.debug(
                    "Subtask %s running ended, slot_id=%r", subtask.subtask_id, slot_id
                )
                if slot_id is not None:
                    await slot_manager_ref.release_free_slot(
                        slot_id, (subtask.session_id, subtask.subtask_id)
                    )
                    logger.debug(
                        "Released slot %d for subtask %s", slot_id, subtask.subtask_id
                    )
                await quota_ref.release_quotas(tuple(batch_quota_req.keys()))

        # TODO(fyrestone): For the retryable op, we should rerun it when
        #  any exceptions occurred.
        if subtask.retryable:
            return await _retry_run(subtask, subtask_info, _run_subtask_once)
        else:
            try:
                return await _run_subtask_once()
            except Exception as e:
                unretryable_op = [
                    chunk.op
                    for chunk in subtask.chunk_graph
                    if not getattr(chunk.op, "retryable", True)
                ]
                message = (
                    f"Run subtask failed due to {e}, the subtask {subtask.subtask_id} is "
                    f"not retryable, it contains unretryable op: \n"
                    f"{pprint.pformat(unretryable_op)}"
                )
                logger.error(message)

                raise wrap_exception(
                    e, wrap_name="_UnretryableException", message=message
                )

    async def run_subtask(
        self, subtask: Subtask, band_name: str, supervisor_address: str
    ):
        if subtask.subtask_id in self._subtask_info:  # pragma: no cover
            raise Exception(
                f"Subtask {subtask.subtask_id} is already running on this band[{self.address}]."
            )
        logger.debug(
            "Start to schedule subtask %s on %s.", subtask.subtask_id, self.address
        )
        self._submitted_subtask_count.record(1, {"band": self.address})
        with mo.debug.no_message_trace():
            task = asyncio.create_task(
                self.ref().internal_run_subtask(subtask, band_name)
            )

        logger.debug("Subtask %r accepted in worker %s", subtask, self.address)
        # the extra_config may be None. the extra config overwrites the default value.
        subtask_max_retries = (
            subtask.extra_config.get("subtask_max_retries")
            if subtask.extra_config
            else None
        )
        if subtask_max_retries is None:
            subtask_max_retries = self._subtask_max_retries

        self._subtask_info[subtask.subtask_id] = SubtaskExecutionInfo(
            task, band_name, supervisor_address, max_retries=subtask_max_retries
        )
        result = await task
        self._subtask_info.pop(subtask.subtask_id, None)
        self._finished_subtask_count.record(1, {"band": self.address})
        logger.debug("Subtask %s finished with result %s", subtask.subtask_id, result)
        return result

    async def cancel_subtask(self, subtask_id: str, kill_timeout: Optional[int] = 5):
        try:
            subtask_info = self._subtask_info[subtask_id]
        except KeyError:
            logger.info("Subtask %s not exists, skip cancel.", subtask_id)
            return
        logger.info(
            "Start to cancel subtask %s in slot %s, kill_timeout is %s",
            subtask_id,
            subtask_info.slot_id,
            kill_timeout,
        )

        kill_timeout = kill_timeout if self._enable_kill_slot else None
        if not subtask_info.cancelling:
            subtask_info.kill_timeout = kill_timeout
            subtask_info.cancelling = True
            subtask_info.aio_task.cancel()

        await subtask_info.aio_task
        self._subtask_info.pop(subtask_id, None)
