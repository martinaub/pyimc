import socket
import logging
import inspect
from operator import itemgetter
from contextlib import suppress
import types
from typing import Dict, List, Tuple

import pyimc
from pyimc.decorators import *
from pyimc.network.udp import IMCSenderUDP, multicast_ip, IMCProtocolUDP
from pyimc.network.utils import get_interfaces
from pyimc.node import IMCNode
from pyimc.exception import AmbiguousKeyError

logger = logging.getLogger('pyimc.actors.base')


class IMCBase:
    """
    Base class for IMC communications.
    Implements an event loop, subscriptions, IMC node bookkeeping
    """
    def __init__(self):
        # Asyncio loop, tasks, and callbacks
        self._loop = None  # type: asyncio.BaseEventLoop
        self._task_mc = None  # type: asyncio.Task
        self._task_imc = None  # type: asyncio.Task
        self._subs = {}  # type: Dict[Type[pyimc.Message], List[types.MethodType]]

        # IMC/Multicast ports (assigned when socket is created)
        self._port_imc = None  # type: int
        self._port_mc = None  # type: int

        # Using a map from (imc address, sys_name) to a node instance
        self.nodes = {}  # type: Dict[Tuple[int, str], IMCNode]

        # Overridden in subclasses
        self.announce = None
        self.entities = {'Daemon': 0}
        self.services = None  # type: List[str]

    def _start_subscriptions(self):
        """
        Add asyncio datagram endpoint for all subscriptions
        """
        # Add datagram endpoint for multicast announce
        multicast_listener = self._loop.create_datagram_endpoint(lambda: IMCProtocolUDP(self, is_multicast=True),
                                                                 family=socket.AF_INET)

        # Add datagram endpoint for UDP IMC messages
        imc_listener = self._loop.create_datagram_endpoint(lambda: IMCProtocolUDP(self, is_multicast=False),
                                                           family=socket.AF_INET)

        if sys.version_info < (3, 4, 4):
            self._task_mc = self._loop.create_task(multicast_listener)
            self._task_imc = self._loop.create_task(imc_listener)
        else:
            self._task_mc = asyncio.ensure_future(multicast_listener)
            self._task_imc = asyncio.ensure_future(imc_listener)

    def _setup_event_loop(self):
        """
        Setup of event loop and decorated functions
        """
        # Add event loop to instance
        if not self._loop:
            self._loop = asyncio.get_event_loop()

        decorated = [(name, method) for name, method in inspect.getmembers(self) if hasattr(method, '_decorators')]
        for name, method in decorated:
            for decorator in method._decorators:
                decorator.add_event(self._loop, self, method)

                if type(decorator) is Subscribe:
                    # Collect subscribed message types for each function
                    for msg_type in decorator.subs:
                        try:
                            self._subs[msg_type].append(method)
                        except (KeyError, AttributeError):
                            self._subs[msg_type] = [method]

        # Subscriptions has been collected from all decorators
        # Add asyncio datagram endpoints to event loop
        self._start_subscriptions()

        #self._loop.set_exception_handler() TODO

    def stop(self):
        """
        Cancels all running tasks and stops the event loop.
        :return:
        """
        logger.info('Stop called by user. Cancelling all running tasks.')
        for task in asyncio.Task.all_tasks():
            task.cancel()

        loop = self._loop

        @asyncio.coroutine
        def exit():
            logger.info('Tasks cancelled. Stopping event loop.')
            loop.stop()

        asyncio.ensure_future(exit())

    def run(self):
        """
        Starts the event loop.
        """
        # Run setup if it hasn't been done yet
        if not self._loop:
            self._setup_event_loop()

        # Start event loop
        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            pending = asyncio.Task.all_tasks()
            for task in pending:
                task.cancel()
                # Now we should await task to execute it's cancellation.
                # Cancelled task raises asyncio.CancelledError that we can suppress when canceling
                with suppress(asyncio.CancelledError):
                    self._loop.run_until_complete(task)
        finally:
            self._loop.close()

    def post_message(self, msg: pyimc.Message):
        """
        Post a message to the subscribed functions
        :param msg: The IMC message to post
        :return:
        """
        # Check that message is subclass of pyimc.Message
        # Note: messages that exists in DUNE, but has no pybind11 bindings are returned as pyimc.Message
        class_hierarchy = inspect.getmro(type(msg))
        if pyimc.Message in class_hierarchy:
            # Post message of known type
            if type(msg) is not pyimc.Message:
                try:
                    for fn in self._subs[type(msg)]:
                        fn(msg)
                except KeyError:
                    pass
            else:
                # Emit warning on IMC type without bindings
                logger.warning(
                    'Unknown IMC message received: {} ({}) from {}'.format(msg.msg_name, msg.msg_id, msg.src))

            # Post messages to functions subscribed to all messages (pyimc.Message)
            try:
                for fn in self._subs[pyimc.Message]:
                    fn(msg)
            except KeyError:
                pass
        else:
            logger.warning('Received message that is not subclass of pyimc.Message: {}'.format(type(msg)))

    def resolve_node_id(self, node_id: Union[int, str, Tuple[int, str], pyimc.Message, IMCNode]) -> IMCNode:
        """
        This function searches the map of connected nodes and returns a match (if unique)

        This function can throw the following exceptions
        KeyError: Node not found (not connected)
        AmbiguousKeyError: Multiple nodes matches the id (e.g multiple nodes announcing the same name)
        ValueError: Id parameter has an unexpected type
        :param node_id: Can be one of the following: imcid(int), imcname(str), node(tuple(int, str)), pyimc.message
        :return: An instance of the IMCNode class
        """

        # Resolve IMCNode
        id_type = type(node_id)
        if id_type is str or id_type is int:
            # Type int or str: either imc name or imc id
            # Search for keys with the name or id, raise exception if not found or ambiguous
            idx = 0 if id_type is int else 1
            possible_nodes = [x for x in self.nodes.keys() if x[idx] == node_id]
            if not possible_nodes:
                raise KeyError('Specified IMC node does not exist.')
            elif len(possible_nodes) > 1:
                raise AmbiguousKeyError('Specified IMC node has multiple possible choices', choices=possible_nodes)
            else:
                return self.nodes[possible_nodes[0]]
        elif id_type is tuple:
            # Type Tuple(int, str): unique identifier of both imc id and name
            # Determine the correct order of arguments
            if type(node_id[0]) is int and type(node_id[1]) is str:
                return self.nodes[node_id]
            else:
                raise TypeError('Node id tuple must be (int, str).')
        elif id_type is IMCNode:
            # Resolve by an preexisting IMCNode object
            return self.resolve_node_id((node_id.announce.src, node_id.announce.sys_name))
        elif isinstance(node_id, pyimc.Message):
            # Resolve by imc address in received message (equivalent to imc id)
            return self.resolve_node_id(node_id.src)
        else:
            raise TypeError('Expected node_id as int, str, tuple(int,str) or Message, received {}'.format(id_type))

    @Periodic(90)
    def prune_nodes(self):
        """
        Clear nodes that have not announced themselves or sent heartbeat in the past 90 seconds
        """
        t = time.time()
        rm_keys = []  # Avoid changes to dict during iteration
        for key, node in self.nodes.items():
            has_heartbeat = type(node.heartbeat) is float and t - node.heartbeat < 60
            has_announce = type(node.announce) is pyimc.Announce and t - node.announce.timestamp < 60
            if not (has_heartbeat or has_announce):
                logger.info('Connection to node "{}" timed out'.format(node))
                rm_keys.append(key)

        for key in rm_keys:
            try:
                logger.debug('Connection to node timed out ({})'.format(self.nodes[key]))
                del self.nodes[key]
            except (KeyError, AttributeError) as e:
                logger.exception('Encountered exception when removing node ({})'.format(e.msg))

    @Periodic(10)
    def print_debug(self):
        # Prints connected nodes every 10 seconds (debugging)
        logger.debug('Connected nodes: {}'.format(list(self.nodes.keys())))

    @Subscribe(pyimc.Announce)
    def recv_announce(self, msg):
        # TODO: Check if IP of a node changes

        # Return if announce originates from this ccu or on duplicate IMC id
        if self.announce and msg.src == self.announce.src:
            # Is another system broadcasting our IMC id?
            if msg.sys_name != self.announce.sys_name:
                logger.warning('Another system is announcing the same IMC id ({})'.format(msg.sys_name))
            return

        # Update announce
        key = (msg.src, msg.sys_name)
        try:
            self.nodes[key].update_announce(msg)
        except KeyError:
            # If the key is new, check for duplicate names/imc addresses
            key_imcadr = [x for x in self.nodes.keys() if x[0] == key[0] or x[1] == key[1]]
            if key_imcadr:
                logger.warning('Multiple nodes are announcing the same IMC address or name: {} and {}'.format(key, key_imcadr))

            # New node
            self.nodes[key] = IMCNode(msg)

    @Subscribe(pyimc.Heartbeat)
    def recv_heartbeat(self, msg):
        try:
            node = self.resolve_node_id(msg)
            node.update_heartbeat(msg)
        except (AmbiguousKeyError, KeyError):
            logger.debug('Received heartbeat from unannounced node ({})'.format(msg.src))

    @Subscribe(pyimc.EntityList)
    def recv_entity_list(self, msg):
        """
        Process received entity lists
        """
        OpEnum = pyimc.EntityList.OperationEnum
        if msg.op == OpEnum.REPORT:
            try:
                node = self.resolve_node_id(msg)
                node.update_entity_list(msg)
            except (AmbiguousKeyError, KeyError):
                logger.debug('Unable to resolve node when updating EntityList')

    @Subscribe(pyimc.EntityInfo)
    def recv_entity_info(self, msg: pyimc.EntityInfo):
        """
        Process entity info messages. Mostly for systems that does not announce EntityList
        """
        try:
            node = self.resolve_node_id(msg)
            node.update_entity_id(ent_id=msg.src_ent, ent_label=msg.label)
        except (AmbiguousKeyError, KeyError):
            pass


class ActorBase(IMCBase):
    """
    Base actor class. Implements rudimentary bookkeeping of other IMC nodes and exchange of necessary messages.
    """
    def __init__(self):
        super().__init__()
        self.t_start = time.time()

        # Set initial announce details
        self.announce = pyimc.Announce()
        self.announce.src = 0x3334  # imcjava uses 0x3333
        self.announce.sys_name = 'ccu-pyimc-{}'.format(socket.gethostname().lower())
        self.announce.sys_type = pyimc.SystemType.CCU
        self.announce.owner = 0xFFFF
        self.announce.src_ent = 1

        # Set initial entities (services generated on first announce)
        self.entities = {'Daemon': 0, 'Service Announcer': 1}

        # IMC nodes to send heartbeat signal to (maintaining comms)
        self.heartbeat = []  # type: List[Union[str, int, Tuple[int, str]]]

    def send(self, node_id, msg):
        """
        Send an imc message to the specified imc node. The node can be specified through it's imc address, system name
        or a tuple of both. If either of the first two does not uniquely specify a node an AmbiguousNode exception is 
        raised.
        :param node_id: The destination node (imc adr (int), system name (str) or a tuple(imc_adr, sys_name))
        :param msg: The imc message to send
        :return: 
        """

        # Fill out source params
        msg.src = self.announce.src
        msg.set_timestamp_now()

        node = self.resolve_node_id(node_id)
        node.send(msg)

    @Subscribe(pyimc.EntityList)
    def reply_entity_list(self, msg):
        """
        Respond to entity list queries
        """
        OpEnum = pyimc.EntityList.OperationEnum
        if msg.op == OpEnum.QUERY:
            try:
                node = self.resolve_node_id(msg)

                # Format entities into string and send back to node that requested it
                ent_lst_sorted = sorted(self.entities.items(), key=itemgetter(1))  # Sort by value (entity id)
                ent_lst = pyimc.EntityList()
                ent_lst.op = OpEnum.REPORT
                ent_lst.list = ';'.join('{}={}'.format(k, v) for k, v in ent_lst_sorted)
                self.send(node, ent_lst)
            except (AmbiguousKeyError, KeyError):
                logger.debug('Unable to resolve node when sending EntityList')

    @Periodic(30)
    def query_entity_list(self):
        """
        Request entity list from nodes without one
        """
        for k, node in self.nodes.items():
            if not node.entities:
                q_ent = pyimc.EntityList()
                q_ent.op = pyimc.EntityList.OperationEnum.QUERY
                self.send(node, q_ent)

    @Periodic(10)
    def send_announce(self):
        """
        Send an announce. Will use properties stored in this class (e.g self.lat, self.lon to set parameters)
        :return: 
        """
        # Build imc+udp string
        # TODO: Add TCP protocol for IMC
        if self._port_imc:  # Port must be ready to build IMC service string
            self.services = ['imc+udp://{}:{}/'.format(adr[1], self._port_imc) for adr in get_interfaces()]
            if not self.services:
                # No external interfaces available, announce localhost/loopback
                self.services = ['imc+udp://{}:{}/'.format(adr[1], self._port_imc) for adr in get_interfaces(False)]

            self.announce.services = ';'.join(self.services)
            with IMCSenderUDP(multicast_ip) as s:
                self.announce.set_timestamp_now()
                for i in range(30100, 30105):
                    s.send(self.announce, i)
        elif (time.time() - self.t_start) > 10:
            logger.debug('IMC socket not ready')  # Socket should be ready by now.

    @Periodic(1)
    def send_heartbeat(self):
        """
        Send a heartbeat signal to nodes specified in self.heartbeat
        """
        hb = pyimc.Heartbeat()
        for node_id in self.heartbeat:
            try:
                node = self.resolve_node_id(node_id)
                self.send(node, hb)
            except AmbiguousKeyError as e:
                logger.exception(str(e) + '({})'.format(e.choices))
            except KeyError:
                pass


if __name__ == '__main__':
    pass

