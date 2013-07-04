# Copyright 2011-2013 Colin Scott
# Copyright 2011-2013 Andreas Wundsam
# Copyright 2012-2012 Kyriakos Zarifis
# Copyright 2012-2013 Sam Whitlock
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from pox.openflow.libopenflow_01 import *
from entities import *
import topology_loader.topology_loader as hsa_topo
import headerspace.applications as hsa
from config_parser.openflow_parser import get_uniq_port_id
import logging
import collections
from sts.util.console import msg
import json
from collections import defaultdict

log = logging.getLogger("invariant_checker")

class InvariantChecker(object):
  def __init__(self, snapshotService):
    self.snapshotService = snapshotService

  # --------------------------------------------------------------#
  #                    Invariant checks                           #
  # --------------------------------------------------------------#

  # All invariant check methods must return a list, which is empty
  # for no violations, and non-empty for violations

  # TODO(cs): when we start logging invariant exceptions rather than halting,
  # we need to make sure that the return value of these checks are
  # determinstic (viz., always sort sets, hashes)

  @staticmethod
  def check_liveness(simulation):
    ''' Very simple: have the controllers crashed? '''
    log.debug("Checking controller liveness...")
    dead_controllers = simulation.controller_manager.check_controller_status(simulation)
    if dead_controllers:
      log.info("Problems found while checking controller liveness:")
      for (c, msg) in dead_controllers:
        log.info("Controller %s - %s" % (c.label, msg))
    if len(simulation.controller_manager.live_controllers) == 0:
      log.info("No live controllers left")
      dead_controllers = list(simulation.controller_manager.down_controllers)
    dead_controllers = [ c.label for c in dead_controllers ]
    return dead_controllers

  @staticmethod
  def python_check_loops(simulation):
    # Always check liveness if there is a single controllers
    # Dynamic imports to allow this method to be serialized
    import topology_loader.topology_loader as hsa_topo
    import headerspace.applications as hsa
    if len(simulation.controller_manager.controllers) == 1:
      # TODO(cs): a better conditional would be: are all controllers down?
      down_controllers = InvariantChecker.check_liveness(simulation)
      if down_controllers != []:
        return down_controllers
    # Warning! depends on python Hassell -- may be really slow!
    NTF = hsa_topo.generate_NTF(simulation.topology.live_switches)
    TTF = hsa_topo.generate_TTF(simulation.topology.live_links)
    loops = hsa.detect_loop(NTF, TTF, simulation.topology.live_switches)
    loops = [ str(l) for l in loops ]
    return loops

  @staticmethod
  def _get_all_pairs(simulation):
    # TODO(cs): translate HSA port numbers to ofp_phy_ports in the
    # headerspace/ module instead of computing uniq_port_id here
    access_links = simulation.topology.access_links
    all_pairs = [ (get_uniq_port_id(l1.switch, l1.switch_port),get_uniq_port_id(l2.switch, l2.switch_port))
                  for l1 in access_links
                  for l2 in access_links if l1 != l2 ]
    all_pairs = set(all_pairs)
    return all_pairs

  @staticmethod
  def python_check_connectivity(simulation):
    # Warning! depends on python Hassell -- may be really slow!
    NTF = hsa_topo.generate_NTF(simulation.topology.live_switches)
    TTF = hsa_topo.generate_TTF(simulation.topology.live_links)
    paths = hsa.find_reachability(NTF, TTF, simulation.topology.access_links)
    # Paths is: in_port -> [p_node1, p_node2]
    # Where p_node is a hash:
    #  "hdr" -> foo
    #  "port" -> foo
    #  "visits" -> foo
    connected_pairs = set()
    for in_port, p_nodes in paths.iteritems():
      for p_node in p_nodes:
        connected_pairs.add((in_port, p_node["port"]))
    all_pairs = InvariantChecker._get_all_pairs(simulation)
    remaining_pairs = all_pairs - connected_pairs
    partitioned_pairs = check_partitions(simulation.topology.switches,
                                         simulation.topology.live_links,
                                         simulation.topology.access_links)
    if len(partitioned_pairs) != 0:
      log.info("Partitioned pairs! %s" % str(partitioned_pairs))
    remaining_pairs -= partitioned_pairs

    # TODO(cs): don't print results here
    if len(remaining_pairs) > 0:
      msg.fail("Not all %d pairs are connected! (%d missing)" %
               (len(all_pairs),len(remaining_pairs)))
      log.info("remaining_pairs: %s" % (str(remaining_pairs)))
    else:
      msg.success("Fully connected!")
    return [ str(p) for p in list(remaining_pairs) ]

  @staticmethod
  def check_connectivity(simulation):
    ''' Return any pairs that couldn't reach each other '''
    # Dynamic imports to allow this method to be serialized
    from config_parser.openflow_parser import get_uniq_port_id
    from sts.util.console import msg
    # Always check liveness if there is a single controllers
    if len(simulation.controller_manager.controllers) == 1:
      # TODO(cs): a better conditional would be: are all controllers down?
      down_controllers = InvariantChecker.check_liveness(simulation)
      if down_controllers != []:
        return down_controllers

    # Effectively, run compute physical omega, ignore concrete values of headers, and
    # check that all pairs can reach eachother
    physical_omega = InvariantChecker.compute_physical_omega(simulation.topology.live_switches,
                                                             simulation.topology.live_links,
                                                             simulation.topology.access_links)
    connected_pairs = set()
    # Omegas are { original port -> [(final hs1, final port1), (final hs2, final port2)...] }
    for start_port, final_location_list in physical_omega.iteritems():
      for _, final_port in final_location_list:
        connected_pairs.add((start_port, final_port))
    all_pairs = InvariantChecker._get_all_pairs(simulation)
    remaining_pairs = all_pairs - connected_pairs
    partitioned_pairs = check_partitions(simulation.topology.switches,
                                         simulation.topology.live_links,
                                         simulation.topology.access_links)
    if len(partitioned_pairs) != 0:
      log.info("Partitioned pairs! %s" % str(partitioned_pairs))
    remaining_pairs -= partitioned_pairs

    # TODO(cs): don't print results here
    if len(remaining_pairs) > 0:
      msg.fail("Not all %d pairs are connected! (%d missing)" %
               (len(all_pairs),len(remaining_pairs)))
      log.info("remaining_pairs: %s" % (str(remaining_pairs)))
    else:
      msg.success("Fully connected!")
    return [ str(p) for p in list(remaining_pairs) ]

  @staticmethod
  def python_check_blackholes(simulation):
    '''Do any switches:
         - send packets into a down link?
         - drop packets that are supposed to go out their in_port?

       This method double checks whether it's possible for any
       packets to fall into the blackhole in the first place.

       Slightly different than check_connectivity. blackholes imply no
       connectivity, but not vice versa. No connectivity could also be due to:
         - a loop
         - PacketIn-based reactive routing
    '''
    # TODO(cs): just realized -- the C-version of Hassell might be configured to
    # *stop* as soon as it gets to an edge port. At least, this is the
    # behavior of the find_reachability function in python Hassell. So we'd
    # have to do an iterative computation: all switches that are one
    # hop away, then two hops, etc. Otherwise we wouldn't find blackholes in
    # the middle of the network.
    # For now, use a python method that explicitly
    # finds blackholes rather than inferring them from check_reachability
    # Warning! depends on python Hassell -- may be really slow!
    NTF = hsa_topo.generate_NTF(simulation.topology.live_switches)
    TTF = hsa_topo.generate_TTF(simulation.topology.live_links)
    blackholes = hsa.find_blackholes(NTF, TTF, simulation.topology.access_links)
    return [ str(b) for b in blackholes ]

  @staticmethod
  def check_correspondence(simulation):
    ''' Return if there were any policy-violations '''
    controllers_with_violations = []

    controllers_with_violations += InvariantChecker.check_liveness(simulation)

    log.debug("Snapshotting live controllers...")
    for controller in simulation.controller_manager.live_controllers:
      controller_snapshot = controller.snapshot_service.fetchSnapshot(controller)
      log.debug("Computing physical omega...")
      physical_omega = InvariantChecker.compute_physical_omega(simulation.topology.live_switches,
                                                               simulation.topology.live_links,
                                                               simulation.topology.access_links)
      log.debug("Computing controller omega...")
      # note: using all_switches to compute the controller omega. The controller might still
      # reference switches in his omega that are currently dead, which should result in a
      # policy violation, not sts crashing
      controller_omega = InvariantChecker.compute_controller_omega(controller_snapshot,
                                                                   simulation.topology.switches,
                                                                   simulation.topology.live_links,
                                                                   simulation.topology.access_links)
      violations = InvariantChecker.infer_policy_violations(physical_omega, controller_omega)
      if violations:
        controllers_with_violations.append(controller)
    return controllers_with_violations

  # --------------------------------------------------------------#
  #                    HSA utilities                              #
  # --------------------------------------------------------------#
  @staticmethod
  def compute_physical_omega(live_switches, live_links, edge_links):
    (name_tf_pairs, TTF) = InvariantChecker._get_transfer_functions(live_switches, live_links)
    physical_omega = hsa.compute_omega(name_tf_pairs, TTF, edge_links)
    return physical_omega

  @staticmethod
  def compute_controller_omega(controller_snapshot, live_switches, live_links, edge_links):
    name_tf_pairs = hsa_topo.tf_pairs_from_snapshot(controller_snapshot, live_switches)
    # Frenetic doesn't store any link or host information.
    # No virtualization though, so we can assume the same TTF. TODO(cs): for now...
    TTF = hsa_topo.generate_TTF(live_links)
    return hsa.compute_omega(name_tf_pairs, TTF, edge_links)

  @staticmethod
  def _get_transfer_functions(live_switches, live_links):
    name_tf_pairs = hsa_topo.generate_tf_pairs(live_switches)
    TTF = hsa_topo.generate_TTF(live_links)
    return (name_tf_pairs, TTF)

  @staticmethod
  def infer_policy_violations(physical_omega, controller_omega):
    ''' Return if there were any missing entries '''
    log.info("# entries in physical omega: %d" % len(physical_omega))
    log.info("# entries in controller omega: %d" % len(controller_omega))

    def get_simple_dict(omega):
      # TODO(cs): ignoring original hs means that we don't account for
      # field modifications, e.g. TTL decreases
      #
      # Omegas are { original port -> [(final hs1, final port1), (final hs2, final port2)...] }
      # Want to turn them into port -> [(final hs1, final port1), (final hs2, final port2)...]
      simple_dict = collections.defaultdict(lambda: set())
      for key, tuples in omega.iteritems():
        port = key
        for tup in tuples:
          printable_tup = (str(tup[0]), tup[1])
          simple_dict[port].add(printable_tup)
      return simple_dict

    physical_omega = get_simple_dict(physical_omega)
    controller_omega = get_simple_dict(controller_omega)

    def print_missing_entries(print_string, omega1, omega2):
      any_missing_entries = False
      for origin_port, final_locations in omega1.iteritems():
        for final_location in final_locations:
          if origin_port not in omega2 or final_location not in omega2[origin_port]:
            any_missing_entries = True
            log.info(": %s: %s" % (print_string,  str(final_location)))
      if not any_missing_entries:
        log.info("No %s!" % print_string)
      return any_missing_entries

    # (physical - controller) = missing routing policies
    missing_routing_entries = print_missing_entries("final locations in physical missing from virtual",
                                                    physical_omega, controller_omega)
    # (controller - physical) = missing ACL policies.
    missing_acl_entries = print_missing_entries("final locations in virtual missing from physical",
                                                controller_omega, physical_omega)
    return missing_routing_entries or missing_acl_entries

def check_partitions(switches, live_links, access_links):
  # TODO(cs): lifted directly from pox.forwarding.l2_multi. Highly
  # redundant!

  # Adjacency map.  [sw1][sw2] -> port from sw1 to sw2
  adjacency = defaultdict(lambda:defaultdict(lambda:None))

  for link in live_links:
    # Make sure to disregard links that are adjacent to down switches
    # (technically those links are still `live', but it's easier to treat it
    #  this way)
    if not (link.start_software_switch.failed or
            link.end_software_switch.failed):
      adjacency[link.start_software_switch][link.end_software_switch] = link

  # Switches we know of.  [dpid] -> Switch
  switches = { sw.dpid : sw for sw in switches }

  # [sw1][sw2] -> (distance, intermediate)
  path_map = defaultdict(lambda:defaultdict(lambda:(None,None)))

  def _calc_paths ():
    """
    Essentially Floyd-Warshall algorithm
    """
    sws = switches.values()
    path_map.clear()
    for k in sws:
      for j,port in adjacency[k].iteritems():
        if port is None: continue
        path_map[k][j] = (1,None)
      path_map[k][k] = (0,None) # distance, intermediate

    """
    for i in sws:
      for j in sws:
        a = path_map[i][j][0]
        #a = adjacency[i][j]
        if a is None: a = "*"
        print a,
      print
    """

    for k in sws:
      for i in sws:
        for j in sws:
          if path_map[i][k][0] is not None:
            if path_map[k][j][0] is not None:
              # i -> k -> j exists
              ikj_dist = path_map[i][k][0]+path_map[k][j][0]
              if path_map[i][j][0] is None or ikj_dist < path_map[i][j][0]:
                # i -> k -> j is better than existing
                path_map[i][j] = (ikj_dist, k)

    """
    print "--------------------"
    for i in sws:
      for j in sws:
        print path_map[i][j][0],
      print
    """

  all_link_pairs = [ (l1,l2) for l1 in access_links
                                for l2 in access_links if l1 != l2 ]

  _calc_paths()
  partioned_pairs = set()
  for link_pair in all_link_pairs:
    if path_map[link_pair[0].switch][link_pair[1].switch] == (None,None):
      id1 = get_uniq_port_id(link_pair[0].switch, link_pair[0].switch_port)
      id2 = get_uniq_port_id(link_pair[1].switch, link_pair[1].switch_port)
      partioned_pairs.add((id1,id2))
  return partioned_pairs

