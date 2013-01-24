'''
Classes for tracking replayed events.

The classes in this module get generated by 2 mechanisms:
* The fuzzer logs events that happens, and it makes one of these and
  conceptually adds it to a list of events to give a global order of events.
* The global ordered event list is parsed from an external input. This could be
  from a single log (for now), or from set of distributed logs (in the future, hopefully).

Author: sw
'''

from sts.util.console import msg
from sts.entities import Link
from sts.god_scheduler import PendingReceive, MessageReceipt
from sts.fingerprints.messages import *
from invariant_checker import InvariantChecker
import itertools
import abc
import logging
import time
import marshal
import types
import json
from collections import namedtuple
from sts.syncproto.base import SyncTime
from pox.lib.util import TimeoutError
log = logging.getLogger("events")

class Event(object):
  __metaclass__ = abc.ABCMeta

  # Create unique labels for events
  _label_gen = itertools.count(1)
  # Ensure globally unique labels
  _all_label_ids = set()

  def __init__(self, prefix="e", label=None, time=None, dependent_labels=None):
    if label is None:
      label_id = Event._label_gen.next()
      label = prefix + str(label_id)
      while label_id in Event._all_label_ids:
        label_id = Event._label_gen.next()
        label = prefix + str(label_id)
    if time is None:
      # TODO(cs): compress time for interactive mode?
      time = SyncTime.now()
    self.label = label
    Event._all_label_ids.add(int(label[1:]))
    self.time = time
    # Add on dependent labels to appease log_processing.superlog_parser.
    # TODO(cs): Replayer shouldn't depend on superlog_parser
    self.dependent_labels = dependent_labels if dependent_labels else []

  @abc.abstractmethod
  def proceed(self, simulation):
    '''Executes a single `round'. Returns a boolean that is true if the
    Replayer may continue to the next Event, otherwise proceed() again
    later.'''
    pass

  def to_json(self):
    fields = dict(self.__dict__)
    fields['class'] = self.__class__.__name__
    if ('fingerprint' in fields and
        isinstance(fields['fingerprint'][1], Fingerprint)):
      fingerprint = list(fields['fingerprint'])
      fingerprint[1] = fingerprint[1].to_dict()
      fields['fingerprint'] = tuple(fingerprint)
    return json.dumps(fields)

  def __hash__(self):
    ''' Assumption: labels are unique '''
    return self.label.__hash__()

  def __eq__(self, other):
    ''' Assumption: labels are unique '''
    if type(self) != type(other):
      return False
    return self.label == other.label

  def __ne__(self, other):
    return not self.__eq__(other)

  def __str__(self):
    return self.__class__.__name__ + ":" + self.label

  def __repr__(self):
    s = self.__class__.__name__ + ":" + self.label
    if hasattr(self, "fingerprint"):
      s += ":" + str(self.fingerprint)
    return s

# -------------------------------------------------------- #
# Semi-abstract classes for internal and external events   #
# -------------------------------------------------------- #

class InternalEvent(Event):
  '''An InternalEvent is one that happens within the controller(s) under
  simulation. Derivatives of this class verify that the internal event has
  occured in its proceed method before it returns.'''
  def __init__(self, label=None, time=None, timeout_disallowed=False):
    super(InternalEvent, self).__init__(prefix='i', label=label, time=time)
    self.timeout_disallowed = timeout_disallowed

  def proceed(self, simulation):
    # There might be nothing happening for certain internal events, so default
    # to just doing nothing for proceed (i.e. proceeding automatically).
    pass

  def disallow_timeouts(self):
    self.timeout_disallowed = True

class InputEvent(Event):
  '''An event that the simulator injects into the simulation. These events are
  assumed to be causally independent.

  Each InputEvent has a list of dependent InternalEvents that it takes in its
  constructor. This enables the pruning of events.

  This class also conceptually models (because it is equivalent to) 'external
  events', which is a term that may be used elsewhere in documentation or
  code.'''
  def __init__(self, label=None, time=None, dependent_labels=None):
    super(InputEvent, self).__init__(prefix='e', label=label, time=time,
                                     dependent_labels=dependent_labels)

# --------------------------------- #
#  Concrete classes of InputEvents  #
# --------------------------------- #

def assert_fields_exist(json_hash, *args):
  ''' assert that the fields exist in json_hash '''
  fields = args
  for field in fields:
    if field not in json_hash:
      raise ValueError("Field %s not in json_hash %s" % (field, str(json_hash)))

def extract_label_time(json_hash):
  assert_fields_exist(json_hash, 'label', 'time')
  label = json_hash['label']
  time = SyncTime(json_hash['time'][0], json_hash['time'][1])
  return (label, time)

class SwitchFailure(InputEvent):
  def __init__(self, dpid, label=None, time=None):
    super(SwitchFailure, self).__init__(label=label, time=time)
    self.dpid = dpid

  def proceed(self, simulation):
    software_switch = simulation.topology.get_switch(self.dpid)
    simulation.topology.crash_switch(software_switch)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'dpid')
    dpid = int(json_hash['dpid'])
    return SwitchFailure(dpid, label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,self.dpid,)

class SwitchRecovery(InputEvent):
  def __init__(self, dpid, label=None, time=None):
    super(SwitchRecovery, self).__init__(label=label, time=time)
    self.dpid = dpid

  def proceed(self, simulation):
    software_switch = simulation.topology.get_switch(self.dpid)
    try:
      down_controller_ids = map(lambda c: c.uuid,
                                simulation.controller_manager.down_controllers)

      simulation.topology.recover_switch(software_switch,
                                         down_controller_ids=down_controller_ids)
    except TimeoutError:
      # Controller is down... Hopefully control flow will notice soon enough
      log.warn("Timed out on %s" % str(self.fingerprint))
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'dpid')
    dpid = int(json_hash['dpid'])
    return SwitchRecovery(dpid, label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,self.dpid,)

def get_link(link_event, simulation):
  start_software_switch = simulation.topology.get_switch(link_event.start_dpid)
  end_software_switch = simulation.topology.get_switch(link_event.end_dpid)
  link = Link(start_software_switch, link_event.start_port_no,
              end_software_switch, link_event.end_port_no)
  return link

class LinkFailure(InputEvent):
  def __init__(self, start_dpid, start_port_no, end_dpid, end_port_no,
               label=None, time=None):
    super(LinkFailure, self).__init__(label=label, time=time)
    self.start_dpid = start_dpid
    self.start_port_no = start_port_no
    self.end_dpid = end_dpid
    self.end_port_no = end_port_no

  def proceed(self, simulation):
    link = get_link(self, simulation)
    simulation.topology.sever_link(link)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'start_dpid', 'start_port_no', 'end_dpid',
                        'end_port_no')
    start_dpid = int(json_hash['start_dpid'])
    start_port_no = int(json_hash['start_port_no'])
    end_dpid = int(json_hash['end_dpid'])
    end_port_no = int(json_hash['end_port_no'])
    return LinkFailure(start_dpid, start_port_no, end_dpid, end_port_no,
                       label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,
            self.start_dpid, self.start_port_no,
            self.end_dpid, self.end_port_no)

class LinkRecovery(InputEvent):
  def __init__(self, start_dpid, start_port_no, end_dpid, end_port_no,
               label=None, time=None):
    super(LinkRecovery, self).__init__(label=label, time=time)
    self.start_dpid = start_dpid
    self.start_port_no = start_port_no
    self.end_dpid = end_dpid
    self.end_port_no = end_port_no

  def proceed(self, simulation):
    link = get_link(self, simulation)
    simulation.topology.repair_link(link)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'start_dpid', 'start_port_no', 'end_dpid',
                        'end_port_no')
    start_dpid = int(json_hash['start_dpid'])
    start_port_no = int(json_hash['start_port_no'])
    end_dpid = int(json_hash['end_dpid'])
    end_port_no = int(json_hash['end_port_no'])
    return LinkRecovery(start_dpid, start_port_no, end_dpid, end_port_no,
                        label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,
            self.start_dpid, self.start_port_no,
            self.end_dpid, self.end_port_no)

class ControllerFailure(InputEvent):
  def __init__(self, controller_id, label=None, time=None):
    super(ControllerFailure, self).__init__(label=label, time=time)
    self.controller_id = controller_id

  def proceed(self, simulation):
    controller = simulation.controller_manager.get_controller(self.controller_id)
    simulation.controller_manager.kill_controller(controller)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist('controller_id')
    controller_id = json_hash['controller_id']
    controller_id = (controller_id[0], int(controller_id[1]))
    return ControllerFailure(controller_id, label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,self.controller_id)

class ControllerRecovery(InputEvent):
  def __init__(self, controller_id, label=None, time=None):
    super(ControllerRecovery, self).__init__(label=label, time=time)
    self.controller_id = controller_id

  def proceed(self, simulation):
    controller = simulation.controller_manager.get_controller(self.controller_id)
    simulation.controller_manager.reboot_controller(controller)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist('controller_id')
    controller_id = json_hash['controller_id']
    controller_id = (controller_id[0], int(controller_id[1]))
    return ControllerFailure(controller_id, label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,self.controller_id)

class HostMigration(InputEvent):
  def __init__(self, old_ingress_dpid, old_ingress_port_no,
               new_ingress_dpid, new_ingress_port_no, label=None, time=None):
    super(HostMigration, self).__init__(label=label, time=time)
    self.old_ingress_dpid = old_ingress_dpid
    self.old_ingress_port_no = old_ingress_port_no
    self.new_ingress_dpid = new_ingress_dpid
    self.new_ingress_port_no =  new_ingress_port_no

  def proceed(self, simulation):
    simulation.topology.migrate_host(self.old_ingress_dpid,
                                     self.old_ingress_port_no,
                                     self.new_ingress_dpid,
                                     self.new_ingress_port_no)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'old_ingress_dpid', 'old_ingress_port_no',
                        'new_ingress_dpid', 'new_ingress_port_no')
    old_ingress_dpid = int(json_hash['old_ingress_dpid'])
    old_ingress_port_no = int(json_hash['old_ingress_port_no'])
    new_ingress_dpid = int(json_hash['new_ingress_dpid'])
    new_ingress_port_no = int(json_hash['new_ingress_port_no'])
    return HostMigration(old_ingress_dpid, old_ingress_port_no,
                         new_ingress_dpid, new_ingress_port_no,
                         label=label, time=time)

  @property
  def fingerprint(self):
    return (self.__class__.__name__,self.old_ingress_dpid,
            self.old_ingress_port_no, self.new_ingress_dpid,
            self.new_ingress_port_no)

class PolicyChange(InputEvent):
  def __init__(self, request_type, label=None, time=None):
    super(PolicyChange, self).__init__(label=label, time=time)
    self.request_type = request_type

  def proceed(self, simulation):
    # TODO(cs): implement me, and add PolicyChanges to Fuzzer
    pass

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'request_type')
    request_type = json_hash['request_type']
    return PolicyChange(request_type, label=label, time=time)

class TrafficInjection(InputEvent):
  def __init__(self, label=None, time=None):
    super(TrafficInjection, self).__init__(label=label, time=time)

  def proceed(self, simulation):
    if simulation.dataplane_trace is None:
      raise RuntimeError("No dataplane trace specified!")
    simulation.dataplane_trace.inject_trace_event()
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    return TrafficInjection(label, time)

class WaitTime(InputEvent):
  def __init__(self, wait_time, label=None, time=None):
    ''' wait_time is specified in seconds '''
    super(WaitTime, self).__init__(label=label, time=time)
    self.wait_time = wait_time

  def proceed(self, simulation):
    log.info("WaitTime: pausing simulation for %f seconds" % (self.wait_time))
    time.sleep(self.wait_time)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'wait_time')
    wait_time = json_hash['wait_time']
    return WaitTime(wait_time, label=label, time=time)

class CheckInvariants(InputEvent):
  def __init__(self, fail_on_error=False, label=None, time=None,
               invariant_check=InvariantChecker.check_correspondence):
    super(CheckInvariants, self).__init__(label=label, time=time)
    self.fail_on_error = fail_on_error
    self.invariant_check = invariant_check

  def proceed(self, simulation):
    try:
      violations = self.invariant_check(simulation)
    except NameError as e:
      raise ValueError('''Closures are unsupported for invariant check '''
                       '''functions.\n Use dynamic imports inside of your '''
                       '''invariant check code and define all globals '''
                       '''locally.\n NameError: %s''' % str(e))

    if violations != []:
      msg.fail("The following controllers had correctness violations!: %s"
               % str(violations))
      if self.fail_on_error:
        msg.fail("Exiting: fail_on_error=True")
        exit(5)
    else:
      msg.interactive("No correctness violations!")
    return True

  def to_json(self):
    fields = dict(self.__dict__)
    fields['invariant_check'] = marshal.dumps(self.invariant_check.func_code)\
                                       .encode('base64')
    fields['invariant_name'] = self.invariant_check.__name__
    fields['class'] = self.__class__.__name__
    return json.dumps(fields)

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    fail_on_error = False
    if 'fail_on_error' in json_hash:
      fail_on_error = json_hash['fail_on_error']
    invariant_check = InvariantChecker.check_correspondence
    if 'invariant_check' in json_hash:
      # Assumes that the closure is empty
      code = marshal.loads(json_hash['invariant_check'].decode('base64'))
      invariant_check = types.FunctionType(code, globals())

    return CheckInvariants(label=label, time=time,
                           fail_on_error=fail_on_error,
                           invariant_check=invariant_check)

class ControlChannelBlock(InputEvent):
  def __init__(self, dpid, controller_id, label=None, time=None):
    super(ControlChannelBlock, self).__init__(label=label, time=time)
    self.dpid = dpid
    self.controller_id = controller_id

  def proceed(self, simulation):
    switch = simulation.topology.get_switch(self.dpid)
    connection = switch.get_connection(self.controller_id)
    if connection.currently_blocked:
      raise RuntimeError("Expected channel %s to not be blocked" % str(connection))
    connection.block()
    return True

  @property
  def fingerprint(self):
    return (self.__class__.__name__,
            self.dpid, self.controller_id)

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'dpid', 'controller_id')
    dpid = json_hash['dpid']
    controller_id = tuple(json_hash['controller_id'])
    return ControlChannelBlock(dpid, controller_id, label=label, time=time)

class ControlChannelUnblock(InputEvent):
  def __init__(self, dpid, controller_id, label=None, time=None):
    super(ControlChannelUnblock, self).__init__(label=label, time=time)
    self.dpid = dpid
    self.controller_id = controller_id

  def proceed(self, simulation):
    switch = simulation.topology.get_switch(self.dpid)
    connection = switch.get_connection(self.controller_id)
    if not connection.currently_blocked:
      raise RuntimeError("Expected channel %s to be blocked" % str(connection))
    connection.unblock()
    return True

  @property
  def fingerprint(self):
    return (self.__class__.__name__,
            self.dpid, self.controller_id)

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'dpid', 'controller_id')
    dpid = json_hash['dpid']
    controller_id = tuple(json_hash['controller_id'])
    return ControlChannelUnblock(dpid, controller_id, label=label, time=time)

# TODO(cs): DataplaneDrop/Permits have really complicated dependencies
# with other input events!
# For now, turn them off completely.
class DataplaneDrop(InputEvent):
  def __init__(self, fingerprint, label=None, time=None):
    super(DataplaneDrop, self).__init__(label=label, time=time)
    if fingerprint[0] != self.__class__.__name__:
      fingerprint = list(fingerprint)
      fingerprint.insert(0, self.__class__.__name__)
    if type(fingerprint) == list:
      fingerprint = (fingerprint[0], DPFingerprint(fingerprint[1]),
                     fingerprint[2], fingerprint[3])
    self.fingerprint = fingerprint

  def proceed(self, simulation):
    dp_event = simulation.patch_panel.get_buffered_dp_event(self.fingerprint[1:])
    if dp_event is not None:
      simulation.patch_panel.drop_dp_event(dp_event)
      return True
    return False

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'fingerprint')
    fingerprint = json_hash['fingerprint']
    return DataplaneDrop(fingerprint, label=label, time=time)

  def to_json(self):
    fields = dict(self.__dict__)
    fields['class'] = self.__class__.__name__
    fields['fingerprint'] = (self.fingerprint[0], self.fingerprint[1].to_dict(),
                             self.fingerprint[2], self.fingerprint[3])
    return json.dumps(fields)

class DataplanePermit(InputEvent):
  def __init__(self, fingerprint, label=None, time=None):
    super(DataplanePermit, self).__init__(label=label, time=time)
    if fingerprint[0] != self.__class__.__name__:
      fingerprint = list(fingerprint)
      fingerprint.insert(0, self.__class__.__name__)
    if type(fingerprint) == list:
      fingerprint = (fingerprint[0], DPFingerprint(fingerprint[1]),
                     fingerprint[2], fingerprint[3])
    self.fingerprint = fingerprint

  def proceed(self, simulation):
    dp_event = simulation.patch_panel.get_buffered_dp_event(self.fingerprint[1:])
    if dp_event is not None:
      simulation.patch_panel.permit_dp_event(dp_event)
      return True
    return False

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'fingerprint')
    fingerprint = json_hash['fingerprint']
    return DataplanePermit(fingerprint, label=label, time=time)

  def to_json(self):
    fields = dict(self.__dict__)
    fields['class'] = self.__class__.__name__
    fields['fingerprint'] = (self.fingerprint[0], self.fingerprint[1].to_dict(),
                             self.fingerprint[2], self.fingerprint[3])
    return json.dumps(fields)

# TODO(cs): Temporary hack until we figure out determinism
class LinkDiscovery(InputEvent):
  def __init__(self, controller_id, link_attrs, label=None, time=None):
    super(LinkDiscovery, self).__init__(label=label, time=time)
    self.fingerprint = (self.__class__.__name__,
                        controller_id, tuple(link_attrs))
    self.controller_id = tuple(controller_id)
    self.link_attrs = link_attrs

  def proceed(self, simulation):
    controller = simulation.controller_manager.get_controller(self.controller_id)
    controller.sync_connection.send_link_notification(self.link_attrs)
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time) = extract_label_time(json_hash)
    assert_fields_exist(json_hash, 'controller_id', 'link_attrs')
    controller_id = json_hash['controller_id']
    link_attrs = json_hash['link_attrs']
    return LinkDiscovery(controller_id, link_attrs, label=label, time=time)

all_input_events = [SwitchFailure, SwitchRecovery, LinkFailure, LinkRecovery,
                    ControllerFailure, ControllerRecovery, HostMigration,
                    PolicyChange, TrafficInjection, WaitTime, CheckInvariants,
                    ControlChannelBlock, ControlChannelUnblock,
                    DataplaneDrop, DataplanePermit, LinkDiscovery]

# ----------------------------------- #
#  Concrete classes of InternalEvents #
# ----------------------------------- #

def extract_base_fields(json_hash):
  (label, time) = extract_label_time(json_hash)
  timeout_disallowed = False
  if 'timeout_disallowed' in json_hash:
    timeout_disallowed = json_hash['timeout_disallowed']
  return (label, time, timeout_disallowed)

class ControlMessageReceive(InternalEvent):
  '''
  Logged whenever the GodScheduler decides to allow a switch to see an
  openflow packet.
  '''
  def __init__(self, dpid, controller_id, fingerprint, label=None, time=None, timeout_disallowed=False):
    # If constructed directly (not from json), fingerprint is the
    # OFFingerprint, not including dpid and controller_id
    super(ControlMessageReceive, self).__init__(label=label, time=time, timeout_disallowed=timeout_disallowed)
    self.dpid = dpid
    self.controller_id = controller_id
    if type(fingerprint) == list:
      fingerprint = (fingerprint[0], OFFingerprint(fingerprint[1]),
                     fingerprint[2], tuple(fingerprint[3]))
    if type(fingerprint) == dict or type(fingerprint) != tuple:
      fingerprint = (self.__class__.__name__, OFFingerprint(fingerprint),
                     dpid, controller_id)

    self.fingerprint = fingerprint

  def proceed(self, simulation):
    pending_receive = PendingReceive(self.dpid, self.controller_id,
                                     self.fingerprint[1])
    message_waiting = simulation.god_scheduler.message_waiting(pending_receive)
    if message_waiting:
      simulation.god_scheduler.schedule(pending_receive)
      return True
    return False

  def __str__(self):
    return "ControlMessageReceive c %s -> s %s [%s]" % (self.controller_id, self.dpid, self.fingerprint[1].human_str())

  @staticmethod
  def from_json(json_hash):
    (label, time, timeout_disallowed) = extract_base_fields(json_hash)
    assert_fields_exist(json_hash, 'dpid', 'controller_id', 'fingerprint')
    dpid = json_hash['dpid']
    controller_id = tuple(json_hash['controller_id'])
    fingerprint = json_hash['fingerprint']
    return ControlMessageReceive(dpid, controller_id, fingerprint, label=label, time=time, timeout_disallowed=timeout_disallowed)

# TODO(cs): move me?
class PendingStateChange(namedtuple('PendingStateChange',
                                ['controller_id', 'time', 'fingerprint',
                                 'name', 'value'])):
  def __new__(cls, controller_id, time, fingerprint, name, value):
    controller_id = tuple(controller_id)
    if type(time) == list:
      time = tuple(time)
    if type(fingerprint) == list:
      fingerprint = tuple(fingerprint)
    if type(value) == list:
      value = tuple(value)
    return super(cls, PendingStateChange).__new__(cls, controller_id, time,
                                                  fingerprint, name, value)

  def _get_regex(self):
    # TODO(cs): if we add varargs to the signature, this needs to be changed
    if type(self.fingerprint) == tuple:
      # Skip over the class name
      return self.fingerprint[1]
    return self.fingerprint

  def __hash__(self):
    # TODO(cs): may need to add more context into the fingerprint to avoid
    # ambiguity
    return self._get_regex().__hash__() + self.controller_id.__hash__()

  def __eq__(self, other):
    if type(other) != type(self):
      return False
    return (self._get_regex() == other._get_regex() and
            self.controller_id == other.controller_id)

  def __ne__(self, other):
    # NOTE: __ne__ in python does *NOT* by default delegate to eq
    return not self.__eq__(other)


class ControllerStateChange(InternalEvent):
  '''
  Logged for any relevent kind of state change in the controller (e.g.
  mastership change)
  '''
  def __init__(self, controller_id, fingerprint, name, value, label=None, time=None, timeout_disallowed=False):
    super(ControllerStateChange, self).__init__(label=label, time=time, timeout_disallowed=timeout_disallowed)
    self.controller_id = tuple(controller_id)
    if type(fingerprint) == str or type(fingerprint) == unicode:
      fingerprint = (self.__class__.__name__, fingerprint)
    if type(fingerprint) == list:
      fingerprint = tuple(fingerprint)
    self._fingerprint = fingerprint
    self.name = name
    if type(value) == list:
      value = tuple(value)
    self.value = value

  def proceed(self, simulation):
    pending_state_change = PendingStateChange(self.controller_id, self.time,
                                              self._get_message_fingerprint(),
                                              self.name, self.value)
    observed_yet = simulation.controller_sync_callback\
                             .state_change_pending(pending_state_change)
    if observed_yet:
      simulation.controller_sync_callback\
                .ack_pending_state_change(pending_state_change)
      return True
    # TODO(cs): possibly need to flush all pending state changes our change
    # hasn't been observed yet, since:
    #  - pending state changes block the controller
    #  - after pruning, different code paths may be taken, and the
    #    controller may just blocked on a pending state change prior to this
    #    one. Seems like scenario this would cause excessive timeouts.
    return False

  def _get_message_fingerprint(self):
    return self._fingerprint[1]

  @property
  def fingerprint(self):
    # Somewhat confusing: the StateChange's fingerprint is self._fingerprint,
    # but the overall fingerprint of this event needs to include the controller
    # id
    return tuple(list(self._fingerprint) + [self.controller_id])

  @staticmethod
  def from_json(json_hash):
    (label, time, timeout_disallowed) = extract_base_fields(json_hash)
    assert_fields_exist(json_hash, 'controller_id', '_fingerprint',
                        'name', 'value')
    controller_id = tuple(json_hash['controller_id'])
    fingerprint = json_hash['_fingerprint']
    name = json_hash['name']
    value = json_hash['value']
    return ControllerStateChange(controller_id, fingerprint, name, value,
                                 label=label, time=time, timeout_disallowed=timeout_disallowed)

class DeterministicValue(InternalEvent):
  '''
  Logged whenever the controller asks for a deterministic value (e.g.
  gettimeofday()
  '''
  def __init__(self, controller_id, name, value, label=None, time=None, timeout_disallowed=False):
    super(DeterministicValue, self).__init__(label=label, time=time, timeout_disallowed=timeout_disallowed)
    self.controller_id = tuple(controller_id)
    self.name = name
    if name == "gettimeofday":
      value = SyncTime(seconds=value[0], microSeconds=value[1])
    elif type(value) == list:
      value = tuple(value)
    self.value = value

  def proceed(self, simulation):
    if simulation.controller_sync_callback\
                 .pending_deterministic_value_request(self.controller_id):
      simulation.controller_sync_callback.send_deterministic_value(self.controller_id,
                                                                   self.value)
      return True
    return False

  @staticmethod
  def from_json(json_hash):
    (label, time, timeout_disallowed) = extract_base_fields(json_hash)
    assert_fields_exist(json_hash, 'controller_id',
                        'name', 'value')
    controller_id = tuple(json_hash['controller_id'])
    name = json_hash['name']
    value = json_hash['value']
    return DeterministicValue(controller_id, name, value,
                              label=label, time=time, timeout_disallowed=timeout_disallowed)


# TODO(cs): this should really be an input event. But need to make sure that
# it can be pruned safely
class ConnectToControllers(InternalEvent):
  def proceed(self, simulation):
    simulation.connect_to_controllers()
    return True

  @staticmethod
  def from_json(json_hash):
    (label, time, timeout_disallowed) = extract_base_fields(json_hash)
    return ConnectToControllers(label=label, time=time, timeout_disallowed=timeout_disallowed)


all_internal_events = [ControlMessageReceive, ConnectToControllers,
                       ControllerStateChange, DeterministicValue]

# Special event:

class InvariantViolation(Event):
  ''' Class for logging violations as json dicts '''
  def __init__(self, violations):
    Event.__init__(self)
    self.violations = [ str(v) for v in violations ]

  def proceed(self, simulation):
    return True
