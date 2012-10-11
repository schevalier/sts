from experiment_config_lib import ControllerConfig
from sts.topology import FatTree, MeshTopology, BufferedPatchPanel
from sts.control_flow import Fuzzer,Interactive
from sts.input_traces.input_logger import InputLogger
from sts.invariant_checker import InvariantChecker

# Use POX as our controller
command_line = "./nox_core -i ptcp:6633 routing"
controllers = [ControllerConfig(command_line, cwd="nox_classic/build/src", address="127.0.0.1", port=6633)]

# Use a FatTree with 4 pods (already the default)
# (specify the class, but don't instantiate the object)
topology_class = FatTree

# Use a BufferedPatchPanel (already the default)
# (specify the class, but don't instantiate the object)
patch_panel_class = BufferedPatchPanel

# Use a Fuzzer (already the default)
control_flow = Fuzzer(input_logger=InputLogger(),
                           check_interval=80,
                           invariant_check=InvariantChecker.check_connectivity)

# Specify None as the dataplane trace (already the default)
# Otherwise, specify the path to the trace file
# (e.g. "dataplane_traces/ping_pong_same_subnet.trace")
dataplane_trace = "dataplane_traces/ping_pong_fat_tree.trace"