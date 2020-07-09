#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


# TODO(#99) do we standardize some kinds of data loader formats? perhaps
# one that loads from files, and then an arbitrary kind? Simple
# interface could be like an iterator. This class will launch tasks
# as if the loader is an iterator.

from mephisto.data_model.assignment import (
    Assignment,
    Unit,
    InitializationData,
    AssignmentState,
)

from typing import Dict, Optional, List, Any, TYPE_CHECKING, Iterator
import os
import time
import enum

if TYPE_CHECKING:
    from mephisto.data_model.task import TaskRun
    from mephisto.data_model.database import MephistoDB

import threading
from mephisto.core.logger_core import get_logger
import types

logger = get_logger(name=__name__, verbose=True, level="debug")

UNIT_GENERATOR_WAIT_SECONDS = 10
GENERATOR_WAIT_SECONDS = 0.5


class GeneratorType(enum.Enum):
    none = 0
    unit = 1
    assignment = 2


class TaskLauncher:
    """
    This class is responsible for managing the process of registering
    and launching units, including the steps for pre-processing
    data and storing them locally for assignments when appropriate.
    """

    def __init__(
        self,
        db: "MephistoDB",
        task_run: "TaskRun",
        assignment_data_iterator: Iterator[InitializationData],
        max_num_concurrent_units: int = 0,
    ):
        """Prepare the task launcher to get it ready to launch the assignments"""
        self.db = db
        self.task_run = task_run
        self.assignment_data_iterable = assignment_data_iterator
        self.assignments: List[Assignment] = []
        self.units: List[Unit] = []
        self.provider_type = task_run.get_provider().PROVIDER_TYPE
        self.max_num_concurrent_units = max_num_concurrent_units
        self.launched_units: Dict[str, Unit] = {}
        self.unlaunched_units: Dict[str, Unit] = {}
        self.keep_launching_units: bool = False
        self.finished_generators: bool = False
        self.did_create_assignments = False
        self.did_launch_units = False
        self.unlaunched_units_access_condition = threading.Condition()
        if isinstance(self.assignment_data_iterable, types.GeneratorType):
            self.generator_type = GeneratorType.assignment
        else:
            self.generator_type = GeneratorType.none
        run_dir = task_run.get_run_dir()
        os.makedirs(run_dir, exist_ok=True)

        logger.debug(f"type of assignment data: {type(self.assignment_data_iterable)}")
        if self.generator_type is not GeneratorType.none:
            self.finished_generators = False
            main_thread = threading.Thread(target=self.manage_generators, args=())
            main_thread.start()

    def manage_generators(self) -> None:
        while not self.finished_generators:
            if self.did_create_assignments:
                # try generating assignments
                self._try_generate_assignments()
            time.sleep(GENERATOR_WAIT_SECONDS)

    def _create_single_assignment(self, assignment_data) -> None:
        """ Create a single assignment in the database using its read assignment_data """
        task_run = self.task_run
        task_config = task_run.get_task_config()
        assignment_id = self.db.new_assignment(
            task_run.task_id,
            task_run.db_id,
            task_run.requester_id,
            task_run.task_type,
            task_run.provider_type,
            task_run.sandbox,
        )
        assignment = Assignment(self.db, assignment_id)
        assignment.write_assignment_data(assignment_data)
        self.assignments.append(assignment)
        unit_count = len(assignment_data["unit_data"])
        for unit_idx in range(unit_count):
            unit_id = self.db.new_unit(
                task_run.task_id,
                task_run.db_id,
                task_run.requester_id,
                assignment_id,
                unit_idx,
                task_config.task_reward,
                task_run.provider_type,
                task_run.task_type,
                task_run.sandbox,
            )
            self.units.append(Unit(self.db, unit_id))
            with self.unlaunched_units_access_condition:
                self.unlaunched_units[unit_id] = Unit(self.db, unit_id)
            self.keep_launching_units = True

    def _try_generate_assignments(self) -> None:
        """ Try to generate more assignments from the assignments_data_iterator"""
        try:
            data = next(self.assignment_data_iterable)
            self._create_single_assignment(data)
        except StopIteration:
            self.did_create_assignments = False
            self.did_launch_units = False
            self.finished_generators = True

    def create_assignments(self) -> None:
        """ Create an assignment and associated units for the generated assignment data """
        if self.generator_type == GeneratorType.none:
            for data in self.assignment_data_iterable:
                self._create_single_assignment(data)
        else:
            self.did_create_assignments = True
            self.did_launch_units = True

    def generate_units(self):
        """ units generator which checks that only 'max_num_concurrent_units' running at the same time,
        i.e. in the LAUNCHED or ASSIGNED states """
        while self.keep_launching_units:
            units_id_to_remove = []
            for db_id, unit in self.launched_units.items():
                status = unit.get_status()
                if (
                    status != AssignmentState.LAUNCHED
                    and status != AssignmentState.ASSIGNED
                ):
                    units_id_to_remove.append(db_id)
            for db_id in units_id_to_remove:
                self.launched_units.pop(db_id)

            num_avail_units = self.max_num_concurrent_units - len(self.launched_units)
            num_avail_units = (
                len(self.unlaunched_units)
                if self.max_num_concurrent_units == 0
                else num_avail_units
            )

            units_id_to_remove = []
            for i, item in enumerate(self.unlaunched_units.items()):
                db_id, unit = item
                if i < num_avail_units:
                    self.launched_units[unit.db_id] = unit
                    units_id_to_remove.append(db_id)
                    yield unit
                else:
                    break
            with self.unlaunched_units_access_condition:
                for db_id in units_id_to_remove:
                    self.unlaunched_units.pop(db_id)

            time.sleep(UNIT_GENERATOR_WAIT_SECONDS)
            if not self.unlaunched_units:
                break

    def _launch_limited_units(self, url: str) -> None:
        """ use units' generator to launch limited number of units according to (max_num_concurrent_units)"""
        while self.did_launch_units:
            for unit in self.generate_units():
                unit.launch(url)
            if self.generator_type == GeneratorType.none:
                self.did_launch_units = False
                break

    def launch_units(self, url: str) -> None:
        """launch any units registered by this TaskLauncher"""
        self.did_launch_units = True
        thread = threading.Thread(target=self._launch_limited_units, args=(url,))
        thread.start()

    def expire_units(self) -> None:
        """Clean up all units on this TaskLauncher"""
        self.keep_launching_units = False
        self.finished_generators = True
        for unit in self.units:
            try:
                unit.expire()
            except Exception as e:
                logger.exception(
                    f"Warning: failed to expire unit {unit.db_id}. Stated error: {e}",
                    exc_info=True,
                )
