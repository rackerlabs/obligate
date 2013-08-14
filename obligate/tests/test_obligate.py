# Copyright (c) 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Test the obligate migration: melange -> quark
"""
from clint.textui import progress
import glob
import json
from obligate.models import melange
from obligate import obligate
from obligate.utils import logit, loadSession, make_offset_lengths
from obligate.utils import migrate_tables, pad, trim_br
from quark.db import models as quarkmodels
from sqlalchemy import distinct, func  # noqa
import unittest2


class TestMigration(unittest2.TestCase):
    def setUp(self):
        self.session = loadSession()
        self.json_data = dict()
        self.log = logit('obligate.tests')

    def get_scalar(self, pk_name, filter=None, is_distinct=False):
        if is_distinct:
            return self.session.query(func.count(distinct(pk_name))).scalar()
        elif filter:
            return self.session.query(func.count(pk_name)).\
                filter(filter[0]).scalar()
        else:
            return self.session.query(func.count(pk_name)).scalar()

    def count_not_migrated(self, tablename):
        err_count = 0
        if self.json_data:
            for k, v in self.json_data[tablename]["ids"].items():
                if not v["migrated"]:
                    err_count += 1
        else:
            self.log.critical("Trying to count not migrated "
                              "but JSON doesn't exist")
        return err_count

    def get_newest_json_file(self, tablename):
        from operator import itemgetter
        import os
        files = glob.glob('logs/*{}.json'.format(tablename))
        filetimes = dict()
        for f in files:
            filetimes.update({f: os.stat(f).st_mtime})
        jsonfiles = sorted(filetimes.items(), key=itemgetter(1))
        if jsonfiles:
            most_recent = jsonfiles[-1]
            return most_recent[0]
        else:
            return None

    def test_migration(self):
        for table in progress.bar(migrate_tables, label=pad('testing')):
            file = self.get_newest_json_file(table)
            if not file:
                self.log.debug("JSON file does not exist,"
                               " for table {} re-running migration".
                               format(table))
                migration = obligate.Obligator(self.session)
                migration.flush_db()
                migration.migrate()
                migration.dump_json()
                file = self.get_newest_json_file(table)
            self.log.info("newest json file is {}".format(file))
            data = open(file)
            self.json_data.update({table: json.load(data)})
            self._validate_migration(table)

    def _validate_migration(self, tablename):
        exec("self._validate_{}()".format(tablename))

    def _validate_networks(self):
        # get_scalar(column, True) <- True == "disctinct" modifier
        blocks_count = self.get_scalar(melange.IpBlocks.network_id, [], True)
        networks_count = self.get_scalar(quarkmodels.Network.id)
        self._compare_after_migration("IP Blocks", blocks_count,
                                      "Networks", networks_count)
        _block = self.session.query(melange.IpBlocks).first()
        _network = self.session.query(quarkmodels.Network).\
            filter(quarkmodels.Network.id == _block.network_id).first()
        self.assertEqual(trim_br(_block.network_id), _network.id)
        self.assertEqual(_block.tenant_id, _network.tenant_id)
        self.assertEqual(_block.network_name, _network.name)

    def _validate_subnets(self):
        blocks_count = self.get_scalar(melange.IpBlocks.id)
        subnets_count = self.get_scalar(quarkmodels.Subnet.id)
        self._compare_after_migration("IP Blocks", blocks_count,
                                      "Subnets", subnets_count)
        _ipblock = self.session.query(melange.IpBlocks).first()
        _subnet = self.session.query(quarkmodels.Subnet).\
            filter(quarkmodels.Subnet.id == _ipblock.id).first()
        self.assertEqual(_subnet.tenant_id, _ipblock.tenant_id)
        self.assertEqual(_subnet.network_id, _ipblock.network_id)
        self.assertEqual(_subnet._cidr, _ipblock.cidr)

    def _validate_routes(self):
        routes = self.get_scalar(melange.IpRoutes.id)
        qroutes = self.get_scalar(quarkmodels.Route.id)
        err_count = self.count_not_migrated("routes")
        self._compare_after_migration("Routes", routes - err_count,
                                      "Routes", qroutes)
        _route = self.session.query(melange.IpRoutes).first()
        _ipblock = self.session.query(melange.IpBlocks).\
            filter(melange.IpBlocks.id == _route.source_block_id).first()
        _qroute = self.session.query(quarkmodels.Route).\
            filter(quarkmodels.Route.id == _route.id).first()
        self.assertEqual(_qroute.cidr, _route.netmask)
        self.assertEqual(_qroute.tenant_id, _ipblock.tenant_id)
        self.assertEqual(_qroute.gateway, _route.gateway)
        self.assertEqual(_qroute.created_at, _ipblock.created_at)
        self.assertEqual(_qroute.subnet_id, _ipblock.id)

    def _validate_ips(self):
        import netaddr
        addresses_count = self.get_scalar(melange.IpAddresses.id)
        qaddresses_count = self.get_scalar(quarkmodels.IPAddress.id)
        self._compare_after_migration("IP Addresses", addresses_count,
                                      "IP Addresses", qaddresses_count)
        _ip_addr = self.session.query(melange.IpAddresses).first()
        _ipblock = self.session.query(melange.IpBlocks).\
            filter(melange.IpBlocks.id == _ip_addr.ip_block_id).first()
        _q_ip_addr = self.session.query(quarkmodels.IPAddress).\
            filter(quarkmodels.IPAddress.id == _ip_addr.id).first()  # noqa
        _ip_address = netaddr.IPAddress(_ip_addr.address)
        self.assertEqual(_q_ip_addr.created_at, _ip_addr.created_at)
        self.assertEqual(_q_ip_addr.tenant_id, _ipblock.tenant_id)
        self.assertEqual(_q_ip_addr.network_id, trim_br(_ipblock.network_id))
        self.assertEqual(_q_ip_addr.subnet_id, _ipblock.id)
        self.assertEqual(_q_ip_addr.version, _ip_address.version)
        self.assertEqual(_q_ip_addr.address_readable, _ip_addr.address)
        self.assertTrue(_q_ip_addr.deallocated_at ==
                        None or _ip_addr.deallocated_at)
        self.assertEqual(int(_q_ip_addr.address), int(_ip_address.ipv6()))

    def _validate_interfaces(self):
        interfaces_count = self.get_scalar(melange.Interfaces.id)
        ports_count = self.get_scalar(quarkmodels.Port.id)
        err_count = self.count_not_migrated("interfaces")
        self._compare_after_migration("Interfaces",
                                      interfaces_count - err_count,
                                      "Ports", ports_count)
        # in Quark, it's easy to connect a port to a network
        # in melange, you must join on ip_addresses
        _interface = self.session.query(melange.Interfaces).first()
        _network_query = self.session.query(melange.IpBlocks).\
            join(melange.IpAddresses).\
            join(melange.Interfaces)
        self.log.info("THE INTERFACE ID IS {}".format(_interface.id))
        _network_filter = _network_query.filter(_interface.id == melange.IpAddresses.interface_id)
        self.log.info("THE QUERY WITH FILTER IS {}".format(str(_network_filter)))

        _network = _network_filter.first()
        _port = self.session.query(quarkmodels.Port).\
            filter(quarkmodels.Port.id == _interface.id).first()
        self.assertEqual(_port.device_id, _interface.device_id)
        self.assertEqual(_port.tenant_id, _interface.tenant_id)
        self.assertEqual(_port.created_at, _interface.created_at)
        self.assertEqual(_port.backend_key, "NVP_TEMP_KEY")
        self.assertEqual(_port.network_id, _network.id)

        # _network = self.session.query(melange.IpBlocks).\
        #    filter(melange.IpBlocks.id == ).first()
        # _qinterface = self.session.query().first()

    def _validate_mac_ranges(self):
        mac_ranges_count = self.get_scalar(melange.MacAddressRanges.id)
        qmac_ranges_count = self.get_scalar(quarkmodels.MacAddressRange.id)
        err_count = self.count_not_migrated("mac_ranges")
        self._compare_after_migration("MAC ranges",
                                      mac_ranges_count - err_count,
                                      "MAC ranges", qmac_ranges_count)
        _mac_range = self.session.query(melange.MacAddressRange).first()
        _q_mac_range = self.session.query(quarkmodels.MacAddressRange).first()
        self.assertEqual(_mac_range.cidr, _q_mac_range.cidr)

    def _validate_macs(self):
        macs_count = self.get_scalar(melange.MacAddresses.id)
        qmacs_count = self.get_scalar(quarkmodels.MacAddress.address)
        err_count = self.count_not_migrated("macs")
        self._compare_after_migration("MACs",
                                      macs_count - err_count,
                                      "MACs", qmacs_count)

    def _validate_policies(self):
        #blocks_count = self.get_scalar(melange.IpBlocks.id)
        #blocks_count = self.session.query(melange.IpBlocks).\
        #    filter(melange.IpBlocks.policy_id is not None).count()
        # filt = ["filter(melange.IpBlocks.policy_id != None)",
        #         "count()"]
        # blocks_count = self.get_scalar(melange.IpBlocks, filters=filt)
        blocks_count = self.get_scalar(melange.IpBlocks.id,
                                       filter=[melange.IpBlocks.policy_id != None])  # noqa
        qpolicies_count = self.get_scalar(quarkmodels.IPPolicy.id)
        err_count = self.count_not_migrated("policies")
        self._compare_after_migration("IP Block Policies",
                                      blocks_count - err_count,
                                      "Policies", qpolicies_count)

    def _get_policy_offset_total(self):
        total_policy_offsets = 0
        policy_ids = {}
        blocks = self.session.query(melange.IpBlocks).all()
        for block in blocks:
            if block.policy_id:
                if block.policy_id not in policy_ids.keys():
                    policy_ids[block.policy_id] = {}
                policy_ids[block.policy_id][block.id] = block.network_id
        octets = self.session.query(melange.IpOctets).all()
        offsets = self.session.query(melange.IpRanges).all()
        for policy, policy_block_ids in policy_ids.items():
            policy_octets = [o.octet for o in octets if o.policy_id == policy]
            policy_offsets = [(off.offset, off.length) for off in offsets
                              if off.policy_id == policy]
            policy_offsets = make_offset_lengths(policy_octets, policy_offsets)
            for block_id in policy_block_ids.keys():
                total_policy_offsets += len(policy_offsets)
        return total_policy_offsets

    def _validate_policy_rules(self):
        offsets_count = self._get_policy_offset_total()
        qpolicy_rules_count = self.get_scalar(quarkmodels.IPPolicyRange.id)
        err_count = self.count_not_migrated("policy_rules")
        self._compare_after_migration("Offsets",
                                      offsets_count - err_count,
                                      "Policy Rules", qpolicy_rules_count)

    def _compare_after_migration(self, melange_type, melange_count,
                                 quark_type, quark_count):
        message = "The number of Melange {} ({}) does " \
                  "not equal the number of Quark {} ({})".\
                  format(melange_type, melange_count, quark_type, quark_count)
        self.assertEqual(melange_count, quark_count, message)
