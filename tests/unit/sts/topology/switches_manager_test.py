# Copyright 2014      Ahmed El-Hassany
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


from tests.unit.sts.util.policy_test import PolicyGenericTest

from sts.topology.switches_manager import SwitchManagerPolicy


class HostsManagerPolicyTest(PolicyGenericTest):
  def setUp(self):
    self._policy_cls = SwitchManagerPolicy