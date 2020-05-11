#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from abc import ABC, abstractmethod
from mephisto.core.utils import find_or_create_qualification
from typing import (
    ClassVar,
    Optional,
    List,
    Dict,
    Any,
    Type,
    ClassVar,
    Union,
    Iterable,
    AsyncIterator,
    TYPE_CHECKING,
)

from recordclass import RecordClass

from mephisto.data_model.exceptions import (
    AgentReturnedError,
    AgentDisconnectedError,
    AgentTimeoutError,
)

if TYPE_CHECKING:
    from mephisto.data_model.agent import Agent, OnboardingAgent
    from mephisto.data_model.task import TaskRun
    from mephisto.data_model.assignment import Assignment, InitializationData, Unit
    from mephisto.data_model.packet import Packet
    from mephisto.data_model.worker import Worker
    from argparse import _ArgumentGroup as ArgumentGroup


class TaskBuilder(ABC):
    """
    Class to manage building a task of a specific type in a directory
    that will be used to deploy that task.
    """

    def __init__(self, task_run: "TaskRun", opts: Dict[str, Any]):
        self.opts = opts
        self.task_run = task_run

    def __new__(cls, task_run: "TaskRun", opts: Dict[str, Any]) -> "TaskBuilder":
        """Get the correct TaskBuilder for this task run"""
        from mephisto.core.registry import get_blueprint_from_type

        if cls == TaskBuilder:
            # We are trying to construct an TaskBuilder, find what type to use and
            # create that instead
            correct_class = get_blueprint_from_type(task_run.task_type).TaskBuilderClass
            return super().__new__(correct_class)
        else:
            # We are constructing another instance directly
            return super().__new__(cls)

    @abstractmethod
    def build_in_dir(self, build_dir: str) -> None:
        """
        Build the server for the given task run into the provided directory
        """
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def task_dir_is_valid(task_dir: str) -> bool:
        """
        Check the given task dir, and assert that the contents
        would be runnable with this task runner.
        """
        raise NotImplementedError()

    @classmethod
    def add_args_to_group(cls, group: "ArgumentGroup") -> None:
        """
        Defines builder options that are potentially usable for this task type,
        and adds them to the given argparser group. The group's 'description'
        attribute should be used to put any general help for these options.

        If the description field is left empty, the argument group is ignored
        """
        # group.description = 'For `TaskBuilder`, you can supply...'
        # group.add_argument('--task-option', help='Lets you customize')
        return


class TaskRunner(ABC):
    """
    Class to manage running a task of a specific type. Includes
    building the dependencies to a directory to be deployed to
    the server, and spawning threads that manage the process of
    passing agents through a task.
    """

    def __init__(self, task_run: "TaskRun", opts: Dict[str, Any]):
        self.opts = opts
        self.task_run = task_run
        self.running_assignments: Dict[str, "Assignment"] = {}
        self.running_units: Dict[str, "Unit"] = {}
        self.running_onboardings: Dict[str, "OnboardingAgent"] = {}
        self.is_concurrent = False
        # TODO(102) populate some kind of local state for tasks that are being run
        # by this runner from the database.

        self.block_qualification = opts.get("block_qualification")
        if self.block_qualification is not None:
            find_or_create_qualification(task_run.db, self.block_qualification)

    def __new__(cls, task_run: "TaskRun", opts: Dict[str, Any]) -> "TaskRunner":
        """Get the correct TaskRunner for this task run"""
        if cls == TaskRunner:
            from mephisto.core.registry import get_blueprint_from_type

            # We are trying to construct an AgentState, find what type to use and
            # create that instead
            correct_class = get_blueprint_from_type(task_run.task_type).TaskRunnerClass
            return super().__new__(correct_class)
        else:
            # We are constructing another instance directly
            return super().__new__(cls)

    def launch_onboarding(self, onboarding_agent: "OnboardingAgent") -> None:
        """
        Validate that onboarding is ready, then launch. Catch disconnect conditions
        """
        onboarding_id = onboarding_agent.get_agent_id()
        if onboarding_id in self.running_onboardings:
            print(f"Onboarding {onboarding_id} is already running")
            return

        print(f"Onboarding {onboarding_id} is launching with {onboarding_agent}")

        # At this point we're sure we want to run Onboarding
        self.running_onboardings[onboarding_id] = onboarding_agent
        try:
            self.run_onboarding(onboarding_agent)
            onboarding_agent.mark_done()
        except (AgentReturnedError, AgentTimeoutError, AgentDisconnectedError):
            self.cleanup_onboarding(onboarding_agent)
        except Exception as e:
            print(f"Unhandled exception in onboarding {onboarding_agent}: {repr(e)}")
            import traceback

            traceback.print_exc()
            self.cleanup_onboarding(onboarding_agent)
        del self.running_onboardings[onboarding_id]
        return

    def launch_unit(self, unit: "Unit", agent: "Agent") -> None:
        """
        Validate the unit is prepared to launch, then run it
        """
        if unit.db_id in self.running_units:
            print(f"Unit {unit.db_id} is already running")
            return

        print(f"Unit {unit.db_id} is launching with {agent}")

        # At this point we're sure we want to run the unit
        self.running_units[unit.db_id] = unit
        try:
            self.run_unit(unit, agent)
        except (AgentReturnedError, AgentTimeoutError, AgentDisconnectedError):
            # A returned Unit can be worked on again by someone else.
            unit.clear_assigned_agent()
            self.cleanup_unit(unit)
        except Exception as e:
            print(f"Unhandled exception in unit {unit}: {repr(e)}")
            import traceback

            traceback.print_exc()
            self.cleanup_unit(unit)
        del self.running_units[unit.db_id]
        return

    def launch_assignment(
        self, assignment: "Assignment", agents: List["Agent"]
    ) -> None:
        """
        Validate the assignment is prepared to launch, then run it
        """
        if assignment.db_id in self.running_assignments:
            print(f"Assignment {assignment.db_id} is already running")
            return

        print(f"Assignment {assignment.db_id} is launching with {agents}")

        # At this point we're sure we want to run the assignment
        self.running_assignments[assignment.db_id] = assignment
        try:
            self.run_assignment(assignment, agents)
        except (AgentReturnedError, AgentTimeoutError, AgentDisconnectedError) as e:
            # TODO(#99) if some operator flag is set for counting complete tasks, launch a
            # new assignment copied from the parameters of this one
            disconnected_agent_id = e.agent_id
            for agent in agents:
                if agent.db_id != e.agent_id:
                    agent.update_status(AgentState.STATUS_PARTNER_DISCONNECT)
                else:
                    # Must expire the disconnected unit so that
                    # new workers aren't shown it
                    agent.get_unit().expire()
            self.cleanup_assignment(assignment)
        except Exception as e:
            print(f"Unhandled exception in assignment {assignment}: {repr(e)}")
            import traceback

            traceback.print_exc()
            self.cleanup_assignment(assignment)
        del self.running_assignments[assignment.db_id]
        return

    @staticmethod
    def get_data_for_assignment(assignment: "Assignment") -> "InitializationData":
        """
        Finds the right data to get for the given assignment.
        """
        return assignment.get_assignment_data()

    @abstractmethod
    def get_init_data_for_agent(self, agent: "Agent"):
        """
        Return the data that an agent will need for their task.
        """
        raise NotImplementedError()

    def filter_units_for_worker(self, units: List["Unit"], worker: "Worker"):
        """
        Returns the list of Units that the given worker is eligible to work on.

        Some tasks may want more direct control of what units a worker is 
        allowed to work on, so this method should be overridden by children
        classes.
        """
        return units

    # TaskRunners must implement either the unit or assignment versions of the
    # run and cleanup functions, depending on if the task is run at the assignment
    # level rather than on the the unit level.

    def run_onboarding(self, agent: "OnboardingAgent"):
        """
        Handle setup for any resources to run an onboarding task. This
        will be run in a background thread, and should be tolerant to being
        interrupted by cleanup_onboarding.

        Only required by tasks that want to implement onboarding
        """
        raise NotImplementedError()

    def cleanup_onboarding(self, agent: "OnboardingAgent"):
        """
        Handle cleaning up the resources that were being used to onboard
        the given agent.
        """
        raise NotImplementedError()

    def run_unit(self, unit: "Unit", agent: "Agent"):
        """
        Handle setup for any resources required to get this unit running.
        This will be run in a background thread, and should be tolerant to
        being interrupted by cleanup_unit.

        Only needs to be implemented by non-concurrent tasks
        """
        raise NotImplementedError()

    def cleanup_unit(self, unit: "Unit"):
        """
        Handle ensuring resources for a given assignment are cleaned up following
        a disconnect or other crash event

        Does not need to be implemented if the run_unit method is
        already error catching and handles its own cleanup
        """
        raise NotImplementedError()

    def run_assignment(self, assignment: "Assignment", agents: List["Agent"]):
        """
        Handle setup for any resources required to get this assignment running.
        This will be run in a background thread, and should be tolerant to
        being interrupted by cleanup_assignment.

        Only needs to be implemented by concurrent tasks
        """
        raise NotImplementedError()

    def cleanup_assignment(self, assignment: "Assignment"):
        """
        Handle ensuring resources for a given assignment are cleaned up following
        a disconnect or other crash event

        Does not need to be implemented if the run_assignment method is
        already error catching and handles its own cleanup
        """
        raise NotImplementedError()

    @classmethod
    def add_args_to_group(cls, group: "ArgumentGroup") -> None:
        """
        Defines runner options that are potentially usable for this task type,
        and adds them to the given argparser group. The group's 'description'
        attribute should be used to put any general help for these options.

        If the description field is left empty, the argument group is ignored
        """
        # group.description = 'For `TaskRunner`, you can supply...'
        # group.add_argument('--task-option', help='Lets you customize something')
        return


# TODO(#101) what is the best method for creating new ones of these for different task types
# in ways that are supported by different backends? Perhaps abstract additional
# methods into the required db interface? Move any file manipulations into a
# extra_data_handler subcomponent of the MephistoDB class?
class AgentState(ABC):
    """
    Class for holding state information about work by an Agent on a Unit, currently
    stored as current task work into a json file.

    Specific state implementations will need to be created for different Task Types,
    as different tasks store and load differing data.
    """

    # Possible Agent Status Values
    STATUS_NONE = "none"
    STATUS_ACCEPTED = "accepted"
    STATUS_ONBOARDING = "onboarding"
    STATUS_WAITING = "waiting"
    STATUS_IN_TASK = "in task"
    STATUS_COMPLETED = "completed"
    STATUS_DISCONNECT = "disconnect"
    STATUS_TIMEOUT = "timeout"
    STATUS_PARTNER_DISCONNECT = "partner disconnect"
    STATUS_EXPIRED = "expired"
    STATUS_RETURNED = "returned"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    def __new__(cls, agent: Union["Agent", "OnboardingAgent"]) -> "AgentState":
        """Return the correct agent state for the given agent"""
        if cls == AgentState:
            from mephisto.data_model.agent import Agent
            from mephisto.core.registry import get_blueprint_from_type

            # We are trying to construct an AgentState, find what type to use and
            # create that instead
            if isinstance(agent, Agent):
                correct_class = get_blueprint_from_type(agent.task_type).AgentStateClass
            else:
                correct_class = get_blueprint_from_type(
                    agent.task_type
                ).OnboardingAgentStateClass
            return super().__new__(correct_class)
        else:
            # We are constructing another instance directly
            return super().__new__(cls)

    @staticmethod
    def complete() -> List[str]:
        """Return all final Agent statuses which cannot be updated"""
        return [
            AgentState.STATUS_COMPLETED,
            AgentState.STATUS_DISCONNECT,
            AgentState.STATUS_TIMEOUT,
            AgentState.STATUS_EXPIRED,
            AgentState.STATUS_RETURNED,
        ]

    @staticmethod
    def valid() -> List[str]:
        """Return all valid Agent statuses"""
        # TODO(#97) write a test that ensures all AgentState statuses are here
        return [
            AgentState.STATUS_NONE,
            AgentState.STATUS_ONBOARDING,
            AgentState.STATUS_WAITING,
            AgentState.STATUS_IN_TASK,
            AgentState.STATUS_COMPLETED,
            AgentState.STATUS_DISCONNECT,
            AgentState.STATUS_TIMEOUT,
            AgentState.STATUS_PARTNER_DISCONNECT,
            AgentState.STATUS_EXPIRED,
            AgentState.STATUS_RETURNED,
        ]

    # Implementations of an AgentState must implement the following:

    @abstractmethod
    def __init__(self, agent: "Agent"):
        """
        Create an AgentState to track the state of an agent's work on a Unit

        Implementations should initialize any required files for saving and
        loading state data somewhere.

        If said file already exists based on the given agent, load that data
        instead.
        """
        raise NotImplementedError()

    @abstractmethod
    def set_init_state(self, data: Any) -> bool:
        """Set the initial state for this agent"""
        raise NotImplementedError()

    @abstractmethod
    def get_init_state(self) -> Optional[Any]:
        """
        Return the initial state for this agent,
        None if no such state exists
        """
        raise NotImplementedError()

    @abstractmethod
    def load_data(self) -> None:
        """
        Load stored data from a file to this object
        """
        raise NotImplementedError()

    @abstractmethod
    def get_data(self) -> Dict[str, Any]:
        """
        Return the currently stored data for this task in the format
        expected by any frontend displays
        """
        raise NotImplementedError()

    def get_parsed_data(self) -> Any:
        """
        Return the portion of the data that is relevant to a human
        who wants to parse or analyze the data

        Utility function to handle stripping the data of any 
        context that is only important for reproducing the task
        exactly. By default is just `get_data`
        """
        return self.get_data()

    @abstractmethod
    def save_data(self) -> None:
        """
        Save the relevant data from this Unit to a file in the expected location
        """
        raise NotImplementedError()

    @abstractmethod
    def update_data(self, packet: "Packet") -> None:
        """
        Put new current Unit data into this AgentState
        """
        # TODO(#100) maybe refine the signature for this function once use cases
        # are fully scoped

        # Some use cases might just be appending new data, some
        # might instead prefer to maintain a final state.

        # Maybe the correct storage is of a series of actions taken
        # on this Unit? Static tasks only have 2 turns max, dynamic
        # ones may have multiple turns or steps.
        raise NotImplementedError()

    def get_task_start(self) -> Optional[float]:
        """
        Return the start time for this task, if it is available
        """
        return 0.0

    def get_task_end(self) -> Optional[float]:
        """
        Return the end time for this task, if it is available
        """
        return 0.0


class OnboardingRequired(object):
    """
    Compositional class for blueprints that may have an onboarding step
    """

    @staticmethod
    def get_failed_qual(qual_name: str) -> str:
        '''Returns the wrapper for a qualification to represent failing an onboarding'''
        return qual_name + '-failed'

    def init_onboarding_config(self, task_run: "TaskRun", opts: Dict[str, Any]):
        self.onboarding_qualification_name: Optional[str] = opts.get(
            "onboarding_qualification"
        )
        self.use_onboarding = self.onboarding_qualification_name is not None
        self.onboarding_qualification_id = None
        if self.onboarding_qualification_name is not None:
            db = task_run.db
            found_qualifications = db.find_qualifications(
                self.onboarding_qualification_name
            )
            if len(found_qualifications) == 0:
                self.onboarding_qualification_id = db.make_qualification(
                    self.onboarding_qualification_name
                )
            else:
                self.onboarding_qualification_id = found_qualifications[0].db_id
            
            # We need to keep a separate qualification for failed onboarding
            # to push to a crowd provider in order to prevent workers
            # who have failed from being shown our task
            self.onboarding_failed_name = self.get_failed_qual(self.onboarding_qualification_name)
            found_qualifications = db.find_qualifications(
                self.onboarding_failed_name
            )
            if len(found_qualifications) == 0:
                self.onboarding_failed_id = db.make_qualification(
                    self.onboarding_failed_name
                )
            else:
                self.onboarding_failed_id = found_qualifications[0].db_id

    @classmethod
    def add_args_to_group(cls, group: "ArgumentGroup") -> None:
        """
        Defines options that are relevant for tasks with onboarding steps. 

        Blueprints that use OnboardingRequired should call this method as part
        of add_args_to_group or supply these options themselves.
        """
        group.description = (
            "For tasks with onboarding, you should specify a qualification "
            "to grant to people who pass the onboarding "
        )
        group.add_argument(
            "--onboarding-qualification",
            help="Specify the name of a qualification used to ",
            dest="onboarding_qualification",
        )
        return

    def get_onboarding_data(self, worker_id: str) -> Dict[str, Any]:
        """
        If the onboarding task on the frontend requires any specialized data, the blueprint
        should provide it for the user.

        As onboarding qualifies a worker for all tasks from this blueprint, this should
        generally be static data that can later be evaluated against.
        """
        return {}

    def validate_onboarding(
        self, worker: "Worker", onboarding_agent: "OnboardingAgent"
    ) -> bool:
        """
        Check the incoming onboarding data and evaluate if the worker
        has passed the qualification or not. Return True if the worker
        has qualified.
        """
        return True


class Blueprint(ABC):
    """
    Configuration class for the various parts of building, launching,
    and running a task of a specific task. Provides utility functions
    for managing between the three main components, which are separated
    into separate classes in acknowledgement that some tasks may have
    particularly complicated processes for them
    """

    AgentStateClass: ClassVar[Type["AgentState"]]
    OnboardingAgentStateClass: ClassVar[Type["AgentState"]] = AgentState  # type: ignore
    TaskRunnerClass: ClassVar[Type["TaskRunner"]]
    TaskBuilderClass: ClassVar[Type["TaskBuilder"]]
    supported_architects: ClassVar[List[str]]
    BLUEPRINT_TYPE: str

    def __init__(self, task_run: "TaskRun", opts: Any):
        self.opts = opts

    @classmethod
    def assert_task_args(cls, args: Any):
        """
        Assert that the provided arguments are valid. Should 
        fail if a task launched with these arguments would
        not work
        """
        return

    @classmethod
    def add_args_to_group(cls, group: "ArgumentGroup") -> None:
        """
        Defines options that are potentially usable for this task type,
        and adds them to the given argparser group. The group's 'description'
        attribute should be used to put any general help for these options.

        These options are used to configure the way that the blueprint
        looks or otherwise runs tasks.

        If the description field is left empty, the argument group is ignored
        """
        runner_group = group.add_argument_group("task_runner_args")
        builder_group = group.add_argument_group("task_builder_args")
        cls.TaskRunnerClass.add_args_to_group(runner_group)
        cls.TaskBuilderClass.add_args_to_group(builder_group)
        # group.description = 'For `Blueprint`, you can supply...'
        # group.add_argument('--task-option', help='Lets you customize')
        return

    def get_frontend_args(self) -> Dict[str, Any]:
        """
        Specifies what options should be fowarded 
        to the client for use by the task's frontend
        """
        return {}

    @abstractmethod
    def get_initialization_data(
        self
    ) -> Union[Iterable["InitializationData"], AsyncIterator["InitializationData"]]:
        """
        Get all of the data used to initialize tasks from this blueprint.
        Can either be a simple iterable if all the assignments can 
        be processed at once, or an AsyncIterator if the number
        of tasks is unknown or changes based on something running
        concurrently with the job.
        """
        raise NotImplementedError
